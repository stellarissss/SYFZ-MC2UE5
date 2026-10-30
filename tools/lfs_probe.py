# -*- coding: utf-8 -*-
"""lfs_probe.py -- ask the LFS batch API what it thinks about an oid.

Used to distinguish two very different situations that look identical from the
client side:

  * "the batch response for `upload` has no `actions`" can mean the object is
    **already on the server** (LFS omits the upload action then), and
  * it can also mean the proxy in front of the batch endpoint **stripped the
    field**.

Only a `download` request settles it: if the proxy were mangling responses, the
download action would be missing too. And a download action is proof the bytes
are upstream -- we then fetch a few bytes and compare the sha256.

Usage:
    python tools/lfs_probe.py <oid> [<oid> ...]
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

BATCH = "https://ghfast.top/https://github.com/stellarissss/SYFZ-MC2UE5.git/info/lfs/objects/batch"
CT = "application/vnd.git-lfs+json"


def token():
    t = os.environ.get("MC2UE5_PAT") or os.environ.get("GH_TOKEN")
    if not t:
        raise SystemExit("no token: set MC2UE5_PAT")
    import base64
    return "Basic " + base64.b64encode(
        ("stellarissss:" + t).encode("ascii")).decode("ascii")


def http(url, method="GET", headers=None, body=None, timeout=300):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers or {})


def main():
    oids = sys.argv[1:]
    if not oids:
        raise SystemExit(__doc__)
    hdr = {"Authorization": token(), "Accept": CT, "Content-Type": CT}
    for oid in oids:
        body = json.dumps({
            "operation": "download",
            "transfers": ["basic"],
            "objects": [{"oid": oid, "size": 0}],
            "ref": {"name": "refs/heads/main"},
        }).encode()
        st, data, _ = http(BATCH, method="POST", headers=hdr, body=body)
        print("=" * 70)
        print("oid %s  batch POST -> %d" % (oid[:16], st))
        if st != 200:
            print("  body:", data[:300])
            continue
        obj = (json.loads(data.decode()).get("objects") or [{}])[0]
        if "error" in obj:
            print("  error:", obj["error"])
        acts = obj.get("actions") or {}
        print("  actions:", list(acts.keys()) or "(none)")
        dl = acts.get("download") or {}
        href = dl.get("href")
        if not href:
            print("  -> no download action; object is NOT upstream")
            continue
        print("  -> download action present, fetching first 64 KB to verify")
        st, chunk, hh = http(href, timeout=300)
        print("  GET -> %d  %d bytes  content-length=%s"
              % (st, len(chunk), hh.get("Content-Length")))
        if st == 200 and len(chunk) == 64 * 1024:
            print("  sha256(first 64KB) =", hashlib.sha256(chunk).hexdigest())
        elif st == 200:
            print("  sha256(whole) =", hashlib.sha256(chunk).hexdigest())


if __name__ == "__main__":
    main()
