# -*- coding: utf-8 -*-
"""truth_colour_map.py -- the top-down colour of the save computed from the
ORIGINAL 16x16 block textures, one pixel per block column.

Why this exists
---------------
`out/ref_map.png` is the position ruler, but it cannot be the colour ruler:
its colours come from `reference_map.BLOCK_COLORS`, a table of in-game MAP-ITEM
colours plus dye colours, which collapses whole material families into one
value. Measured in `docs/colour_truth.md`: 315 block names -> 74 distinct
colours, and 15 of 18 dye-colour groups fold two or more different textures
into the same RGB (`white_concrete`'s real texture mean is (207,213,214),
ref_map records (249,255,254)).

So a pixel-level comparison of a UE orthographic screenshot against ref_map
measures the palette collapse, not the render. This tool builds the missing
ruler: each pixel is the arithmetic mean sRGB of the 16x16 vanilla tile that
the column's topmost solid block shows when looked at from above, i.e. the
`top` face slot of `block_textures.slots_of()`. Both inputs are offline and
external to the renderer -- the save and `assets/textures/block/*.png` -- so
the result is a ground truth, not a calibration.

Averaging rule
--------------
The mean is taken on the 8-bit sRGB values as stored in the emitted PNG (the
PNG itself already carries the biome tint and the opaque-glass composite from
`block_textures`), NOT on linear light: the thing being compared is an 8-bit
screenshot, so the comparison has to happen in the same space.

61 of the emitted tiles carry real alpha (leaves, plants, iron_bars, cutouts
in general). Their transparent texels are holes, not dark paint, and in-game
they show what is behind the block. Averaging them in as if they were colour
is exactly the "leaves read far darker than vanilla" bug, so texels are
weighted by alpha/255. Tiles that are fully opaque are unaffected -- the
weighted mean degenerates to the plain one.

Orientation matches `reference_map.build_map`: row = z (top row is z min),
column = x, 448 x 768, so the two images can be diffed without a transform.

Outputs (out/ is not tracked):
  truth_colour.png        448x768, top-face texture mean per column
  truth_colour_stats.txt  coverage, fallback audit, top-40 area table,
                          ref_map legend cross-check, the two "from a
                          distance" numbers (mean |dRGB| vs ref_map, and
                          adjacent-pixel grey detail for both maps)

    python tools/truth_colour_map.py
"""

import json
import os
import sys
from collections import Counter

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import block_textures                                            # noqa: E402
from reference_map import CAMPUS, MATERIAL_CACHE, top_block_per_column  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXTURE_DIR = os.path.join(REPO, "out", "textures")
OUT_DIR = os.path.join(REPO, "out")
TRUTH_PNG = os.path.join(OUT_DIR, "truth_colour.png")
REF_PNG = os.path.join(OUT_DIR, "ref_map.png")
LEGEND_JSON = os.path.join(OUT_DIR, "ref_map_legend.json")
STATS_TXT = os.path.join(OUT_DIR, "truth_colour_stats.txt")

#: All-air columns. None exist in this save, but the code must never let the
#: palette index 0 (bedrock... or any block) paint a column that has no block.
EMPTY_COLOR = (0, 0, 0)

#: Luma weights for the "detail" metric. Rec.601 on 8-bit values: the metric
#: only has to be applied identically to both maps, as a proxy for how much
#: local contrast survives at 1 px per block.
LUMA = np.array([0.299, 0.587, 0.114], dtype=np.float32)

TOP_N = 40


def tile_mean(path):
    """-> (rgb, alpha_weighted) for one 16x16 tile, mean on 8-bit sRGB.

    Alpha-weighted (see the module docstring): a cutout tile's holes carry no
    colour, so they must not pull the mean down.
    """
    arr = np.asarray(Image.open(path).convert("RGBA"), dtype=np.float64)
    rgb, alpha = arr[..., :3], arr[..., 3]
    if alpha.min() >= 255.0:
        return rgb.reshape(-1, 3).mean(axis=0), False
    w = alpha.reshape(-1) / 255.0
    total = w.sum()
    if total <= 0.0:
        # Fully transparent tile: no visible colour at all. Not reached by the
        # campus (tripwire is the only such tile and no column tops out on it),
        # but returning black silently would hide a broken asset.
        raise SystemExit("tile_mean: %s is fully transparent" % path)
    return (rgb.reshape(-1, 3) * w[:, None]).sum(axis=0) / total, True


def build_palette(names):
    """-> (palette_rgb (N,3) uint8, info list per name index).

    One entry per name index, resolved once: 344,064 columns then cost a single
    fancy-index and no image decode. `info[i]` records what the column would
    show, for the audit tables (block name, slot, slot name, alpha-weighted).
    """
    slot_names = block_textures.slot_names()
    cache = {}
    palette = np.zeros((len(names), 3), dtype=np.uint8)
    info = []
    for i, name in enumerate(names):
        top_slot, _side, _bottom = block_textures.slots_of(name)
        if top_slot <= 0 or top_slot > len(slot_names):
            raise SystemExit("block %r resolved to out-of-range top slot %d"
                             % (name, top_slot))
        tex = slot_names[top_slot - 1]
        if tex not in cache:
            path = os.path.join(TEXTURE_DIR, tex + ".png")
            if not os.path.isfile(path):
                raise SystemExit("missing top texture %s (block %r, slot %d) "
                                 "-- run tools/block_textures.py" %
                                 (path, name, top_slot))
            mean, weighted = tile_mean(path)
            cache[tex] = (tuple(int(v) for v in np.rint(mean)), weighted)
        rgb, weighted = cache[tex]
        palette[i] = rgb
        info.append({"block": name, "slot": top_slot, "tex": tex,
                     "rgb": rgb, "alpha_weighted": weighted})
    return palette, info


def grey(arr):
    """-> (H, W) float32 luma of an (H, W, 3) uint8 image."""
    return arr.astype(np.float32) @ LUMA


def detail_metric(gray_img):
    """-> (horizontal, vertical, mean) mean |neighbour grey difference|.

    The quantity a palette collapse destroys: ref_map paints whole families one
    flat colour, so neighbouring blocks differ only where the family changes,
    while the textured truth keeps per-material contrast.
    """
    h = float(np.abs(np.diff(gray_img, axis=1)).mean())
    v = float(np.abs(np.diff(gray_img, axis=0)).mean())
    return h, v, 0.5 * (h + v)


def load_names():
    z = np.load(os.path.join(REPO, MATERIAL_CACHE), allow_pickle=False)
    return [str(s) for s in z["names"]]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    names = load_names()
    top, has = top_block_per_column()
    W, D = top.shape

    # --- invariants that make the comparison meaningful -------------------
    exp_w, exp_d = CAMPUS[1] - CAMPUS[0] + 1, CAMPUS[3] - CAMPUS[2] + 1
    if (W, D) != (exp_w, exp_d):
        raise SystemExit("column grid %dx%d does not match CAMPUS %r (%dx%d)"
                         % (W, D, CAMPUS, exp_w, exp_d))

    palette, info = build_palette(names)
    # Work in image space from here on: row = z, column = x, exactly the
    # orientation reference_map.build_map emits, so no transform is needed
    # between the two images.
    top_img = top.T                                      # (D, W)
    has_img = has.T
    img = palette[top_img]                               # (D, W, 3)
    img = np.where(has_img[..., None], img, np.array(EMPTY_COLOR, np.uint8))
    if img.shape != (D, W, 3) or (D, W) != (768, 448):
        raise SystemExit("truth image is %r, expected (768, 448, 3)"
                         % (img.shape,))
    Image.fromarray(img.astype(np.uint8)).save(TRUTH_PNG)

    # --- per-column accounting -------------------------------------------
    total = int(W * D)
    with_block = int(has_img.sum())
    no_block = total - with_block
    per_block = Counter()
    per_tex = Counter()
    block_info = {}
    for y, x in zip(*np.nonzero(has_img)):
        idx = int(top_img[y, x])
        nm = names[idx]
        per_block[nm] += 1
        per_tex[info[idx]["tex"]] += 1
        block_info[nm] = info[idx]

    fallback_cols = sum(c for nm, c in per_block.items()
                        if block_info[nm]["slot"] == 1)
    fallback_blocks = sorted(nm for nm in per_block
                             if block_info[nm]["slot"] == 1)

    # --- cross-check against ref_map's own palette ------------------------
    ref_rgb = np.asarray(Image.open(REF_PNG).convert("RGB"))
    if ref_rgb.shape != (D, W, 3):
        raise SystemExit("ref_map.png is %r, expected %r -- rebuild it first"
                         % (ref_rgb.shape, (D, W, 3)))
    with open(LEGEND_JSON, encoding="utf-8") as fh:
        legend = json.load(fh)
    legend_blocks = {tuple(int(v) for v in e["rgb"]): set(e["block_names"])
                     for e in legend}

    agree = 0
    missing_colour = Counter()          # ref pixel colour absent from legend
    mismatch = Counter()                # (block, ref colour) -> columns
    ref_of_block = {}
    for y, x in zip(*np.nonzero(has_img)):
        colour = tuple(int(v) for v in ref_rgb[y, x])
        cands = legend_blocks.get(colour)
        nm = names[int(top_img[y, x])]
        ref_of_block.setdefault(nm, colour)
        if cands is None:
            missing_colour[colour] += 1
        elif nm in cands:
            agree += 1
        else:
            mismatch[(nm, colour)] += 1
    agree_rate = 100.0 * agree / with_block if with_block else 0.0

    # --- how much material identity the ref palette destroys --------------
    # The adjacent-pixel metric below cannot see this: at 1 px per block a
    # texture mean is a single number, so neither map carries intra-block
    # detail. What the collapse removes is the DISTINCTION between materials,
    # so compare every pair of blocks that use different textures and ask how
    # far apart the two maps put them.
    items = [(nm, block_info[nm]["tex"],
              float(np.dot(block_info[nm]["rgb"], LUMA)),
              float(np.dot(ref_of_block[nm], LUMA))) for nm in per_block]
    pairs = collapsed = closer = farther = 0
    gt = gr = 0.0
    collapsed_pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items[i][1] == items[j][1]:
                continue                     # same tile, distance is 0 by right
            dt = abs(items[i][2] - items[j][2])
            dr = abs(items[i][3] - items[j][3])
            pairs += 1
            gt += dt
            gr += dr
            if dr <= 1e-9:
                collapsed += 1
                collapsed_pairs.append((dt, items[i][0], items[j][0]))
            elif dr < dt:
                closer += 1
            else:
                farther += 1

    # --- the two "from a distance" numbers --------------------------------
    absdiff = np.abs(img.astype(np.int16) - ref_rgb.astype(np.int16))
    mean_abs_rgb = float(absdiff.mean())
    per_channel = absdiff.reshape(-1, 3).mean(axis=0)
    truth_detail = detail_metric(grey(img))
    ref_detail = detail_metric(grey(ref_rgb))

    # --- stats file -------------------------------------------------------
    ranked = sorted(per_block.items(), key=lambda t: (-t[1], t[0]))
    lines = []
    a = lines.append
    a("# MC2UE5 truth colour map -- top-face vanilla texture mean per column")
    a("# campus x[%d..%d] z[%d..%d], %d columns, row = z, column = x"
      % (CAMPUS[0], CAMPUS[1], CAMPUS[2], CAMPUS[3], total))
    a("# source: out/materials/voxelmat.npz top face + out/textures/<tile>.png")
    a("")
    a("## coverage")
    a("columns total            %d" % total)
    a("columns with solid block %d (%.4f%%)"
      % (with_block, 100.0 * with_block / total))
    a("columns with no block    %d" % no_block)
    a("distinct top blocks      %d" % len(per_block))
    a("distinct top textures    %d" % len(per_tex))
    a("distinct colours in map  %d" % len({tuple(c) for c in img.reshape(-1, 3)}))
    a("")
    a("## fallback slot (slot 1, `other`) -- must be 0 inside the campus")
    a("columns in fallback slot %d" % fallback_cols)
    if fallback_blocks:
        for nm in fallback_blocks:
            a("  %-40s %8d" % (nm, per_block[nm]))
    a("")
    a("## top %d blocks by column count" % TOP_N)
    a("%-40s %-26s %-15s %8s %8s" %
      ("block", "top texture", "mean rgb", "columns", "share"))
    for nm, c in ranked[:TOP_N]:
        i = block_info[nm]
        a("%-40s %-26s %-15s %8d %7.3f%%"
          % (nm, i["tex"], "(%3d,%3d,%3d)" % i["rgb"], c,
             100.0 * c / with_block))
    a("")
    a("## top textures by column count")
    for tex, c in sorted(per_tex.items(), key=lambda t: (-t[1], t[0])):
        a("  %-26s %8d %7.3f%%" % (tex, c, 100.0 * c / with_block))
    a("")
    a("## cross-check vs out/ref_map_legend.json")
    a("columns checked          %d" % with_block)
    a("consistent               %d (%.4f%%)" % (agree, agree_rate))
    a("ref colour not in legend %d" % sum(missing_colour.values()))
    a("block/legend mismatch    %d" % sum(mismatch.values()))
    for colour, c in missing_colour.most_common():
        a("  ref rgb %s absent from legend, %d columns" % (colour, c))
    for (nm, colour), c in sorted(mismatch.items(), key=lambda t: (-t[1], t[0])):
        a("  %-40s ref rgb %-15s %8d columns"
          % (nm, "(%3d,%3d,%3d)" % colour, c))
    a("")
    a("## distance metrics")
    a("mean |dRGB| truth vs ref_map      %.2f  (%s)"
      % (mean_abs_rgb, "the palette collapse, not an error"))
    a("  per channel R/G/B               %.2f / %.2f / %.2f"
      % tuple(per_channel))
    a("adjacent-pixel grey |d| truth     %.3f  (h %.3f, v %.3f)"
      % (truth_detail[2], truth_detail[0], truth_detail[1]))
    a("adjacent-pixel grey |d| ref_map   %.3f  (h %.3f, v %.3f)"
      % (ref_detail[2], ref_detail[0], ref_detail[1]))
    a("  -> ratio ref/truth              %.1f%%"
      % (100.0 * ref_detail[2] / truth_detail[2] if truth_detail[2] else 0.0))
    a("  note: at 1 px per block this is block-to-block contrast, NOT texture")
    a("        detail -- both maps hold one number per block. ref_map scores")
    a("        higher because map colours are more saturated than texture")
    a("        means, so material borders jump further; that is exaggeration")
    a("        in a few places, not preserved detail.")
    a("material-pair separation (blocks using different top textures):")
    a("  pairs compared                  %d" % pairs)
    a("  mean |dgrey| truth              %.3f" % (gt / pairs if pairs else 0.0))
    a("  mean |dgrey| ref_map            %.3f" % (gr / pairs if pairs else 0.0))
    a("  pairs ref cannot tell apart     %d (%.1f%% of pairs, identical colour)"
      % (collapsed, 100.0 * collapsed / pairs if pairs else 0.0))
    a("  pairs ref under-separates       %d" % closer)
    a("  pairs ref over-separates        %d" % farther)
    if collapsed_pairs:
        a("  (block pair, truth |dgrey|) -- these are different textures that")
        a("  ref_map paints one colour, i.e. the ruler cannot distinguish them:")
        for dt, na, nb in sorted(collapsed_pairs, reverse=True)[:20]:
            a("    %-34s %-34s %6.1f" % (na, nb, dt))
    alpha_tiles = sorted({i["tex"] for i in block_info.values()
                          if i["alpha_weighted"]})
    a("alpha-weighted tiles in use       %d%s"
      % (len(alpha_tiles),
         (": " + ", ".join(alpha_tiles)) if alpha_tiles else ""))

    with open(STATS_TXT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print("\n".join(lines[:24]))
    print("...")
    print("mean |dRGB| vs ref_map %.2f | detail truth %.3f vs ref %.3f"
          % (mean_abs_rgb, truth_detail[2], ref_detail[2]))
    print("wrote %s\n      %s" % (TRUTH_PNG, STATS_TXT))

    # A non-100% cross-check means the two maps disagree about which block owns
    # a column, i.e. the palette construction is broken -- a real failure, not
    # a number to report and move on from.
    if agree_rate < 100.0:
        print("FAIL: ref_map legend agrees on only %.4f%% of columns"
              % agree_rate, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
