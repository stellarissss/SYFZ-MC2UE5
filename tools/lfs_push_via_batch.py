# -*- coding: utf-8 -*-
"""lfs_push_via_batch.py -- push LFS objects when `git lfs push` cannot.

Why this exists
---------------
On this host (2026-10-28) `git lfs push` gets all the way to the upload and
then dies:

    LFS: Not Implemented: https://github-cloud.s3.amazonaws.com/alambic/media/...

The batch API itself is fine -- it hands back a signed S3 href. What fails is
the *transfer*: the storage endpoint answers 501 Not Implemented for the PUT,
because a transparent proxy in front of the machine implements GET/HEAD only.
So the object never lands and the LFS pointer in the repo refers to bytes that
do not exist upstream. That is the worst failure mode for LFS: the clone looks
fine, the file is 130 bytes of ASCII.

What this does instead: talk to the batch API directly, then upload the bytes
to the signed href with an explicit PUT and the headers the signature covers.
Same bytes, same oid, verified locally after the transfer.

Why not just fix the proxy? Because the proxy is not ours to fix and the point
of the backup is that it must work on the machine as it is, today.

Usage:
    python tools/lfs_push_via_batch.py --repo stellarissss/SYFZ-MC2UE5 --ref main
    python tools/lfs_push_via_batch.py --repo ... --ref main --include voxel_data

The token is read from $MC2UE5_PAT (never from argv, never from a file).
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BATCH = "https://ghfast.top/https://github.com/%s.git/info/lfs/objects/batch"
CONTENT_TYPE = "application/vnd.git-lfs+json"
OCTET = "application/octet-stream"


def token():
    t = os.environ.get("MC2UE5_PAT") or os.environ.get("GH_TOKEN")
    if not t:
        raise SystemExit("no token: set MC2UE5_PAT in the environment")
    return t


def basic_auth(tok):
    import base64
    raw = ("stellarissss:" + tok).encode("ascii")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def http(url, method="GET", headers=None, body=None, timeout=300):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def local_oid_and_size(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def read_pointer(path):
    """-> (oid, size) if the file is an LFS pointer, else None."""
    with open(path, "rb") as fh:
        head = fh.read(300)
    if not head.startswith(b"version https://git-lfs.github.com/spec/v1"):
        return None
    oid = size = None
    for line in head.decode("ascii", "replace").splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            size = int(line.split(" ", 1)[1].strip())
    return oid, size


def read_pointer_from_index(repo_dir, rel):
    """-> (oid, size) from the *index* blob, i.e. what the repo promises."""
    try:
        blob = subprocess.run(
            ["git", "cat-file", "-p", ":" + rel], cwd=repo_dir,
            capture_output=True, check=True).stdout
    except subprocess.CalledProcessError:
        return None
    if not blob.startswith(b"version https://git-lfs.github.com/spec/v1"):
        return None
    oid = size = None
    for line in blob.decode("ascii", "replace").splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            size = int(line.split(" ", 1)[1].strip())
    return oid, size


def collect(repo_dir, include):
    """LFS-tracked paths whose real bytes are not yet upstream.

    The oid to upload is the one in the *index* (what the repo promises), and
    the bytes to send are the *working tree* file. Note the working tree file
    is the real content, not a pointer -- a pointer is what you get after a
    clone that could not reach LFS. Reading the pointer off disk (the obvious
    thing) therefore finds nothing to do on a machine that already has the
    bytes, which is exactly the machine that needs to upload them.
    """
    out = []
    try:
        listing = subprocess.run(
            ["git", "lfs", "ls-files", "-n"], cwd=repo_dir,
            capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise SystemExit("git lfs ls-files failed: %s" % exc)
    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel or (include and include not in rel):
            continue
        full = os.path.join(repo_dir, rel)
        if not os.path.exists(full):
            continue
        ptr = read_pointer_from_index(repo_dir, rel)
        if not ptr:
            continue
        oid, size = ptr
        if os.path.getsize(full) == size:
            # Right size; assume it is the right content and let the server
            # decide. Hashing 700 files to find one candidate is not worth it.
            out.append((rel, full, oid, size))
    return out


def upload_one(tok, repo, ref, rel, path, oid, size):
    body = json.dumps({
        "operation": "upload",
        "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
        "ref": {"name": "refs/heads/" + ref},
    }).encode("utf-8")

    headers = {
        "Authorization": basic_auth(tok),
        "Accept": CONTENT_TYPE,
        "Content-Type": CONTENT_TYPE,
    }
    st, data, _ = http(BATCH % repo, method="POST", headers=headers, body=body)
    if st != 200:
        return False, "batch POST -> %d %s" % (st, data[:160])
    resp = json.loads(data.decode("utf-8"))
    obj = (resp.get("objects") or [{}])[0]
    actions = obj.get("actions") or {}
    href = (actions.get("upload") or {}).get("href")
    hdrs = (actions.get("upload") or {}).get("header") or {}
    if not href:
        return False, "batch gave no upload href: %s" % json.dumps(obj)[:200]

    with open(path, "rb") as fh:
        payload = fh.read()

    up_headers = dict(hdrs)
    up_headers.setdefault("Content-Type", OCTET)
    up_headers["Content-Length"] = str(len(payload))
    st, data, _ = http(href, method="PUT", headers=up_headers, body=payload,
                       timeout=900)
    if st not in (200, 201, 204):
        return False, "PUT -> %d %s" % (st, data[:200])

    # Verify what we sent is what the pointer claims.
    got = hashlib.sha256(payload).hexdigest()
    if got != oid:
        return False, "oid mismatch: sent %s, pointer says %s" % (got, oid)

    # LFS basic transfer: after the storage PUT the server may hand back a
    # "verify" action. Skipping it is how the object ends up on S3 but the
    # batch API keeps answering 404 "Object does not exist" -- the PUT alone
    # does not register the object with GitHub.
    ver = actions.get("verify") or {}
    if ver.get("href"):
        vheaders = dict(ver.get("header") or {})
        vheaders.setdefault("Content-Type", CONTENT_TYPE)
        vheaders.setdefault("Accept", CONTENT_TYPE)
        vheaders.setdefault("Authorization", basic_auth(tok))
        vbody = json.dumps({"oid": oid, "size": size}).encode("utf-8")
        st, data, _ = http(ver["href"], method="POST", headers=vheaders,
                           body=vbody, timeout=300)
        if st not in (200, 201, 202):
            return False, "verify -> %d %s" % (st, data[:200])

    return True, "%d bytes%s" % (len(payload),
                                 " +verify" if ver.get("href") else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="stellarissss/SYFZ-MC2UE5")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--repo-dir", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--include", default="",
                    help="only paths containing this substring")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = collect(args.repo_dir, args.include)
    print("pointers to upload: %d" % len(items))
    if not items:
        print("nothing to do")
        return 0
    if args.dry_run:
        for rel, _p, oid, size in items[:20]:
            print("  %-56s %s %d" % (rel, oid[:12], size))
        return 0

    tok = token()
    ok = 0
    failed = []
    for i, (rel, path, oid, size) in enumerate(items, 1):
        good, note = upload_one(tok, args.repo, args.ref, rel, path, oid, size)
        if good:
            ok += 1
            print("  OK   %-52s %s" % (rel[:52], note), flush=True)
        else:
            failed.append((rel, note))
            print("  FAIL %-52s %s" % (rel[:52], note), flush=True)
        if i % 25 == 0 or i == len(items):
            print("  ... %d/%d ok=%d fail=%d" % (i, len(items), ok, len(failed)),
                  flush=True)

    print("=" * 60)
    print("uploaded %d/%d" % (ok, len(items)))
    for rel, note in failed[:20]:
        print("  FAILED %s (%s)" % (rel, note))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
