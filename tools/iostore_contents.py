# -*- coding: utf-8 -*-
"""
iostore_contents.py -- list the files inside an IoStore .utoc container.

Why this exists. UnrealPak's `-list` on the companion `.pak` reports 1,732
files, **none** of them project content: with IoStore enabled the cooked
`.uasset` payloads live in the `.ucas`, and the pak holds only Engine content,
config and the asset registry. So the obvious check -- "grep the pak listing for
MC/Terrain" -- returns zero and reads exactly like the historical failure where
assets were built but never cooked.

Both answers are wrong in opposite directions, and neither is the question.
"Zero entries in the pak" does not mean the content is missing, and it does not
mean it is present. The container's own file table is the authority, so this
reads it and counts by directory.

Pure stdlib: the .utoc is a documented container format (TocHeader followed by
one TOC entry per file), and parsing it beats trusting either of the other two
signals.

    python3 tools/iostore_contents.py <utoc> [ucas]
"""

import collections
import os
import struct
import sys

# UE5 container header. The first entry ("UnrealPak/Cooked..." style) is the
# container signature; we skip it and print the rest, so no version guessing is
# needed beyond the field order, which has been stable since UE5.0.
TOC_ENTRY = "<qQQiI"      # offset, size, compressed_size, entry_offset, pad
TOC_HEADER_LEN = 4 * 8 + 4 + 4 + 4 + 4 + 4 + 4 + 4   # up to and incl. name


def read_toc(path):
    """-> list of (offset, size, compressed, name)."""
    data = open(path, "rb").read()
    if len(data) < 512:
        raise ValueError("%s is too small to be a .utoc (%d bytes)"
                         % (path, len(data)))
    # TOC starts after the container header. Its offset is stored in the header;
    # read it rather than assuming a constant.
    toc_offset = struct.unpack_from("<Q", data, 0x10)[0]
    count = struct.unpack_from("<I", data, toc_offset + 0x30)[0]
    pos = toc_offset + 0x38
    out = []
    for _ in range(count):
        try:
            off, size, csize, _eoff, _pad = struct.unpack_from(TOC_ENTRY, data, pos)
        except struct.error:
            break
        name_start = pos + struct.calcsize(TOC_ENTRY)
        end = data.index(b"\x00", name_start)
        name = data[name_start:end].decode("utf-8", "replace")
        out.append((off, size, csize, name))
        pos = end + 1
        while pos < len(data) and data[pos:pos + 1] == b"\x00":
            pos += 1
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    utoc = sys.argv[1]
    if not os.path.isfile(utoc):
        print("FATAL: %s not found" % utoc)
        return 2

    try:
        toc = read_toc(utoc)
    except Exception as exc:
        print("could not parse %s: %s" % (utoc, exc))
        return 1

    names = [t[3] for t in toc]
    print("container: %s" % os.path.basename(utoc))
    print("  size on disk: %.1f MB" % (os.path.getsize(utoc) / 1048576.0))
    print("  TOC entries:  %d" % len(toc))

    total = sum(t[1] for t in toc)
    print("  uncompressed payload: %.1f MB" % (total / 1048576.0))

    groups = collections.Counter()
    for n in names:
        parts = n.split("/")
        key = "/".join(parts[:3]) if len(parts) >= 3 else n
        groups[key] += 1
    print("")
    print("--- by directory ---")
    for k, v in groups.most_common(20):
        print("  %-46s %d" % (k, v))

    print("")
    print("--- MC content by subdir ---")
    for sub in ("Terrain", "Families", "Structures", "Maps", "Materials",
                "Props", "Character", "Atlas"):
        n = len([x for x in names
                 if x.startswith("MCReplica/Content/MC/%s/" % sub)])
        print("  MC/%-12s %d" % (sub, n))

    uasset = [x for x in names if x.endswith(".uasset")]
    umap = [x for x in names if x.endswith(".umap")]
    print("")
    print(".uasset entries: %d" % len(uasset))
    print(".umap   entries: %d" % len(umap))
    for n in umap[:5]:
        print("  MAP %s" % n)

    # The specific check that has historically failed: are the colour variants
    # actually present, or were they built and never cooked?
    print("")
    print("--- colour variants in container ---")
    for v in ("brick_pink", "fabric_lime", "fabric_green", "fabric_white",
              "glass_white", "bars"):
        hits = [x for x in names if v in x]
        print("  %-14s %d" % (v, len(hits)))
    return 0


if __name__ == "__main__":
    sys.exit(main())