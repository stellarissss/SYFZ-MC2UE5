# -*- coding: utf-8 -*-
"""verify_lfs_object.py -- prove that an LFS object upstream really is ours.

Why this exists
---------------
`git lfs push` on this host dies at the transfer step with
`LFS: Not Implemented: https://github-cloud.s3.amazonaws.com/...` -- a
transparent proxy in front of the machine answers 501 for the PUT. So the
question "is the 58 MB overworld.bin actually backed up?" cannot be answered by
`git lfs push` reporting success, and cannot be answered by the repo either: a
pointer file is 130 bytes of ASCII and always "exists".

The only honest answer is to fetch the object back through the download path
(which works) and compare bytes:

    upstream bytes  ==  local bytes   ->  sha256 equal

We compare the **whole** object, not the head, because a truncated transfer is
the failure mode that a size check would miss.

Usage:
    python tools/verify_lfs_object.py <path-in-repo> [--repo owner/name]
    python tools/verify_lfs_object.py voxel_data/full/overworld.bin
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BATCH = ("https://ghfast.top/https://github.com/%s.git/info/lfs/objects/batch")
CT = "application/vnd.git-lfs+json"


def token():
    t = os.environ.get("MC2UE5_PAT") or os.environ.get("GH_TOKEN")
    if not t:
        raise SystemExit("no token: set MC2UE5_PAT in the environment")
    return "Basic " + base64.b64encode(
        ("stellarissss:" + t).encode("ascii")).decode("ascii")


def http(url, method="GET", headers=None, body=None, timeout=1800):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def pointer_oid(repo_dir, rel):
    blob = subprocess.run(["git", "cat-file", "-p", ":" + rel], cwd=repo_dir,
                          capture_output=True, check=True).stdout
    if not blob.startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise SystemExit("%s is not an LFS pointer in the index" % rel)
    oid = size = None
    for line in blob.decode().splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            size = int(line.split(" ", 1)[1].strip())
    return oid, size


def sha256_file(path):
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
            h.update(chunk)
    return h.hexdigest(), n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--repo", default="stellarissss/SYFZ-MC2UE5")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--repo-dir", default=os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    rel = args.path.replace("\\", "/")
    oid, size = pointer_oid(args.repo_dir, rel)
    print("pointer oid   %s" % oid)
    print("pointer size  %d" % size)

    local_path = os.path.join(args.repo_dir, rel)
    if not os.path.exists(local_path):
        raise SystemExit("local file missing: %s" % local_path)
    local_hash, local_size = sha256_file(local_path)
    print("local  sha256 %s  (%d bytes)" % (local_hash, local_size))

    body = json.dumps({
        "operation": "download",
        "transfers": ["basic"],
        "objects": [{"oid": oid, "size": size}],
        "ref": {"name": "refs/heads/" + args.ref},
    }).encode()
    hdr = {"Authorization": token(), "Accept": CT, "Content-Type": CT}
    st, data, _ = http(BATCH % args.repo, method="POST", headers=hdr, body=body)
    print("batch POST    -> %d" % st)
    if st != 200:
        print(data[:400].decode("utf-8", "replace"))
        return 2
    obj = (json.loads(data.decode()).get("objects") or [{}])[0]
    if obj.get("error"):
        print("batch error:", obj["error"])
        return 2
    dl = (obj.get("actions") or {}).get("download") or {}
    href = dl.get("href")
    if not href:
        print("no download action -> the object is NOT upstream")
        return 1
    print("download host %s" % href.split("/")[2])

    up_hdr = dict(dl.get("header") or {})
    st, payload, hh = http(href, headers=up_hdr, timeout=3600)
    print("GET           -> %d  %d bytes" % (st, len(payload)))
    if st != 200:
        print(payload[:300].decode("utf-8", "replace"))
        return 2
    up_hash = hashlib.sha256(payload).hexdigest()
    print("upstream sha256 %s" % up_hash)

    same = (up_hash == local_hash) and (len(payload) == local_size)
    print()
    if same:
        print("RESULT: PASS -- upstream bytes are byte-identical to the local "
              "file (%d bytes, sha256 %s)" % (local_size, local_hash))
        return 0
    print("RESULT: FAIL -- upstream differs from local "
          "(upstream %d B / %s, local %d B / %s)"
          % (len(payload), up_hash, local_size, local_hash))
    return 1


if __name__ == "__main__":
    sys.exit(main())
