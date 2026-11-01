# -*- coding: utf-8 -*-
"""
qa_obj_top_vs_truth.py -- what the meshes actually draw, column by column.

The ortho frame says "something is wrong here"; this says *what*. It rasterises
the +Z faces of every building mesh back into the campus grid and compares each
column's topmost drawn material with the material the save says belongs there.

Why a rasteriser and not another render: the render merges geometry, materials,
lighting and tone mapping into one number, so a wrong roof and a warm sun look
the same in it. The OBJ files are the contract between the save and the level --
if the top face of a column carries the wrong `usemtl`, the level has the wrong
material no matter what the light does, and that is decidable offline with no
editor, no RHI and no sun.

Inputs
  out/structures/manifest.json + the *_structure.obj / *_detail.obj beside it
  out/materials/voxelmat.npz      (per-voxel face slots + block names)
  tools/block_textures.py         (block name -> top/side/bottom texture slot)

Outputs
  out/qa/obj_top_vs_truth.txt     report
  out/qa/obj_top_vs_truth.png     truth | drawn | mismatch map

Each column is classified once:
  ok        the drawn top material equals the truth top material
  wrong     a top face is drawn, with a different material
  missing   the save has a block, the meshes draw no top face
  extra     the meshes draw a top face where the save has nothing
  nofacedata the OBJ's usemtl is not in the slot table (cannot be judged)

The pair table (truth material -> drawn material) is the actionable part: one
row with 5,000 columns is one bug, not 5,000.

Usage
  python tools/qa_obj_top_vs_truth.py [--struct out/structures] [--out out/qa]
"""

import argparse
import collections
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import block_textures                                          # noqa: E402
from reference_map import CAMPUS, MATERIAL_CACHE               # noqa: E402

#: An ortho pixel covers one block, so a column is one sample. The report sorts
#: by these, and a pair covering less than this is listed but not blamed.
NOISE_COLUMNS = 20


def say(msg, lines):
    lines.append(str(msg))
    print(msg)


# --------------------------------------------------------------------------- #
# truth: what the save says the top block of each column is
# --------------------------------------------------------------------------- #

def truth_top():
    """-> (top_y (W, D) int16, slot (W, D) uint16, block (W, D) object, names).

    A column's answer is its highest voxel with a non-zero top-face slot. The
    slot table is indexed from 1 with 0 reserved for air, which is the same test
    `reference_map.top_block_per_column` and `classify` use.
    """
    z = np.load(os.path.join(REPO, MATERIAL_CACHE), allow_pickle=False)
    top_vol, nid = z["top"], z["nameid"]
    names = [str(s) for s in z["names"]]
    W, H, D = top_vol.shape

    top_y = np.full((W, D), -1, dtype=np.int16)
    slot = np.zeros((W, D), dtype=np.uint16)
    blk = np.full((W, D), -1, dtype=np.int32)
    for y in range(H):
        here = top_vol[:, y, :] != 0
        top_y = np.where(here, y, top_y)
        slot = np.where(here, top_vol[:, y, :], slot)
        blk = np.where(here, nid[:, y, :], blk)
    return top_y, slot, blk, names


# --------------------------------------------------------------------------- #
# drawn: rasterise every mesh's +Z faces into the same grid
# --------------------------------------------------------------------------- #

def read_obj_flat_faces(path):
    """-> list of (usemtl, z_cm, x0, x1, y0, y1) for *up-facing* Z-plane faces.

    The mesher writes axis-aligned rectangles (then triangulates them on export),
    so a triangle whose three vertices share a Z is one corner of such a face and
    its bbox is the face. Verts are declared once at the top of the file and
    `usemtl` only switches the material of later faces, so the vertex table is
    read first and the faces second.

    Only +Z faces count. A box has two faces that are flat in Z -- its top and
    its bottom -- and an ortho camera above the campus sees only the top one.
    The first version of this function took every flat face on the strength of
    its own docstring saying "+Z", never read the `vn` table, and so let every
    down-facing face compete for "the highest thing in this column". Where a
    column's real roof was culled (blocked by the block above, which belongs to
    a different object but is in the shared `occupied` volume) the bottom face
    of a slab one level up would win, reporting a height the level never draws.
    """
    verts, norms = [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("vn "):
                p = line.split()
                norms.append((float(p[1]), float(p[2]), float(p[3])))
            elif not line.startswith(("#", "vt ")):
                break
    if not verts:
        return []
    V = np.asarray(verts, dtype=np.float64)
    N = np.asarray(norms, dtype=np.float64) if norms else None
    n = len(V)

    out = []
    mat = None
    with open(path) as fh:
        for line in fh:
            if line.startswith("usemtl "):
                mat = line.split(None, 1)[1].strip()
            elif mat is not None and line.startswith("f "):
                tok = line.split()[1:]
                idx = [int(t.split("/")[0]) for t in tok]
                idx = [i - 1 if i > 0 else n + i for i in idx]
                pts = V[idx]
                zs = pts[:, 2]
                z0, z1 = float(zs.min()), float(zs.max())
                if z0 != z1:
                    continue                    # vertical face: not a roof
                if N is not None:
                    # `f v/vt/vn` with one shared index, so the third field is
                    # the normal this corner was written with. A face is up if
                    # its normal is; one corner is enough, the mesher emits a
                    # flat normal per quad.
                    try:
                        ni = int(tok[0].split("/")[2])
                    except (IndexError, ValueError):
                        ni = 0
                    if ni and float(N[ni - 1 if ni > 0 else len(N) + ni][2]) <= 0.5:
                        continue                # down-facing: not a roof
                out.append((mat, z0,
                            float(pts[:, 0].min()), float(pts[:, 0].max()),
                            float(pts[:, 1].min()), float(pts[:, 1].max())))
    return out


def rasterise(struct_dir, manifest, W, D):
    """-> (top_z (W, D), mat_id (W, D), mat_names, per_job, owner (W, D)).

    Faces are applied in ascending Z, so the last write to a column is its
    highest face -- the one the ortho camera sees. Overlapping coplanar faces
    (structure and detail both drawing the same plane, or a greedy quad and the
    quad it overlaps) resolve to whichever came last, which is arbitrary but
    only matters where the two disagree, and the pair table will show that.

    ``owner`` is 1-based into ``per_job`` and records *which* mesh won the
    column, because "163 columns are wrong" is not actionable and "bld_013's
    structure mesh is drawing bld_013's roof four blocks low" is.
    """
    jobs = []
    for b in manifest["buildings"]:
        for part in b["meshes"].values():
            p = os.path.join(struct_dir, part["obj"])
            if os.path.isfile(p):
                jobs.append((b["id"], p))
    water = (manifest.get("water") or {}).get("water_layer") or {}
    if water.get("obj"):
        p = os.path.join(struct_dir, water["obj"])
        if os.path.isfile(p):
            jobs.append((None, p))

    faces = []
    per_job = []
    for job_index, (bid, path) in enumerate(jobs):
        f = read_obj_flat_faces(path)
        per_job.append((bid, os.path.basename(path), len(f)))
        faces.extend((job_index + 1,) + face for face in f)

    mat_names = sorted({f[1] for f in faces})
    mat_index = {m: i + 1 for i, m in enumerate(mat_names)}

    top_z = np.full((W, D), -1e9, dtype=np.float64)
    mat_id = np.zeros((W, D), dtype=np.int16)
    owner = np.zeros((W, D), dtype=np.int16)

    x0, x1, z0, z1 = CAMPUS
    for job, mat, zc, xa, xb, ya, yb in sorted(faces, key=lambda f: f[2]):
        # cm -> block index, half-open. Floor on the low edge, ceil on the high
        # edge, clamped so a quad that ends exactly on a boundary does not claim
        # the next column.
        #
        # The grid is [x, z] (that is the order of the material cache and of
        # every (W, D) array here), while a quad's bbox is (x from the OBJ's X,
        # z from the OBJ's Y). Writing [ra:rb, ca:cb] transposes the whole map
        # and produced a symmetric +-4-block height histogram plus 0% agreement
        # on the first run -- the kind of result that reads as a data problem
        # rather than as a bug in the checker.
        ca = int(np.floor(xa / 100.0)) - x0
        cb = int(np.ceil(xb / 100.0)) - x0
        ra = int(np.floor(ya / 100.0)) - z0
        rb = int(np.ceil(yb / 100.0)) - z0
        ca, cb = max(0, ca), min(W, cb)
        ra, rb = max(0, ra), min(D, rb)
        if ca >= cb or ra >= rb:
            continue
        top_z[ca:cb, ra:rb] = zc
        mat_id[ca:cb, ra:rb] = mat_index[mat]
        owner[ca:cb, ra:rb] = job

    return top_z, mat_id, mat_names, per_job, owner


# --------------------------------------------------------------------------- #
# compare
# --------------------------------------------------------------------------- #

def truth_role(top_y):
    """-> (W, D) uint8 role of each column's top block, 255 where there is none.

    The comparison has to know who is *supposed* to draw a column. A grass lawn
    is the terrain tiles' job and the building meshes legitimately draw nothing
    there, so counting it as `missing` would drown the real defects: the first
    run of this tool did exactly that and reported 75.94% missing, which was the
    terrain, not a bug.
    """
    role = np.load(os.path.join(REPO, "out/classify/rolevolume.npy"))
    W, H, D = role.shape
    out = np.full((W, D), 255, dtype=np.uint8)
    for y in range(H):
        here = top_y == y
        out = np.where(here, role[:, y, :], out)
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--struct", default=os.path.join(REPO, "out/structures"))
    ap.add_argument("--out", default=os.path.join(REPO, "out/qa"))
    ap.add_argument("--dump", type=int, default=40,
                    help="print this many wrong columns with their coordinates "
                         "(0 to suppress)")
    args = ap.parse_args(argv)
    dump = max(0, int(args.dump))

    os.makedirs(args.out, exist_ok=True)
    lines = []
    say("=== obj top face vs save, per column ===", lines)

    with open(os.path.join(args.struct, "manifest.json"), encoding="utf-8") as fh:
        manifest = json.load(fh)

    top_y, truth_slot, truth_blk, names = truth_top()
    W, D = top_y.shape
    say("campus %s  grid %dx%d (x by z)" % (list(CAMPUS), W, D), lines)

    slot_names = block_textures.slot_names()
    say("slot table: %d textures" % len(slot_names), lines)

    top_z, mat_id, mat_names, per_job, owner = rasterise(args.struct, manifest,
                                                         W, D)
    say("meshes rasterised: %d (%d flat +Z faces)"
        % (len(per_job), sum(j[2] for j in per_job)), lines)

    # usemtl -> texture slot. The OBJ writes the texture name, the slot table is
    # the canonical index, and the truth side speaks in slots, so both sides are
    # compared as slot indices rather than as strings.
    tex_to_slot = {}
    for i, nm in enumerate(slot_names):
        tex_to_slot.setdefault(nm, i + 1)
    unmapped = [m for m in mat_names if m not in tex_to_slot]
    say("usemtl names: %d, of which %d are not in the slot table%s"
        % (len(mat_names), len(unmapped),
           (" (" + ", ".join(unmapped[:6]) + ")") if unmapped else ""), lines)

    mat_slot = np.zeros(len(mat_names) + 1, dtype=np.uint16)
    for i, m in enumerate(mat_names):
        mat_slot[i + 1] = tex_to_slot.get(m, 0)
    drawn_slot = mat_slot[mat_id]                 # (W, D) slot in truth space

    has_truth = top_y >= 0
    has_drawn = mat_id > 0

    role = truth_role(top_y)
    import classify as _cl
    in_charge = has_truth & ((role == _cl.STRUCTURE) | (role == _cl.DETAIL))
    role_names = {getattr(_cl, n): n for n in
                  ("EMPTY", "TERRAIN", "STRUCTURE", "VEGETATION", "WATER",
                   "DETAIL")}

    ok = in_charge & has_drawn & (drawn_slot == truth_slot)
    wrong = in_charge & has_drawn & (drawn_slot != truth_slot)
    missing = in_charge & ~has_drawn
    extra = ~has_truth & has_drawn

    total = int(in_charge.sum())
    say("", lines)
    say("columns whose top block is a building part (structure or detail): %d"
        % total, lines)
    say("  ok        %7d  %5.2f%%" % (ok.sum(), 100.0 * ok.sum() / total), lines)
    say("  wrong     %7d  %5.2f%%" % (wrong.sum(),
                                      100.0 * wrong.sum() / total), lines)
    say("  missing   %7d  %5.2f%%" % (missing.sum(),
                                      100.0 * missing.sum() / total), lines)
    say("  extra (drawn where the save has no block) %7d" % extra.sum(), lines)
    say("", lines)
    say("for reference, columns by top-block role:", lines)
    for rv in sorted(set(np.unique(role).tolist())):
        n = int((role == rv).sum())
        say("  %-12s %7d" % (role_names.get(rv, "none" if rv == 255 else rv), n),
            lines)

    # ---- the actionable table: truth material -> drawn material ----------- #
    pair = collections.Counter()
    rows, cols = np.nonzero(wrong)
    for r, c in zip(rows, cols):
        pair[(int(truth_slot[r, c]), int(mat_id[r, c]))] += 1

    say("", lines)
    say("## wrong columns by (save material -> drawn usemtl)", lines)
    say("  %-28s %-28s %8s %7s" % ("save top texture", "drawn usemtl",
                                   "columns", "share"), lines)
    for (ts, di), n in pair.most_common(30):
        say("  %-28s %-28s %8d %6.2f%%"
            % (slot_names[ts - 1], mat_names[di - 1], n,
               100.0 * n / max(1, wrong.sum())), lines)

    # ---- what the save says for the biggest offender, as block names ------ #
    if pair:
        (ts, di), n = pair.most_common(1)[0]
        sel = wrong & (truth_slot == ts) & (mat_id == di)
        blk = collections.Counter(names[int(truth_blk[r, c])]
                                  for r, c in zip(*np.nonzero(sel)))
        say("", lines)
        say("## biggest offender: save texture %r drawn as usemtl %r (%d columns)"
            % (slot_names[ts - 1], mat_names[di - 1], n), lines)
        say("   the save puts these blocks on top there:", lines)
        for b, k in blk.most_common(10):
            say("     %-34s %d" % (b, k), lines)

    # ---- who drew the bad columns ----------------------------------------- #
    #
    # "163 columns are wrong" cannot be acted on. The same 163 grouped by the
    # mesh that won them is either one bug in one building or a systemic one,
    # and the difference decides whether the fix is in the mesher or in the
    # save parsing.
    def by_owner(mask, label):
        say("", lines)
        say("## %s by the mesh that won the column" % label, lines)
        if not mask.any():
            say("  (none)", lines)
            return
        cnt = collections.Counter(int(owner[r, c])
                                  for r, c in zip(*np.nonzero(mask)))
        say("  %-30s %-26s %8s" % ("building", "mesh", "columns"), lines)
        for j, n in cnt.most_common(15):
            # owner == 0 means no face at all reached the column. It is not
            # "the last mesh in per_job" -- indexing per_job[-1] for it is how
            # the first version of this table blamed one innocent building for
            # every missing column on the campus.
            if j == 0:
                bid = base = "(nothing drawn)"
            else:
                bid, base, _nf = per_job[j - 1]
                bid = bid or "(no id)"
            say("  %-30s %-26s %8d" % (bid, base, n), lines)
        if len(cnt) > 15:
            say("  ... and %d more meshes" % (len(cnt) - 15), lines)

    by_owner(wrong, "wrong columns")
    by_owner(missing, "missing columns")

    # ---- the columns themselves, so the fix has somewhere to start --------- #
    if dump:
        say("", lines)
        say("## first %d wrong columns, in grid order" % dump, lines)
        say("  %-14s %-8s %-28s %-28s %s"
            % ("x,z (block)", "save y", "save top texture", "drawn usemtl",
               "mesh"), lines)
        for r, c in list(zip(*np.nonzero(wrong)))[:dump]:
            j = int(owner[r, c])
            # Every (W, D) array here is [x, z] -- see the transpose warning in
            # rasterise -- so the row is x and the column is z. Printing them
            # the other way round produced a plausible-looking list of
            # coordinates where the save had no building block at all, which
            # sends whoever reads it to the wrong building.
            say("  %-14s %-8d %-28s %-28s %s"
                % ("%d,%d" % (int(r) + CAMPUS[0], int(c) + CAMPUS[2]),
                   int(top_y[r, c]),
                   slot_names[int(truth_slot[r, c]) - 1],
                   mat_names[int(mat_id[r, c]) - 1],
                   per_job[j - 1][1] if j else "?"), lines)

    # ---- per-column height agreement, independent of material ------------- #
    drawn_y = np.where(has_drawn, top_z / 100.0 - 1.0, np.nan)
    both = in_charge & has_drawn
    dz = drawn_y - top_y.astype(np.float64)
    dz = dz[both]
    if dz.size:
        say("", lines)
        say("## top surface height (drawn_y = z_cm/100 - 1)", lines)
        say("  exact %.2f%%  within 1 block %.2f%%  mean %.2f  max %.2f"
            % (100.0 * (dz == 0).mean(), 100.0 * (np.abs(dz) <= 1).mean(),
               float(dz.mean()), float(np.abs(dz).max())), lines)
        hist = collections.Counter(np.clip(dz, -4, 4).astype(int).tolist())
        for d in sorted(hist):
            say("    drawn-save = %+d : %d" % (d, hist[d]), lines)

    # ---- image ------------------------------------------------------------ #
    def rgb_of(slot):
        rgb = np.zeros((W, D, 3), np.uint8)
        palette = np.zeros((len(slot_names) + 1, 3), np.uint8)
        for i, nm in enumerate(slot_names):
            try:
                img = block_textures.Image
                palette[i + 1] = _tex_mean(nm)
            except Exception:
                palette[i + 1] = (128, 128, 128)
        rgb = palette[np.clip(slot, 0, len(slot_names))]
        return rgb

    truth_rgb = _palette_rgb(truth_slot, slot_names)
    drawn_rgb = _palette_rgb(np.where(has_drawn, drawn_slot, 0), slot_names)
    mask = np.zeros((W, D, 3), np.uint8)
    mask[...] = (40, 40, 40)
    mask[ok] = (40, 150, 60)
    mask[wrong] = (220, 60, 40)
    mask[missing] = (60, 120, 220)
    mask[extra] = (200, 180, 40)

    img = np.concatenate([truth_rgb.transpose(1, 0, 2),
                          drawn_rgb.transpose(1, 0, 2),
                          mask.transpose(1, 0, 2)], axis=1)
    out_png = os.path.join(args.out, "obj_top_vs_truth.png")
    block_textures.Image.fromarray(img).save(out_png)
    say("", lines)
    say("wrote %s  (truth | drawn | red=wrong blue=missing)" % out_png, lines)

    txt = os.path.join(args.out, "obj_top_vs_truth.txt")
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say("wrote %s" % txt, lines)

    # The masks, so a follow-up probe can ask "why" about exactly these columns
    # instead of re-deriving them. `probe_unclaimed_top.py --masks` reads this.
    npz = os.path.join(args.out, "obj_top_vs_truth_masks.npz")
    np.savez_compressed(npz, wrong=wrong, missing=missing, ok=ok, extra=extra,
                        top_y=top_y, truth_slot=truth_slot, drawn_slot=drawn_slot,
                        campus=np.asarray(CAMPUS, dtype=np.int32))
    say("wrote %s  (masks for probe_unclaimed_top.py)" % npz, lines)
    say("", lines)
    say("RESULT: %s -- %.2f%% of columns draw the save's top material"
        % ("PASS" if ok.sum() >= 0.95 * total else "FAIL",
           100.0 * ok.sum() / total), lines)
    return 0


#: texture mean colours, cached because the palette is 274 images and the
#: comparison asks for them once per slot, not once per column.
_TEX_MEAN = {}


def _tex_mean(tex_name):
    v = _TEX_MEAN.get(tex_name)
    if v is not None:
        return v
    path = os.path.join(block_textures.OUT_DIR, tex_name + ".png")
    if not os.path.isfile(path):
        _TEX_MEAN[tex_name] = (128, 128, 128)
        return _TEX_MEAN[tex_name]
    im = block_textures.Image.open(path).convert("RGB")
    a = np.asarray(im, dtype=np.float64)
    _TEX_MEAN[tex_name] = tuple(int(round(x)) for x in a.reshape(-1, 3).mean(0))
    return _TEX_MEAN[tex_name]


def _palette_rgb(slot, slot_names):
    W, D = slot.shape
    lut = np.zeros((len(slot_names) + 1, 3), np.uint8)
    for i, nm in enumerate(slot_names):
        lut[i + 1] = _tex_mean(nm)
    return lut[np.clip(slot, 0, len(slot_names))]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
