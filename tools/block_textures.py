# -*- coding: utf-8 -*-
r"""
block_textures.py -- the material slot table, keyed on the ORIGINAL Minecraft
block textures instead of 19 photographic families.

Why this exists
---------------
The family model threw away the thing that makes the save readable. Measured on
this campus (2026-10-30):

  * 260 distinct block names in the palette collapsed into 19 families, so
    ``cobblestone``, ``stone_bricks``, ``end_stone_bricks`` and
    ``smooth_stone`` all rendered as one flat grey photo (sigma 3.0 at the
    resolution the GPU actually displays);
  * the family textures were 512 px CC0 *photographs* tiled one-per-block, so a
    block -- 16-20 px on screen -- showed the photo's 16x16 mip, i.e. its local
    average. Measured per-block contrast sigma 11.6 vs 45.6 for the original
    art, edge energy 4.3 vs 18.8;
  * the manifest's per-face textures (top/side/bottom) were unused, so a grass
    block's side was flat green instead of dirt with a grass fringe.

``assets/material_manifest.json`` (315 entries) already resolves every block
name in the save to its exact top/side/bottom texture, and
``assets/textures/block/*.png`` holds the 695 original 16x16 pixel-art tiles.
So the faithful thing is also the cheap thing: 273 textures cover the whole
manifest, and pixel art authored at 16 px per block survives being displayed at
16-20 px per block, whereas a photograph never can.

The table is a pure function of the manifest -- NOT of the campus palette.
Deriving it from the palette would be circular (the palette cache is produced by
the mesher that needs the slot table), and a manifest-derived table is stable
across runs and machines, which the slot numbers written into
``out/terrain_pack/*.fam`` depend on.

The slot encoding (must not change without re-running everything)
-----------------------------------------------------------------
0                    air / no face (a face mask uses 0 as "empty")
1                    ``other`` -- the neutral fallback, generated, never from
                     the save. Kept at slot 1 so ``terrain_smooth.OTHER_SLOT``
                     stays meaningful for a column whose surface cannot be
                     resolved.
2..N                 one slot per texture, ordered by texture file name.

Ordering by file name (not by first use) makes the table reproducible across
processes and machines, which matters because the slot numbers are written into
``out/terrain_pack/*.fam`` and matched against ``slots.json`` on the editor side.

Biome tinting
-------------
Minecraft stores ``grass_block_top``, ``*_leaves``, ``grass`` and ``fern`` as
GREYSCALE and multiplies them by the biome colour map at render time. Measured
here: ``grass_block_top`` mean RGB (147,147,147), ``oak_leaves`` (97,97,97),
``birch_leaves`` (73,73,73). Sampling them raw turns every lawn and every canopy
on the campus grey -- a colour bug that no amount of lighting fixes. TINT below
carries the temperate/plains colours (grass 0x91BD59, foliage 0x77AB2F) and is
applied when the texture is emitted, not in the material, so the editor side
keeps the one proven 5-node graph.
"""

import json
import os
import re
import sys

import numpy as np
from PIL import Image

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(REPO, "assets", "material_manifest.json")
SRC_DIR = os.path.join(REPO, "assets", "textures", "block")
OUT_DIR = os.path.join(REPO, "out", "textures")
PALETTE_CACHE = os.path.join(REPO, "out", "materials", "voxelmat.npz")

#: Neutral slot. ``terrain_smooth.OTHER_SLOT`` is this, and a column that cannot
#: be resolved gets it rather than a bogus material.
FALLBACK = "other"

#: Biome tints, 1.16.5 temperate (plains). Multiplied into the emitted PNG.
#: These are Minecraft's own biome colour constants (grass 0x91BD59, foliage
#: 0x77AB2F), NOT values fitted to ``out/ref_map.png``: that map carries the
#: in-game MAP-ITEM palette, which is a flat per-material colour and cannot
#: reproduce a texture average (it paints white_concrete and white_wool the
#: same white). See ``docs/colour_truth.md`` for the measurement.
GRASS_TINT = (0x91, 0xBD, 0x59)
FOLIAGE_TINT = (0x77, 0xAB, 0x2F)
WATER_TINT = (0x3F, 0x76, 0xE4)

#: Texture stem -> tint. Explicit on purpose: "is this greyscale" is not a
#: reliable test (white_concrete is greyscale and must NOT be tinted), so the
#: list is the authority and the emitted report prints what it touched.
TINT = {
    "grass_block_top": GRASS_TINT,
    "grass": GRASS_TINT,
    "fern": GRASS_TINT,
    "tall_grass": GRASS_TINT,
    "oak_leaves": FOLIAGE_TINT,
    "birch_leaves": FOLIAGE_TINT,
    "jungle_leaves": FOLIAGE_TINT,
    "acacia_leaves": FOLIAGE_TINT,
    "dark_oak_leaves": FOLIAGE_TINT,
    "spruce_leaves": FOLIAGE_TINT,
    "vine": FOLIAGE_TINT,
    "lily_pad": FOLIAGE_TINT,
    "water_still": WATER_TINT,
    "water_flow": WATER_TINT,
}

#: The size every tile is emitted at. 16 is Minecraft's own resolution; mips
#: then run 16 -> 8 -> 4 -> 2 -> 1 exactly as the game's do, and NEAREST
#: sampling keeps the texels crisp at the 16-20 px a block occupies on screen.
OUT_PX = 16

#: Glass must be OPAQUE (``docs/glass_spec.md`` section 0.1): the campus buildings
#: are hollow shells (measured fill 20.6 % / 21.3 %), so any see-through pane
#: exposes an empty interior and reads as unfinished at campus scale. The spec
#: rejects ``BLEND_TRANSLUCENT`` for the same reason it rejects alpha cutouts --
#: no depth write, so hundreds of panes per building sort incorrectly.
#:
#: Vanilla tiles carry real alpha because in Minecraft you look through them:
#: ``glass`` 74.6 % fully transparent, ``glass_pane_top`` 87.5 %,
#: ``*_stained_glass_pane_top`` 87.5 %. That alpha is composited onto a dark base
#: and dropped to 255 HERE, so the importer derives BLEND_OPAQUE from the bytes and
#: no override table is needed.
#:
#: The base is ``GLASS_DARKEN * mean(visible colour)`` rather than one fixed grey,
#: so a red pane stays dark red instead of turning slate. For plain glass it lands
#: on (62, 75, 77), matching the (58, 68, 74) approved in glass_spec.md 2.1.
#:
#: ``iron_bars`` is deliberately NOT matched: glass_spec.md 2.2 wants it
#: alpha-cut (the geometry is a solid cube, so only a cutout reads as railing),
#: and its vanilla tile (54.7 % transparent) is already the rails.
GLASS_DARKEN = 0.35
GLASS_RE = re.compile(r"glass(?:_pane(?:_top)?)?$")

_TABLE = None


def stem(name):
    """'grass_block_top.png' -> 'grass_block_top'."""
    return name[:-4] if name.lower().endswith(".png") else name


def campus_palette(cache=PALETTE_CACHE):
    """-> {block_short_name: voxel_count} for the blocks the save actually uses.

    Read from the material cache rather than the save: the cache is the scan of
    the save that every mesher already depends on, so using it guarantees the
    slot table describes the same set of blocks the geometry will ask about.
    """
    if not os.path.isfile(cache):
        raise FileNotFoundError(
            "material cache missing (%s) -- run extract_structures.py once so "
            "the palette exists before building the slot table" % cache)
    z = np.load(cache, allow_pickle=False)
    names = [str(s).split(":")[-1] for s in z["names"]]
    nid = z["nameid"]
    counts = {}
    for slot, n in zip(*np.unique(nid, return_counts=True)):
        if int(slot) < len(names):
            counts[names[int(slot)]] = int(n)
    return counts


def _load_manifest(manifest=MANIFEST):
    with open(manifest, encoding="utf-8") as fh:
        entries = json.load(fh)
    return {e["blockName"].split(":")[-1]: e for e in entries}


def build_table(blocks=None, manifest=MANIFEST):
    """-> (slot_names, block_slots, report) with 1-based slots.

    ``blocks`` defaults to every block the manifest describes; pass a palette
    (e.g. ``campus_palette()``) to narrow it. ``slot_names[0]`` is
    ``FALLBACK`` and ``block_slots[block]`` is a ``(top, side, bottom)`` triple
    of 1-based slot numbers.
    """
    man = _load_manifest(manifest)
    if blocks is None:
        blocks = sorted(man)

    used = set()
    block_tex = {}
    unmapped = []
    for block in blocks:
        entry = man.get(block)
        if entry is None:
            unmapped.append(block)
            continue
        triple = []
        for key in ("topTex", "sideTex", "bottomTex"):
            tex = entry.get(key)
            if not tex:
                # water/lava-style entries only carry `particle`; fall back to
                # whichever face they do have rather than inventing a texture.
                tex = (entry.get("topTex") or entry.get("sideTex")
                       or entry.get("bottomTex"))
            if not tex:
                unmapped.append(block)
                break
            triple.append(stem(tex))
        else:
            block_tex[block] = tuple(triple)
            used.update(triple)

    slot_names = [FALLBACK] + sorted(t for t in used if t != FALLBACK)
    slot_of = {n: i + 1 for i, n in enumerate(slot_names)}
    block_slots = {b: tuple(slot_of[t] for t in tri)
                   for b, tri in block_tex.items()}

    report = {
        "palette_blocks": len(blocks),
        "mapped_blocks": len(block_slots),
        "unmapped_blocks": sorted(unmapped),
        "textures": len(slot_names),
        "tinted": sorted(t for t in used if t in TINT),
    }
    return slot_names, block_slots, report


def table():
    """Lazy module-level accessor so importers pay nothing until they ask."""
    global _TABLE
    if _TABLE is None:
        _TABLE = build_table()
    return _TABLE


def slot_names():
    return table()[0]


def slots_of(block_name):
    """-> (top, side, bottom) 1-based slots for a block name.

    An unknown block gets the neutral fallback for all three faces. That is a
    visible flat colour, not a crash and not a guess: the report from
    ``build_table`` lists exactly which names took this path.
    """
    _, block_slots, _ = table()
    n = block_name.split(":")[-1]
    return block_slots.get(n, (1, 1, 1))


def _emit_png(src_stem, out_dir):
    """Copy one 16x16 tile to the output dir, tinted / de-alpha'd as needed.

    Both decisions are applied to the asset rather than the material, which keeps
    the editor-side graph at the five nodes this project has proven compilable and
    keeps each decision measurable from the PNG:

    * **tint** -- ``TINT`` (biome colours) is multiplied in;
    * **glass opacity** -- ``GLASS_RE`` frames are composited onto a dark base and
      made opaque (see GLASS_DARKEN). Everything else keeps the alpha it shipped
      with, which is what makes ``iron_bars`` reach the editor as a cutout.
    """
    src = os.path.join(SRC_DIR, src_stem + ".png")
    if not os.path.isfile(src):
        return None, "png missing"
    im = Image.open(src)
    keep_alpha = im.mode in ("RGBA", "LA", "P")
    im = im.convert("RGBA" if keep_alpha else "RGB")
    if im.size != (OUT_PX, OUT_PX):
        # A handful of vanilla tiles are strips (water_still is 16x512). They
        # are not cube faces; take the first frame and say so.
        im = im.crop((0, 0, min(OUT_PX, im.width), min(OUT_PX, im.height)))
        if im.size != (OUT_PX, OUT_PX):
            im = im.resize((OUT_PX, OUT_PX), Image.NEAREST)
    arr = np.asarray(im).astype(np.float32)
    tint = TINT.get(src_stem)
    if tint:
        arr[..., :3] *= np.array(tint, np.float32) / 255.0
    if arr.shape[2] == 4 and GLASS_RE.search(src_stem):
        alpha = arr[..., 3:4] / 255.0
        visible = arr[..., :3][arr[..., 3] > 8]
        base = (visible.mean(axis=0) * GLASS_DARKEN if len(visible)
                else np.zeros(3, np.float32))
        arr[..., :3] = arr[..., :3] * alpha + base * (1.0 - alpha)
        arr[..., 3] = 255.0
    arr = np.rint(np.clip(arr, 0, 255)).astype(np.uint8)
    out = os.path.join(out_dir, src_stem + ".png")
    Image.fromarray(arr, "RGBA" if arr.shape[2] == 4 else "RGB").save(out)
    alpha_min = int(arr[..., 3].min()) if arr.shape[2] == 4 else 255
    return out, ("ok" if alpha_min == 255 else "alpha(min=%d)" % alpha_min)


def emit(out_dir=OUT_DIR):
    """Write every slot's PNG + slots.json. -> report dict."""
    names, block_slots, report = table()
    os.makedirs(out_dir, exist_ok=True)
    missing, masked = [], []
    for name in names:
        if name == FALLBACK:
            # The fallback is generated, not from the save: a neutral grey that
            # reads as "unresolved", never as a material the campus has.
            arr = np.full((OUT_PX, OUT_PX, 3), 150, np.uint8)
            Image.fromarray(arr, "RGB").save(
                os.path.join(out_dir, FALLBACK + ".png"))
            continue
        path, why = _emit_png(name, out_dir)
        if path is None:
            missing.append((name, why))
        elif why != "ok":
            masked.append((name, why))

    # Fail loudly rather than shipping a window that is 3/4 hole: the importer
    # derives BLEND_MASKED from the emitted alpha, so a glass stem that still
    # carries alpha means GLASS_RE stopped matching (a rename upstream), not
    # that the spec changed.
    still = [n for n, _ in masked if GLASS_RE.search(n)]
    if still:
        raise SystemExit(
            "block_textures: %d glass tile(s) still carry alpha after the "
            "opacity pass: %s -- docs/glass_spec.md requires opaque windows"
            % (len(still), ", ".join(still)))

    with open(os.path.join(out_dir, "slots.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "note": "1-based slot -> texture name; 0 is air. Ordered by "
                    "texture name so the table is reproducible.",
            "tile_px": OUT_PX,
            "fallback_slot": 1,
            "tint": {k: list(v) for k, v in TINT.items()},
            "slot_names": names,
            "blocks": {b: list(t) for b, t in sorted(block_slots.items())},
            "unmapped_blocks": report["unmapped_blocks"],
        }, fh, indent=1)

    report["missing"] = missing
    report["masked"] = masked
    # Campus coverage is a diagnostic, never a dependency: the table itself is
    # built from the manifest. If the cache is absent this simply does not run.
    try:
        pal = campus_palette()
    except Exception:
        pal = None
    if pal:
        report["campus_blocks"] = len(pal)
        report["campus_unmapped"] = sorted(b for b in pal if b not in block_slots)
    return report


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    out_dir = OUT_DIR
    for i, a in enumerate(argv):
        if a == "--out" and i + 1 < len(argv):
            out_dir = argv[i + 1]
    rep = emit(out_dir)
    print("slot table: %d slots (%d textures + fallback)"
          % (rep["textures"], rep["textures"] - 1))
    print("blocks: %d/%d manifest entries mapped"
          % (rep["mapped_blocks"], rep["palette_blocks"]))
    if "campus_blocks" in rep:
        print("campus: %d block names, %d unresolved -> slot 1"
              % (rep["campus_blocks"], len(rep["campus_unmapped"])))
        if rep["campus_unmapped"]:
            print("  %s" % rep["campus_unmapped"][:20])
    if rep["unmapped_blocks"]:
        print("  unmapped (-> slot 1): %s" % rep["unmapped_blocks"])
    print("tinted: %s" % rep["tinted"])
    if rep["masked"]:
        print("masked (alpha < 255, needs BLEND_MASKED): %d" % len(rep["masked"]))
        for n, why in rep["masked"]:
            print("   %-22s %s" % (n, why))
    if rep["missing"]:
        print("MISSING SOURCE PNGs: %s" % rep["missing"])
        return 1
    print("wrote %s + slots.json" % out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
