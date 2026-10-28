# -*- coding: utf-8 -*-
"""check_lfs_state.py -- are the LFS-tracked files real content or pointers?

Why this exists
---------------
After the 10-28 drive migration a clone/pull can leave git-lfs *pointer files*
in the working tree. A pointer is a 130-byte text file that starts with
`version https://git-lfs.github.com/spec/v1`. It has a `.png` name, it opens, it
is the right size class for nothing -- and every tool that reads it will happily
produce garbage instead of failing. In this project that means the atlas bake
would average 130 bytes of ASCII per "texture" and the result would be a
plausible-looking but wrong atlas.

So the check is: count the files git-lfs is supposed to manage, and count how
many of them are still pointers. Non-zero pointer count is a hard "do not
re-bake anything yet".

Usage:
    python tools/check_lfs_state.py            # summary only
    python tools/check_lfs_state.py --list     # also print every pointer path

Exit code: 0 = every LFS file is real content, 1 = pointers remain.
"""

import argparse
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POINTER_MAGIC = b"version https://git-lfs.github.com/spec/v1"


def lfs_tracked():
    """Paths git-lfs manages, straight from git (no pattern guessing)."""
    try:
        out = subprocess.run(
            ["git", "lfs", "ls-files", "-n"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print("cannot ask git-lfs: %s" % exc, flush=True)
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def is_pointer(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(len(POINTER_MAGIC)) == POINTER_MAGIC
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="print every pointer path")
    args = ap.parse_args()

    paths = lfs_tracked()
    if paths is None:
        return 2

    pointers = []
    missing = []
    for rel in paths:
        full = os.path.join(REPO, rel)
        if not os.path.exists(full):
            missing.append(rel)
        elif is_pointer(full):
            pointers.append(rel)

    print("repo        %s" % REPO, flush=True)
    print("lfs tracked %d" % len(paths), flush=True)
    print("real        %d" % (len(paths) - len(pointers) - len(missing)), flush=True)
    print("pointers    %d" % len(pointers), flush=True)
    print("missing     %d" % len(missing), flush=True)

    # Group pointers by top-level directory: one line says whether this is the
    # block-texture tree (cosmetic for the atlas) or the UE assets (fatal).
    buckets = {}
    for rel in pointers:
        top = rel.split("/")[0] if "/" in rel else rel
        buckets[top] = buckets.get(top, 0) + 1
    for top in sorted(buckets, key=lambda k: -buckets[k]):
        print("  pointers in %-24s %d" % (top, buckets[top]), flush=True)

    if args.list:
        for rel in pointers:
            print("  POINTER " + rel, flush=True)

    if pointers or missing:
        print("\nRESULT: INCOMPLETE -- run `git lfs pull` before re-baking anything",
              flush=True)
        return 1
    print("\nRESULT: OK -- every LFS-tracked file holds real content", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
