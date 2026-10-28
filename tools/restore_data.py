# -*- coding: utf-8 -*-
"""
restore_data.py -- rebuild voxel_data/ and the built level from the
``data-backup`` GitHub release (uploaded by tools/archive_data.py).

Run on a fresh clone (stdlib only):

    python tools/restore_data.py            # both archives
    python tools/restore_data.py --only voxel

Token: same resolution as archive_data.py (GH_TOKEN env or .gh_token file).
The voxel archive unpacks to <repo>/voxel_data, the content archive to
<repo>/project/Content/{MC,Maps} -- after which tools/build_world.py and the
package bats work on a machine that never saw the original save.
"""

import argparse
import os
import sys
import tarfile
import tempfile
import urllib.request

from archive_data import REPO, REPO_SLUG, TAG, API, load_token, gh  # noqa: E402


def download(token, name, dest):
    req = urllib.request.Request(
        API + "/repos/%s/releases/tags/%s" % (REPO_SLUG, TAG),
        headers={"Authorization": "token %s" % token,
                 "Accept": "application/vnd.github+json"})
    import json
    with urllib.request.urlopen(req) as r:
        rel = json.loads(r.read())
    asset = next((a for a in rel.get("assets", []) if a["name"] == name), None)
    if asset is None:
        print("[restore_data] asset %s not found on release %s" % (name, TAG))
        return None
    print("[restore_data] downloading %s (%.1f MB) ..."
          % (name, asset["size"] / 1e6))
    req = urllib.request.Request(asset["url"], headers={
        "Authorization": "token %s" % token,
        "Accept": "application/octet-stream"})
    with urllib.request.urlopen(req) as r, open(dest, "wb") as fh:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=("voxel", "content"), default="")
    ap.add_argument("--workdir", default="")
    args = ap.parse_args()

    token = load_token()
    if not token:
        return 1
    work = args.workdir or tempfile.mkdtemp(prefix="mc2ue5_restore_")
    os.makedirs(work, exist_ok=True)

    jobs = []
    if args.only in ("", "voxel"):
        jobs.append(("voxel_data.tar.gz", REPO))
    if args.only in ("", "content"):
        jobs.append(("content_level.tar.gz", os.path.join(REPO, "project")))
    for name, unpack_root in jobs:
        path = os.path.join(work, name)
        if not download(token, name, path):
            return 1
        print("[restore_data] unpacking into %s" % unpack_root)
        with tarfile.open(path, "r:gz") as tf:
            tf.extractall(unpack_root)
    print("[restore_data] done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
