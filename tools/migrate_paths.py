# -*- coding: utf-8 -*-
"""One-shot migration helper: rewrite the dead drive root in text files.

The environment moved from a drive mounted as Q: to G:\\BaiduNetdiskDownload\\real.
Every literal that started with the old root still *parsed* after the move, which
is why nothing failed fast -- so they have to be rewritten wholesale rather than
found one failure at a time.

Mapping (uniform, because the old Q:\\ and the new root hold the same subtrees):

    Q:\\MC2UE5\\repo        -> <NEWROOT>\\MC2UE5\\repo
    Q:\\MC2UE5\\logs        -> <NEWROOT>\\MC2UE5\\logs
    Q:\\MC2UE5\\assets_cc0  -> <NEWROOT>\\MC2UE5\\assets_cc0
    Q:\\UE\\UE_5.8          -> <NEWROOT>\\UE\\UE_5.8
    Q:\\AutoSDK            -> <NEWROOT>\\AutoSDK
    /q/MC2UE5/...          -> /g/BaiduNetdiskDownload/real/MC2UE5/...

Skipped on purpose:
  * project/Intermediate, project/Saved -- regenerated caches, rewriting them is
    churn that hides real diffs
  * tools/mcpaths.py -- documents the old root deliberately
  * .git, out/ (binaries), voxel_data

Usage:
    python tools/migrate_paths.py            # dry run, prints counts
    python tools/migrate_paths.py --apply    # rewrite in place
"""

import argparse
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEWROOT = "G:\\BaiduNetdiskDownload\\real"

SKIP_DIRS = {
    ".git", "Intermediate", "Saved", "__pycache__", "voxel_data",
    "assets_cc0", "dist", "node_modules",
}
SKIP_FILES = {"tools/mcpaths.py", "tools/migrate_paths.py"}
EXTS = {".py", ".bat", ".md", ".txt", ".ini", ".json", ".cs", ".cfg", ".yaml", ".yml"}

# Q:<sep>  ->  G:<sep>BaiduNetdiskDownload<sep>real<sep>
_DRIVE = re.compile(r"(?i)(?<![A-Za-z0-9])Q:([\\/])")
# bash-style /q/MC2UE5 or /q/UE
_BASHPATH = re.compile(r"(?i)(?<![A-Za-z0-9])/q/(?=MC2UE5|UE/|AutoSDK|WindowsKits|dotnet|UBA|assets)")


def rewrite(text):
    def drive(m):
        sep = m.group(1)
        return "G:" + sep + "BaiduNetdiskDownload" + sep + "real" + sep

    text = _DRIVE.sub(drive, text)
    text = _BASHPATH.sub("/g/BaiduNetdiskDownload/real/", text)
    return text


def walk():
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() not in EXTS:
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, REPO).replace("\\", "/")
            if rel in SKIP_FILES:
                continue
            yield path, rel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total_files = 0
    total_hits = 0
    for path, rel in walk():
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                src = fh.read()
        except (UnicodeDecodeError, OSError):
            continue
        n = len(_DRIVE.findall(src)) + len(_BASHPATH.findall(src))
        if not n:
            continue
        total_files += 1
        total_hits += n
        print("%-58s %3d" % (rel, n))
        if args.apply:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(rewrite(src))

    print()
    print("files=%-4d hits=%d  %s" % (total_files, total_hits,
                                      "APPLIED" if args.apply else "dry-run"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
