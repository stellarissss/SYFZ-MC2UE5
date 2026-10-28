# -*- coding: utf-8 -*-
"""
build_vegetation_pack.py -- emit the vegetation layer (S4.5) as OBJ chunks.

Why this exists
----------------
REFACTOR_PLAN §8.10: the campus had **0 trees** in the new pipeline because
``extract_structures.py`` only meshes blocks belonging to the 88 building
labels, and all 47,671 vegetation blocks live outside them. The voxel layer
that carried them was then *hidden* (commit 336948e) before S4.5 existed, so
the campus renders bare -- a direct violation of the plan's own precondition
("必须先补植被层，否则关掉旧层 = 校园所有树消失").

This is the data half of S4.5. It meshes every VEGETATION-role voxel in the
campus with the **same mesher, the same OBJ writer, the same family slots and
the same block-unit UVs** as the 158 structure meshes, so the trees come out
voxel-faithful (blocky form from the save; M_MC_bark / M_MC_leaves PBR
materials, leaves already masked per §8.28) -- exactly what RENDER_PLAN.md §1
defines as "形态忠实于 MC 地图 + 材质写实".

Why OBJ and not a MeshDescription pack: vegetation is voxel-shell geometry,
the one class the Interchange OBJ importer lands at **100%** (measured: a
10,924-face shell imported 10,924/10,924, while heightfields lose up to
99.2%; see the table in tools/build_terrain_meshes.py). Going through the
importer also inherits its basis transform (x, y, z) -> (x, -y, z) -- the
same convention the structures already live in, so no second coordinate
convention is born (a mirror split between layers is exactly the class of
bug that hid the new pipeline for weeks; see REFACTOR_PLAN §8.43).

Output, under out/vegetation_obj/:

    veg_x0000.obj ...      one OBJ per X strip (greedy quads, block UVs,
                           per-family usemtl, vn per vertex)
    manifest.json          per-chunk stats + totals + the run's parameters

Run (host python with numpy, not the editor):

    python tools/build_vegetation_pack.py \
        --bin voxel_data/full/overworld.bin \
        --classify out/classify --out out/vegetation_obj
"""

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_structures as es                       # noqa: E402
from extract_structures import (                       # noqa: E402
    CAMPUS, MAX_Y, SLOT_NAMES, build_material_volume)
from classify import VEGETATION                        # noqa: E402

#: Vegetation lands in the same asset folder the level census already knows
#: how to count; the SM_ prefix matches the terrain naming convention.
MESH_DIR = "/Game/MC/Vegetation"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default="voxel_data/full/overworld.bin")
    ap.add_argument("--classify", default="out/classify")
    ap.add_argument("--out", default="out/vegetation_obj")
    ap.add_argument("--material-cache",
                    default="out/materials/voxelmat.npz")
    ap.add_argument("--strip", type=int, default=64,
                    help="blocks per chunk (X strips). 448 blocks -> 7 chunks")
    ap.add_argument("--max-quad", type=int, default=4,
                    help="same default as the structure extraction")
    args = ap.parse_args()

    t0 = time.time()
    os.makedirs(args.out, exist_ok=True)

    role_path = os.path.join(args.classify, "rolevolume.npy")
    if not os.path.isfile(role_path):
        print("missing %s -- run tools/classify.py first" % role_path)
        return 1
    role = np.load(role_path)
    fam, _nid, _names = build_material_volume(args.bin, args.material_cache)
    if role.shape != fam.shape:
        print("role %s != fam %s -- both must be the campus grid"
              % (role.shape, fam.shape))
        return 1

    veg = (role == VEGETATION)
    occupied = (role != 0)
    n_veg = int(veg.sum())
    if n_veg == 0:
        print("no vegetation-role voxels -- nothing to pack")
        return 1

    per_family = {}
    vals, counts = np.unique(fam[veg], return_counts=True)
    for v, c in zip(vals.tolist(), counts.tolist()):
        if 1 <= v <= len(SLOT_NAMES):
            per_family[SLOT_NAMES[v - 1]] = int(c)

    # Block-unit UVs, like the structures' --uv-mode block: the per-family
    # tiling M_MC_* samplers need one repeat per block, not an atlas cell.
    es.UV_MODE = "block"

    W = role.shape[0]
    chunks = []
    tot_t = tot_q = 0
    for x_lo in range(0, W, args.strip):
        x_hi = min(W, x_lo + args.strip)
        m = np.zeros_like(veg)
        m[x_lo:x_hi] = veg[x_lo:x_hi]
        if not m.any():
            continue
        name = "veg_x%04d" % (CAMPUS[0] + x_lo)
        st = es.emit(m, fam, occupied, None, args.max_quad,
                     args.out, name, [
                         "S4.5 vegetation chunk %s" % name,
                         "strip x[%d, %d) of %d, max_quad %d"
                         % (x_lo, x_hi, W, args.max_quad),
                         "mesher/writer: extract_structures (structure chain)",
                     ])
        if st is None:
            continue
        st["name"] = name
        st["obj_path"] = os.path.join(args.out, name + ".obj")
        st["mesh_path"] = "%s/SM_%s" % (MESH_DIR, name)
        chunks.append(st)
        tot_t += st["triangles"]
        tot_q += st["quads"]

    manifest = {
        "_doc": ("S4.5 vegetation: VEGETATION-role voxels meshed by "
                 "extract_structures.emit with block UVs. Import with "
                 "project/Content/Python/build_vegetation_meshes.py."),
        "source": {"bin": args.bin, "classify": args.classify,
                   "material_cache": args.material_cache,
                   "role": "VEGETATION", "max_quad": args.max_quad,
                   "strip": args.strip},
        "slot_names": list(SLOT_NAMES),
        "block_cm": 100.0,
        "vegetation_blocks": n_veg,
        "blocks_by_family": per_family,
        "chunks": chunks,
        "totals": {"chunks": len(chunks), "blocks": n_veg,
                   "quads": tot_q, "triangles": tot_t,
                   "elapsed_s": round(time.time() - t0, 1)},
    }
    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    print("wrote %d chunks -> %s" % (len(chunks), args.out))
    print("  %s quads / %s triangles (%d vegetation blocks, %.1fs)"
          % (format(tot_q, ","), format(tot_t, ","), n_veg,
             time.time() - t0))
    for fam_name, n in sorted(per_family.items(), key=lambda kv: -kv[1]):
        print("  %-12s %8d blocks" % (fam_name, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
