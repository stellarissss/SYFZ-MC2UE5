# -*- coding: utf-8 -*-
"""
Vegetation OBJ emission (S4.5) regression tests, on synthetic volumes.

tools/build_vegetation_pack.py meshes the VEGETATION-role voxels by calling
``extract_structures.emit`` -- the same mesher/writer the 158 structure
meshes went through -- with block UVs, in X strips. Everything the emission
promises is checkable here by parsing the OBJ text: face culling against the
campus occupancy, flat per-quad normals, front-face winding in OBJ space,
block-unit UVs for the tiling M_MC_* samplers, per-family ``usemtl`` groups
in first-seen order, and OBJ-space coordinates
(X=(mc_x+CAMPUS.x0)*100, Y=(mc_z+CAMPUS.z0)*100, Z=mc_y*100 -- positive Y;
the importer's (x,-y,z) basis flip is what puts it into the level's world).

These are the invariants that made the structure meshes render correctly;
the vegetation layer must hold the same ones, because it reuses the exact
same emission path.
"""

import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import extract_structures as es                     # noqa: E402
from extract_structures import CAMPUS, SLOT_NAMES   # noqa: E402

LEAVES = SLOT_NAMES.index("leaves") + 1
BARK = SLOT_NAMES.index("bark") + 1
X0 = CAMPUS[0]
Z0 = CAMPUS[2]

FAILURES = []


def check(cond, name, detail=""):
    print("  %-6s %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  " + str(detail)) if detail else ""))
    if not cond:
        FAILURES.append(name)


def parse_obj(path):
    """-> (verts, uvs, normals, faces) with faces as [(a,b,c)] 0-based index
    triples plus (usemtl name at that face)."""
    verts, uvs, norms, faces = [], [], [], []
    mtl = None
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                p = [float(x) for x in line.split()[1:4]]
                verts.append(p)
            elif line.startswith("vt "):
                p = [float(x) for x in line.split()[1:3]]
                uvs.append(p)
            elif line.startswith("vn "):
                p = [float(x) for x in line.split()[1:4]]
                norms.append(p)
            elif line.startswith("f "):
                idx = []
                for tok in line.split()[1:4]:
                    v_i, _t_i, n_i = tok.split("/")
                    idx.append((int(v_i) - 1, int(n_i) - 1))
                faces.append((idx, mtl))
            elif line.startswith("usemtl "):
                mtl = line.split()[1].strip()
    return verts, uvs, norms, faces


def main():
    print("=" * 70)
    print("vegetation OBJ emission (synthetic)")
    print("=" * 70)

    # 3x3x3 leaf cube in the middle of a 5x5x5 volume, one bark block under
    # its centre. occupied = everything solid, so the cube's bottom-face
    # centre cell must be culled against the bark, exactly like a real trunk.
    shape = (5, 5, 5)
    mask = np.zeros(shape, dtype=bool)
    fam = np.zeros(shape, dtype=np.uint8)
    mask[1:4, 1:4, 1:4] = True
    fam[1:4, 1:4, 1:4] = LEAVES
    mask[2, 0, 2] = True
    fam[2, 0, 2] = BARK
    occupied = mask.copy()

    es.UV_MODE = "block"
    out_dir = tempfile.mkdtemp(prefix="mc2ue5_vegtest_")
    st = es.emit(mask, fam, occupied, None, 4, out_dir, "vegtest", [
        "synthetic vegetation test"])

    check(st is not None, "emit produced a chunk")
    if st is None:
        print("FAILED: no chunk")
        return 1

    verts, uvs, norms, faces = parse_obj(os.path.join(out_dir,
                                                      "vegtest.obj"))

    # Geometry accounting, verified by hand against this synthetic scene
    # (a 3x3x3 leaves cube + 1 bark block below its centre):
    #   * leaf cube: 4 sides (3x3 -> 1 merged quad each) + top (1) + bottom
    #     3x3 with the centre cell culled by the bark -> 8 cells greedy-split
    #     into 1x3 + 1x1 + 1x1 + 1x3 = 4 quads => 9 leaf quads.
    #   * bark block: 4 sides + top culled against the leaf cube + bottom at
    #     the VOLUME EDGE (mesh_volume only emits faces between two in-array
    #     cells; the real campus has ground below every trunk, so this never
    #     fires there) => 4 bark quads.
    want_quads = 13
    check(len(faces) == want_quads * 2, "face count",
          "%d (want %d)" % (len(faces), want_quads * 2))
    check(len(verts) == want_quads * 4, "vertex count", len(verts))
    check(st["triangles"] == len(faces), "manifest agrees with the file")
    check(st["material_slots"] == sorted(["bark", "leaves"]),
          "manifest material slots", st["material_slots"])

    # -- flat normals + front-face winding (OBJ space, right-handed) --------
    ok_flat = ok_wind = True
    for (idx, _m) in faces:
        nvals = [norms[ni] for (_vi, ni) in idx]
        if not (np.allclose(nvals[0], nvals[1])
                and np.allclose(nvals[0], nvals[2])):
            ok_flat = False
        a, b, c = (np.array(verts[vi]) for (vi, _ni) in idx)
        n = np.array(nvals[0])
        if float(np.dot(np.cross(b - a, c - a), n)) <= 0.0:
            ok_wind = False
    check(ok_flat, "every face's corners share one flat normal")
    check(ok_wind, "winding: cross(ab, ac) . normal > 0 on every face")

    # -- coordinates ----------------------------------------------------------
    # OBJ space: X = (mc_x + X0)*100, Y = (mc_z + Z0)*100 (positive -- the
    # importer applies the (x,-y,z) flip), Z = mc_y*100.
    ok_z = all(0.0 - 1e-6 <= v[2] <= 400.0 + 1e-6 for v in verts)
    check(ok_z, "heights within [0, 400] (cube tops out at mc y=4)")
    top = [v for v in verts if abs(v[2] - 400.0) < 1e-6]
    check(len(top) >= 4, "vertices at the top plane exist", len(top))
    up_faces = [(i, n) for (i, (idx, _m)) in enumerate(faces)
                for n in [norms[idx[0][1]]]
                if abs(n[2] - 1.0) < 1e-6]
    check(len(up_faces) == 2, "one upward quad (2 faces)",
          len(up_faces))

    # -- culling --------------------------------------------------------------
    # Downward faces (normal (0,0,-1)) live on the plane Z=100. Their quads
    # must cover exactly 8 of the 9 cells (the centre cell sits above the
    # bark). Faces come in consecutive pairs (2 per quad); a quad's covered
    # area is its bbox extent (greedy quads are rectangles).
    down = [i for (i, (idx, _m)) in enumerate(faces)
            if abs(norms[idx[0][1]][2] + 1.0) < 1e-6]
    check(len(down) == 8, "four downward quads (8 faces)", len(down))
    down_area = 0.0
    quad_min_corners = []
    for qi in range(len(down) // 2):
        xs, zs = [], []
        for i in down[2 * qi: 2 * qi + 2]:
            idx, _m = faces[i]
            for (vi, _ni) in idx:
                xs.append(verts[vi][0])
                zs.append(verts[vi][1])
        down_area += ((max(xs) - min(xs)) / 100.0) * ((max(zs) - min(zs)) / 100.0)
        quad_min_corners.append((min(xs), min(zs)))
    check(int(round(down_area)) == 8, "leaf bottom face culled to 8/9 cells",
          down_area)
    centre = ((2 + X0) * 100.0, (2 + Z0) * 100.0)
    centre_covered = any(
            abs(cx - centre[0]) < 1e-3 and abs(cz - centre[1]) < 1e-3
            for (cx, cz) in quad_min_corners)
    check(not centre_covered, "centre cell of the bottom face is culled")

    # -- usemtl groups --------------------------------------------------------
    groups = []
    seen = set()
    for (_idx, m) in faces:
        if m not in seen:
            seen.add(m)
            groups.append(m)
    check(sorted(seen) == ["bark", "leaves"], "usemtl families present",
          sorted(seen))
    leaf_faces = sum(1 for (_i, m) in faces if m == "leaves")
    bark_faces = sum(1 for (_i, m) in faces if m == "bark")
    check(leaf_faces == 18, "leaf faces", leaf_faces)
    check(bark_faces == 8, "bark faces", bark_faces)

    # -- UVs ------------------------------------------------------------------
    # Block-unit UVs: every vt pair must be integral (whole blocks).
    check(np.allclose(uvs, np.round(uvs)), "UVs integral in block units")

    print()
    if FAILURES:
        print("FAILED (%d): %s" % (len(FAILURES), ", ".join(FAILURES)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
