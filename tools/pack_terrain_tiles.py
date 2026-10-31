# -*- coding: utf-8 -*-
"""
pack_terrain_tiles.py -- turn the height field into a compact binary pack for
the editor side to build meshes from.

Why a pack instead of an OBJ. The editor's embedded Python has no numpy
("ModuleNotFoundError: No module named 'numpy'"), so the height field has to be
evaluated over here. And the OBJ route is out anyway: the Interchange OBJ
translator drops 99.2% of a 128x128 height field's polygons (measured; see
build_terrain_meshes.py for the full table). So this writes the mesh as plain
arrays the editor can read with the standard library alone -- no numpy on that
side, no OBJ parsing, no importer.

Per tile, under out/terrain_pack/:

    <name>.pos   float32  (nv, 3)  vertex positions, ALREADY in Unreal cm
    <name>.uv    float32  (nv, 2)  vertex UVs
    <name>.tri   int32    (nt, 3)  triangle -> vertex indices, 0-based
    <name>.fam   uint16   (nt,)    1-based texture slot per triangle
    manifest.json                  tiles, family names, material paths

Positions are written the way the mesh builder needs them, which is the way the
OBJ importer would have delivered them: the writer's own (x, y, z) with Y
negated, and V flipped, matching UInterchangeOBJTranslator's PositionToUEBasis
and UVToUEBasis. Doing the basis change here rather than in the editor keeps the
editor side a dumb array reader.

Run with the project venv:

    G:/BaiduNetdiskDownload/real/venv/Scripts/python.exe tools/pack_terrain_tiles.py
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_structures import SLOT_NAMES                      # noqa: E402
from terrain_smooth import (CAMPUS, BLOCK_CM, fill_invalid,     # noqa: E402
                            smooth_heightfield, surface_family_map)
from extract_structures import build_material_volume            # noqa: E402

PACK_DIR = "out/terrain_pack"
MAT_FMT = "/Game/MC/Textures/M_MC_%s"
MESH_DIR = "/Game/MC/Terrain"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terrain", default="out/classify/terrain.npy")
    ap.add_argument("--bin", default="voxel_data/full/overworld.bin")
    ap.add_argument("--material-cache", default="out/materials/voxelmat.npz")
    ap.add_argument("--sigma", type=float, default=0.9)
    ap.add_argument("--flat-restore", type=float, default=0.85)
    ap.add_argument("--tile", type=int, default=128)
    ap.add_argument("--out", default=PACK_DIR)
    args = ap.parse_args()

    t0 = time.time()
    os.makedirs(args.out, exist_ok=True)

    h = np.load(args.terrain)
    h = fill_invalid(h)
    before = h.astype(np.float32)

    print("loading material volume for surface families ...")
    # Top faces only: the terrain is a single-layer height field, so the slot
    # that matters is the one the block shows upwards (grass on a lawn, quartz
    # on a podium) -- not its side or bottom.
    fam_vol = build_material_volume(args.bin, args.material_cache)[0]
    fam_map = surface_family_map(h, fam_vol)

    sm, shift = smooth_heightfield(before, args.sigma, args.flat_restore)
    sm = sm.astype(np.float32)
    print("smoothing: mean |dh| = %.3f m" % shift)
    print("surface textures: %d distinct; unresolved = %d columns"
          % (int(np.unique(fam_map).size), int((fam_map == 1).sum())))

    nu, nv = sm.shape
    x0, z0 = CAMPUS[0], CAMPUS[2]

    # Gradients over the GLOBAL height field, computed once. Per-tile
    # np.gradient would see only one side of a tile boundary, so the two
    # copies of a seam vertex would shade differently; global gradients also
    # serve the seam vertices added below (audit F1).
    sm64 = sm.astype(np.float64)
    g_dzdx = np.gradient(sm64, axis=0) / float(BLOCK_CM)
    g_dzdy = np.gradient(sm64, axis=1) / float(BLOCK_CM)

    def vertex_aux(us, vs):
        """Shading normal + U-tangent for vertices at global grid coords."""
        nx, ny = -g_dzdx[us, vs], g_dzdy[us, vs]
        nz = np.ones_like(nx)
        ln = np.sqrt(nx * nx + ny * ny + nz * nz)
        ln[ln == 0.0] = 1.0
        n = np.stack([nx / ln, ny / ln, nz / ln], axis=1).astype(np.float32)
        tz = g_dzdx[us, vs]
        tl = np.sqrt(1.0 + tz * tz)
        t = np.stack([np.full_like(nx, 1.0) / tl,
                      np.zeros_like(nx), tz / tl], axis=1).astype(np.float32)
        return n, t

    def make_verts(us, vs):
        """Vertex rows (pos/uv/nrm/tan) at global grid coords, Unreal cm."""
        pos = np.empty((us.size, 3), dtype=np.float32)
        pos[:, 0] = (x0 + us) * BLOCK_CM                    # UE X
        pos[:, 1] = -(z0 + vs) * BLOCK_CM                   # UE Y (negated)
        pos[:, 2] = sm[us, vs] * BLOCK_CM                   # UE Z (up)
        uv = np.empty((us.size, 2), dtype=np.float32)
        uv[:, 0] = us - tu                                  # U, block tiling
        uv[:, 1] = 1.0 - (vs - tv)                          # V, flipped
        nrm, tan = vertex_aux(us, vs)
        return pos, uv, nrm, tan

    tiles = []
    tot_v = tot_t = 0
    for tu in range(0, nu, args.tile):
        for tv in range(0, nv, args.tile):
            bu, bv = min(tu + args.tile, nu), min(tv + args.tile, nv)
            if bu - tu < 2 or bv - tv < 2:
                continue
            name = "terrain_%04d_%04d" % (x0 + tu, z0 + tv)
            w, d = bu - tu, bv - tv
            fam = fam_map[tu:bu, tv:bv]

            # Base grid, row-major (i outer, j inner) -- the OBJ writer's order.
            us = np.arange(tu, bu).repeat(d)
            vs = np.tile(np.arange(tv, bv), w)
            pos, uv, nrm, tan = make_verts(us, vs)

            # Triangles per cell: (a,b,c) and (a,c,d) with a=(i,j),
            # b=(i+1,j), c=(i+1,j+1), d=(i,j+1) -- the OBJ writer's winding.
            gi, gj = np.meshgrid(np.arange(w - 1), np.arange(d - 1),
                                 indexing="ij")
            a = (gi * d + gj).ravel()
            b = ((gi + 1) * d + gj).ravel()
            c = ((gi + 1) * d + (gj + 1)).ravel()
            dd = (gi * d + (gj + 1)).ravel()
            tri = np.empty((2 * a.size, 3), dtype=np.int32)
            tri[0::2, 0], tri[0::2, 1], tri[0::2, 2] = a, b, c
            tri[1::2, 0], tri[1::2, 1], tri[1::2, 2] = a, c, dd
            facefam = np.repeat(fam[:w - 1, :d - 1].ravel(), 2).astype(np.uint16)

            # ---- seam strips (audit F1) ---------------------------------
            # facefam drops every tile's last cell row/column, so between two
            # neighbouring tiles a one-cell strip never became triangles --
            # 18 X seams + 20 Z seams of 100 cm slits across the campus. The
            # fix: a tile OWNS the seam on its max side. It emits the strip
            # cells (i=w-1) and (j=d-1), borrowing the neighbour's first
            # vertex row/column as extra vertices; the last tile in each
            # dimension has no neighbour and correctly emits nothing.
            # Local vertex layout: base 0..w*d-1, then (only when the
            # neighbour exists) the X-seam column (d), the Z-seam row (w),
            # and the corner vertex (1). Offsets are conditional -- a tile
            # without an X neighbour must not reserve the X block.
            quads = []
            sx, sz = bu < nu, bv < nv
            if sx:
                pos2, uv2, nrm2, tan2 = make_verts(
                    np.full(d, bu), np.arange(tv, bv))
                pos = np.concatenate([pos, pos2])
                uv = np.concatenate([uv, uv2])
                nrm = np.concatenate([nrm, nrm2])
                tan = np.concatenate([tan, tan2])
            if sz:
                pos2, uv2, nrm2, tan2 = make_verts(
                    np.arange(tu, bu), np.full(w, bv))
                pos = np.concatenate([pos, pos2])
                uv = np.concatenate([uv, uv2])
                nrm = np.concatenate([nrm, nrm2])
                tan = np.concatenate([tan, tan2])
            if sx and sz:
                pos2, uv2, nrm2, tan2 = make_verts(
                    np.array([bu]), np.array([bv]))
                pos = np.concatenate([pos, pos2])
                uv = np.concatenate([uv, uv2])
                nrm = np.concatenate([nrm, nrm2])
                tan = np.concatenate([tan, tan2])
            xv0 = w * d                          # first X-seam vertex (k = row j)
            zv0 = xv0 + (d if sx else 0)         # first Z-seam vertex (k = col i)
            cv0 = zv0 + (w if sz else 0)         # corner vertex
            if sx:
                for j in range(d - 1):
                    quads.append(((w - 1) * d + j, xv0 + j, xv0 + j + 1,
                                  (w - 1) * d + j + 1,
                                  fam[w - 1, j]))
            if sz:
                for i in range(w - 1):
                    quads.append((i * d + (d - 1), (i + 1) * d + (d - 1),
                                  zv0 + i + 1, zv0 + i,
                                  fam[i, d - 1]))
            if sx and sz:
                quads.append((w * d - 1, xv0 + (d - 1),
                              cv0, zv0 + (w - 1),
                              fam[w - 1, d - 1]))
            if quads:
                # Cell (a,b,c,dd) -> triangles (a,b,c) and (a,c,dd), matching
                # the base grid's tri[0::2]/tri[1::2] convention above.
                q = np.array([(a_, b_, c_) for (a_, b_, c_, _dd, _f) in quads]
                             + [(a_, c_, _dd)
                                for (a_, b_, c_, _dd, _f) in quads],
                             dtype=np.int32)
                qfam = np.array([_f for (_a, _b, _c, _dd, _f) in quads]
                                + [_f for (_a, _b, _c, _dd, _f) in quads],
                                dtype=np.uint16)
                tri = np.concatenate([tri, q])
                facefam = np.concatenate([facefam, qfam])

            keep = facefam > 0
            if not keep.any():
                print("  %-22s skipped (no family)" % name)
                continue
            tri, facefam = tri[keep], facefam[keep]

            for suffix, arr in (("pos", pos), ("uv", uv), ("tri", tri),
                                ("fam", facefam), ("nrm", nrm), ("tan", tan)):
                arr.tofile(os.path.join(args.out, "%s.%s" % (name, suffix)))

            fams = sorted(int(s) for s in np.unique(facefam))
            tiles.append({
                "name": name,
                "mesh_path": "%s/%s" % (MESH_DIR, name),
                "vertex_count": int(pos.shape[0]),
                "triangle_count": int(tri.shape[0]),
                # `.fam` holds the ABSOLUTE SLOT_NAMES slot (1-based), while
                # `slots` lists only the families this tile actually uses. The
                # two are paired by position so the builder can map one to the
                # other without re-deriving the family table.
                "slot_numbers": fams,
                "slots": [SLOT_NAMES[s - 1] for s in fams],
                "materials": [MAT_FMT % SLOT_NAMES[s - 1] for s in fams],
                "origin_block": [x0 + tu, z0 + tv],
                "size_blocks": [w, d],
            })
            tot_v += pos.shape[0]
            tot_t += tri.shape[0]

    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump({
            "campus": list(CAMPUS),
            "coordinate_note":
                "positions are already in Unreal cm, with the OBJ importer's "
                "basis change applied: OBJ (x,y,z) -> UE (x,-y,z), UV (u,v) -> "
                "(u,1-v). Building from this pack therefore produces the same "
                "surface the OBJ path would have, without the importer.",
            "why_pack":
                "the editor's embedded Python has no numpy and the Interchange "
                "OBJ translator drops 99.2% of a 128x128 height field",
            "tile_px": args.tile,
            "source": {"terrain": args.terrain, "bin": args.bin,
                       "sigma": args.sigma, "flat_restore": args.flat_restore},
            "totals": {"tiles": len(tiles), "vertices": tot_v,
                       "triangles": tot_t},
            "tiles": tiles,
        }, fh, indent=2)

    print("wrote %d tiles -> %s" % (len(tiles), args.out))
    print("  %s vertices, %s triangles  (%.1fs)"
          % (format(tot_v, ","), format(tot_t, ","), time.time() - t0))
    print("  128x128 tile triangle check: %s"
          % sorted({t["triangle_count"] for t in tiles
                    if t["size_blocks"] == [128, 128]}))


if __name__ == "__main__":
    main()
