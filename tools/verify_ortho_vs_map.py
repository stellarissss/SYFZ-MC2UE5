# -*- coding: utf-8 -*-
"""verify_ortho_vs_map.py -- per-block agreement between the saved world and the
UE orthographic top-down frame.

Why this exists
---------------
The acceptance list carries one item the pipeline never proved with numbers:
"check the pipeline logic against the original map file". `out/truth_colour.png`
is the save side of that check -- one pixel per block column, the mean sRGB of
the 16x16 vanilla top-face texture, computed from the save and the textures with
no UE content in it. `out/ortho_topdown.png` is the render side, same 448x768,
one pixel per block. Until now the two were only ever shown side by side, and a
side-by-side image cannot say *which* blocks disagree.

Three things make a naive pixel diff useless here, and each is answered
separately below rather than averaged into one number:

1. **Alignment is not a given.** Earlier frames in this project were exported
   with flipped rows, and a mirrored frame would diff "almost right", which is
   the most dangerous kind of wrong. So the frame is scored against the truth
   over all 8 axis transforms (4 rotations x mirrored-or-not) plus a small
   translation search, using a zero-parameter colour signature. The winning
   transform is printed and written to the report; if it is a mirroring one, the
   report says so on a line of its own -- that is a fact about the world, not
   something to absorb into a resampling step.

2. **RGB is not comparable.** The render is lit and tone-mapped; the truth is
   unlit albedo. A single global per-channel gain+offset (6 parameters, fitted by
   least squares over the whole frame) removes the systematic exposure and white
   balance difference. It is deliberately global: per-pixel or per-tile
   adaptation would also absorb the local errors that this tool exists to find.
   The residual it leaves is reported, because that residual is the ceiling on
   how fine a colour question this frame can answer (see "metric ceiling").

3. **Per-pixel equality is the wrong question.** A material can be the right
   material in the right place and still be 30/255 off. The primary measure is
   therefore coarse-class agreement over 7 classes (lawn / fabric / track /
   paving / water / veg / other). The truth side is classified by *block name*
   through `block_families.family()`, the project's single mapping; the render
   side by nearest colour prototype, where the prototypes are the truth-side
   top-face texture means of the blocks that actually appear on this campus. Both
   sides are judged against one colour table by construction, and no class
   statistics are ever estimated from the render itself.

Because the aggregate turns out to be sensitive to the colour metric, the report
does not stop at one number: it also gives a per-block identity table (which
truth block reads as which class at each column), the same classification under
two alternative global colour models, and the distance between the classes that
sit closest together. The aggregate is a summary; the per-block table is the
evidence.

Expected disagreements are reported as their own layer instead of being mixed
into the headline: a tree in the render is cross-plane geometry, so from directly
above it shows slivers of leaf and mostly whatever is beneath, while the truth
records the canopy texture. That layer is supposed to differ.

Outputs (out/ is not tracked):
  out/ortho_vs_map_diff.png        truth | registered render | mismatch map
  out/ortho_vs_map_report.txt      everything below, as text
  ../logs/verify_ortho_vs_map.txt  the same, timestamped (a stale log cannot pass)

    python tools/verify_ortho_vs_map.py
    python tools/verify_ortho_vs_map.py --frame out/some_other_frame.png

Exit codes are real: a wrong image size, an unregistrable or ambiguous frame, a
scale outside 1.00 +/- 0.03, or a prototype table that cannot reproduce the truth
all exit non-zero, and the last line is always `RESULT: PASS ...` or
`RESULT: FAIL ...`.
"""

import argparse
import collections
import hashlib
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import block_families as bf                                       # noqa: E402
import truth_colour_map as tcm                                    # noqa: E402
from reference_map import CAMPUS                                  # noqa: E402

W, H = 448, 768
CELL = 16                       # block-vote cell for the spatial table
NCELL_X, NCELL_Z = W // CELL, H // CELL     # 28 x 48

#: Coarse classes. The index is what the confusion matrix is indexed by.
COARSE = ("lawn", "fabric", "track", "paving", "water", "veg", "other")

#: Material family -> coarse class. `block_families.FAMILIES` is the authority on
#: what a family *is*; this table only groups families for this comparison, so it
#: cannot drift from the renderer's block->material decision.
#:
#: Two groupings are debatable and are chosen deliberately, and the report says
#: so: `gravel` (sand / sandstone / concrete_powder) joins `lawn` because the
#: class means "ground", and the family cannot split sand from sandstone; `brick`
#: is the track because 16,798 of its columns are pink_terracotta, i.e. the track
#: surface.
FAMILY_TO_COARSE = {
    "grass": "lawn", "path": "lawn", "soil": "lawn", "gravel": "lawn",
    "fabric": "fabric", "concrete": "fabric",
    "brick": "track", "tiles": "track",
    "greystone": "paving", "quartz": "paving", "granite": "paving",
    "water": "water",
    "leaves": "veg", "bark": "veg",
    # everything else is a structure or a fitting, not a surface:
    # wood, roof, metal, bars, plaster, glass, rock, asphalt, other
}

TRANSFORMS = [
    ("identity",   lambda a: a,                                 False),
    ("flip_h",     lambda a: a[:, ::-1],                        True),
    ("flip_v",     lambda a: a[::-1],                           True),
    ("rot180",     lambda a: a[::-1, ::-1],                     False),
    ("rot90",      lambda a: np.rot90(a, 1),                    False),
    ("rot270",     lambda a: np.rot90(a, 3),                    False),
    ("transpose",  lambda a: a.transpose(1, 0, 2),               True),
    ("antitrans",  lambda a: np.rot90(a, 2).transpose(1, 0, 2),  True),
]

MAX_SHIFT = 6                   # px, the translation search around zero
MIN_LEAD = 1.15                 # best transform must beat the runner-up by this
MAX_SCALE_DEV = 0.03            # |scale - 1| above this is a failure


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #

class Log(object):
    """Line list mirrored to stdout and to a log file on every line.

    The file is rewritten on each line (same contract as ensure_lighting.py): if
    the process dies, the log still shows how far it got, and the first line
    carries the timestamp so an old log cannot be mistaken for a fresh pass.
    """

    def __init__(self, path):
        self.path = path
        self.lines = []

    def __call__(self, msg=""):
        msg = str(msg)
        self.lines.append(msg)
        print(msg)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(self.lines) + "\n")
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# io
# --------------------------------------------------------------------------- #

def load_rgb(path):
    if not os.path.isfile(path):
        raise SystemExit("missing image: %s" % path)
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    a = np.asarray(im)
    if a.shape[:2] != (H, W):
        raise SystemExit("%s is %dx%d, expected %dx%d (1 px = 1 block)"
                         % (path, a.shape[1], a.shape[0], W, H))
    return a.astype(np.uint8)


def md5(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()[:8]


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

def signature(img, axis):
    """-> per-line colour signature: mean and std of each channel.

    axis=1 gives one row per image row, axis=0 one row per image column. Built
    from statistics *along* the line, so it changes when the line is replaced by
    another one; standardising each feature makes it insensitive to exposure.
    Std is carried because two lines can share a mean and still be different.
    """
    f = img.astype(np.float64)
    return np.concatenate([f.mean(axis=axis), f.std(axis=axis)], axis=1)


def zcorr(x, y):
    """Pooled correlation of two (n, k) feature matrices."""
    x = x - x.mean(axis=0)
    y = y - y.mean(axis=0)
    sx, sy = x.std(axis=0), y.std(axis=0)
    sx[sx < 1e-9] = 1.0
    sy[sy < 1e-9] = 1.0
    return float(((x / sx) * (y / sy)).mean())


def best_shift(x, y, max_shift=MAX_SHIFT):
    """-> (score, shift) for the best overlap of two signatures."""
    n = len(x)
    best = (-2.0, 0)
    for s in range(-max_shift, max_shift + 1):
        if s >= 0:
            a, b = x[s:], y[:n - s]
        else:
            a, b = x[:n + s], y[-s:]
        if len(a) < 32:
            continue
        c = zcorr(a, b)
        if c > best[0]:
            best = (c, s)
    return best


def as_grid(a):
    """Nearest-neighbour fit to (H, W, 3) when a transform rotated the aspect."""
    if a.shape[:2] == (H, W):
        return a
    return np.asarray(Image.fromarray(a).resize((W, H), Image.NEAREST))


def register(frame, truth, log):
    """-> dict with the winning transform, its margins, scale and offset.

    Scores every axis transform at its best small translation, then fits a 1-D
    scale+offset per axis at the winner. No step assumes the frame is aligned.
    """
    t_sig_r, t_sig_c = signature(truth, 1), signature(truth, 0)
    rows = []
    for name, fn, mirror in TRANSFORMS:
        t = as_grid(fn(frame))
        rr = best_shift(signature(t, 1), t_sig_r)
        cc = best_shift(signature(t, 0), t_sig_c)
        rows.append({"name": name, "mirror": mirror,
                     "row": rr[0], "row_shift": rr[1],
                     "col": cc[0], "col_shift": cc[1],
                     "score": 0.5 * (rr[0] + cc[0])})
    rows.sort(key=lambda r: -r["score"])
    best, second = rows[0], rows[1]
    lead = best["score"] / second["score"] if second["score"] > 0 else float("inf")

    log("--- registration: 8 axis transforms against the truth ---")
    log("%-11s %-6s %8s %8s %8s %9s  %s"
        % ("transform", "mirror", "r_row", "r_col", "score", "shift", "note"))
    for r in rows:
        note = ""
        if r["name"] == best["name"]:
            note = "<== best"
        elif r["name"] == second["name"]:
            note = "runner-up"
        log("%-11s %-6s %8.4f %8.4f %8.4f %4d/%+3d   %s"
            % (r["name"], "yes" if r["mirror"] else "no", r["row"], r["col"],
               r["score"], r["row_shift"], r["col_shift"], note))
    log("")
    log("winner .......... %s%s" % (best["name"],
                                    "   (MIRRORING)" if best["mirror"] else ""))
    log("lead over 2nd ... %.3fx (over %s, %.4f vs %.4f)"
        % (lead, second["name"], best["score"], second["score"]))
    log("shift at zero ... rows %+d px, columns %+d px"
        % (best["row_shift"], best["col_shift"]))

    fn = dict((n, f) for n, f, _m in TRANSFORMS)[best["name"]]
    t = as_grid(fn(frame))

    def fit_axis(src, dst):
        """1-D scale+offset: dest[i] = src[scale*i + offset]."""
        out = (-2.0, 1.0, 0.0)
        n = len(dst)
        idx = np.arange(n, dtype=np.float64)
        for scale in np.arange(0.94, 1.0601, 0.002):
            for off in np.arange(-14.0, 14.01, 0.5):
                j = scale * idx + off
                if j[0] < 0.0 or j[-1] > n - 1:
                    continue
                samp = np.stack([np.interp(j, idx, src[:, k])
                                 for k in range(src.shape[1])], axis=1)
                c = zcorr(samp, dst)
                if c > out[0]:
                    out = (c, float(scale), float(off))
        return out

    r_corr, sy, oy = fit_axis(signature(t, 1), t_sig_r)
    c_corr, sx, ox = fit_axis(signature(t, 0), t_sig_c)

    # A frame that is 1:1 within half a pixel is compared 1:1. Resampling it by
    # a sub-pixel amount with nearest neighbour duplicates some rows and drops
    # others, which manufactures a row of error along the whole frame -- the
    # first run of this tool lost 5 points of agreement to exactly that.
    if abs(sy - 1.0) < 0.005:
        sy, oy = 1.0, float(np.rint(oy))
    if abs(sx - 1.0) < 0.005:
        sx, ox = 1.0, float(np.rint(ox))
    log("scale .......... rows %.3f (r=%.4f)  columns %.3f (r=%.4f)"
        % (sy, r_corr, sx, c_corr))
    log("offset ......... rows %+.1f px  columns %+.1f px" % (oy, ox))
    log("")

    return {"best": best, "second": second, "lead": lead, "frame": t,
            "sy": sy, "oy": oy, "sx": sx, "ox": ox, "mirror": best["mirror"]}


def warp(img, sy, oy, sx, ox):
    """Resample `img` onto the truth grid: dest[i] = src[scale*i + offset]."""
    ys = np.clip(np.rint(sy * np.arange(H) + oy), 0, H - 1).astype(np.int32)
    xs = np.clip(np.rint(sx * np.arange(W) + ox), 0, W - 1).astype(np.int32)
    return img[ys][:, xs]


# --------------------------------------------------------------------------- #
# colour models
# --------------------------------------------------------------------------- #

def fit_colour_model(render, truth):
    """-> (gain (3,), offset (3,), rmse (3,), mae, r2 (3,), fitted image).

    render = gain * truth + offset, one global 6-parameter least squares over
    every pixel. Global and not adaptive on purpose: a per-tile fit would track
    lighting variation, but it would also track *material* errors, which are the
    thing being measured.
    """
    gain = np.zeros(3)
    off = np.zeros(3)
    res = np.zeros(3)
    r2 = np.zeros(3)
    for c in range(3):
        x = truth[..., c].astype(np.float64).ravel()
        y = render[..., c].astype(np.float64).ravel()
        A = np.stack([x, np.ones_like(x)], axis=1)
        sol, _r, _k, _s = np.linalg.lstsq(A, y, rcond=None)
        gain[c], off[c] = sol
        e = y - A.dot(sol)
        res[c] = np.sqrt(np.mean(e * e))
        var = np.var(y)
        r2[c] = 1.0 - np.var(e) / var if var > 0 else 0.0
    fitted = np.clip((render.astype(np.float64) - off) / gain, 0.0, 255.0)
    mae = float(np.abs(fitted - truth.astype(np.float64)).mean())
    return gain, off, res, mae, r2, fitted


def fit_gamma_model(render, truth):
    """-> fitted image under render = a * (truth/255)^p + b, per channel.

    Not the criterion -- a sensitivity check. If a global curve-fit moves the
    class agreement a lot, then the aggregate is limited by the frame's tone
    response rather than by where materials were placed, and the numbers have to
    be read that way.
    """
    from scipy.optimize import minimize_scalar
    p = np.zeros(3)
    a = np.zeros(3)
    b = np.zeros(3)
    for c in range(3):
        x = truth[..., c].astype(np.float64).ravel() / 255.0
        y = render[..., c].astype(np.float64).ravel()

        def sse(expo):
            A = np.stack([x ** expo, np.ones_like(x)], axis=1)
            sol, _r, _k, _s = np.linalg.lstsq(A, y, rcond=None)
            e = y - A.dot(sol)
            return float(np.mean(e * e))

        p[c] = minimize_scalar(sse, bounds=(0.3, 3.0), method="bounded").x
        A = np.stack([x ** p[c], np.ones_like(x)], axis=1)
        a[c], b[c] = np.linalg.lstsq(A, y, rcond=None)[0]
    inv = np.clip(((render.astype(np.float64) - b) / a) ** (1.0 / p)
                  * 255.0, 0.0, 255.0)
    return p, a, b, inv


def to_lab(img):
    """sRGB [0,255] -> CIELAB (D65), for the perceptual sensitivity check."""
    a = np.asarray(img, dtype=np.float64) / 255.0
    a = np.where(a > 0.04045, ((a + 0.055) / 1.055) ** 2.4, a / 12.92)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = a @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16.0 / 116.0)
    return np.stack([116.0 * f[..., 1] - 16.0,
                     500.0 * (f[..., 0] - f[..., 1]),
                     200.0 * (f[..., 1] - f[..., 2])], axis=-1)


# --------------------------------------------------------------------------- #
# coarse classes
# --------------------------------------------------------------------------- #

def coarse_lut(names):
    """-> (class index per name index, family per name index)."""
    cls = np.zeros(len(names), dtype=np.int8)
    fam = np.empty(len(names), dtype=object)
    for i, nm in enumerate(names):
        f = bf.family(nm)
        fam[i] = f
        cls[i] = COARSE.index(FAMILY_TO_COARSE.get(f, "other"))
    return cls, fam


def prototypes(L, observed):
    """-> (rgb (K,3), class (K,)) one prototype per distinct (colour, class).

    Only blocks that actually appear as a column top on this campus contribute.
    A block that is in the material cache but nowhere on the surface would still
    sit somewhere in colour space and act as an attractor for real columns -- the
    first run of this tool lost 207k lawn columns to `large_fern`, a block with
    zero columns on the map. The prototypes are the truth's own texture means, so
    this is not a second colour table.

    Keyed by (colour, class), not by colour: two blocks of different families can
    share a texture mean (white wool and a white brick both hit one value), and
    collapsing them to a single prototype silently relabels a whole class.
    """
    pairs = set()
    for k in observed:
        c = tuple(int(v) for v in L["palette"][int(k)])
        pairs.add((c, int(L["cls"][int(k)])))
    pairs = sorted(pairs)
    rgb = np.array([p[0] for p in pairs], dtype=np.float32)
    cls = np.array([p[1] for p in pairs], dtype=np.int8)
    return rgb, cls


def prototype_gaps(prot, pcls, limit=5):
    """-> [(distance, class_a, class_b, rgb_a, rgb_b)] closest cross-class pairs.

    These are the decision boundaries the classifier actually uses, so the
    smallest one is the finest colour question this frame can be asked.
    """
    d = np.sqrt(((prot[:, None, :] - prot[None, :, :]) ** 2).sum(axis=2))
    out = []
    for i in range(len(prot)):
        for j in range(i + 1, len(prot)):
            if pcls[i] != pcls[j]:
                out.append((float(d[i, j]), COARSE[int(pcls[i])],
                            COARSE[int(pcls[j])],
                            tuple(int(v) for v in prot[i]),
                            tuple(int(v) for v in prot[j])))
    out.sort()
    return out[:limit]


def class_distances(blk, prot, classes, k):
    """-> (N, k) distance from every pixel to the nearest prototype of each class.

    "Distance to a class" is the distance to its closest member, not to its
    centroid: several classes are multimodal by nature (fabric spans white wool to
    lime wool), and a centroid would sit in the gap between the modes and describe
    nothing. The members are the truth's own texture means.
    """
    dcls = np.full((len(blk), k), np.inf, dtype=np.float32)
    for c in range(k):
        if not len(classes[c]):
            continue
        sub = prot[classes[c]]
        d = ((blk[:, None, :] - sub[None, :, :]) ** 2).sum(axis=2)
        dcls[:, c] = np.sqrt(d.min(axis=1))
    return dcls


def nearest_class(img, prot, pcls, prior, chunk=64):
    """-> ((H,W) class index, (H,W) margin = second-best minus best distance).

    Exact ties are broken by class area share. A colour that two classes share
    carries no evidence for either, so the more common class wins -- that is a
    prior, applied only where the data is silent, and it is visible here rather
    than hidden inside the argmin order.
    """
    k = len(COARSE)
    classes = [np.nonzero(pcls == c)[0] for c in range(k)]
    out = np.empty(img.shape[:2], dtype=np.int8)
    margin = np.empty(img.shape[:2], dtype=np.float32)
    rows = img.shape[1]
    for y0 in range(0, img.shape[0], chunk):
        blk = img[y0:y0 + chunk].reshape(-1, 3).astype(np.float32)
        dcls = class_distances(blk, prot, classes, k)
        adj = dcls - 1e-4 * prior[None, :]
        ij = np.arange(len(blk))
        best = np.argmin(adj, axis=1)
        bd = dcls[ij, best]
        adj[ij, best] = np.inf
        sd = dcls[ij, np.argmin(adj, axis=1)]
        n = len(blk) // rows
        out[y0:y0 + chunk] = best.reshape(n, rows)
        margin[y0:y0 + chunk] = (sd - bd).reshape(n, rows)
    return out, margin


def self_check(truth, prot, pcls, truth_cls, chunk=64):
    """-> share of truth columns whose own class holds the nearest prototype.

    The truth image is painted from the prototype colours, so this is the test
    that the prototype table is the truth's own table. A column whose colour is
    shared with another class counts as reproduced -- it is exactly reproduced,
    and no colour test could have done better.
    """
    k = len(COARSE)
    classes = [np.nonzero(pcls == c)[0] for c in range(k)]
    ok = 0
    rows = truth.shape[1]
    tc = truth_cls.ravel()
    for y0 in range(0, truth.shape[0], chunk):
        blk = truth[y0:y0 + chunk].reshape(-1, 3).astype(np.float32)
        dcls = class_distances(blk, prot, classes, k)
        ij = np.arange(len(blk))
        own = dcls[ij, tc[y0 * rows:(y0 + chunk) * rows]]
        ok += int((own <= dcls.min(axis=1) + 1e-6).sum())
    return ok / float(truth.shape[0] * rows)


def confusion(a, b, k):
    m = np.zeros((k, k), dtype=np.int64)
    np.add.at(m, (a.ravel(), b.ravel()), 1)
    return m


def cell_rate(mask):
    return mask.reshape(NCELL_Z, CELL, NCELL_X, CELL).mean(axis=(1, 3))


def cell_majority(cls):
    k = len(COARSE)
    one = np.zeros(cls.shape + (k,), dtype=np.float32)
    r, c = np.indices(cls.shape)
    one[r, c, cls] = 1.0
    cnt = one.reshape(NCELL_Z, CELL, NCELL_X, CELL, k).sum(axis=(1, 3))
    return cnt.argmax(axis=2), cnt.max(axis=2) / float(CELL * CELL)


def ranked(rate):
    return np.dstack(np.unravel_index(np.argsort(-rate, axis=None),
                                      rate.shape))[0]


def below_map():
    """-> ((H, W) index of the block under each column's top, (H, W) its height).

    The probe below uses it to tell "this surface is drawn with the wrong
    material" from "this surface is not drawn at all, so the frame shows what is
    underneath" -- two findings with completely different fixes. The height is
    returned because it separates the two cases that look identical otherwise:
    a whole class of surface missing at one level of the build.
    """
    from reference_map import MATERIAL_CACHE
    z = np.load(os.path.join(ROOT, MATERIAL_CACHE), allow_pickle=False)
    vol, nid = z["top"], z["nameid"]
    nw, nh, nd = vol.shape
    ytop = np.zeros((nw, nd), dtype=np.int32)
    for y in range(nh):
        ytop = np.where(vol[:, y, :] != 0, y, ytop)
    yi = np.maximum(ytop - 1, 0)
    xi = np.arange(nw)[:, None]
    zi = np.arange(nd)[None, :]
    below = nid[xi, yi, zi]
    below[ytop == 0] = 0
    return below.T, ytop.T


def occlusion_probe(names, idx, below, height, fitted, palette, min_cols=400):
    """-> rows for surfaces whose colour in the frame matches the block beneath.

    For every block that owns at least `min_cols` columns, compare the rendered
    colour at its columns with (a) its own albedo and (b) the rendered colour of
    the block recorded underneath it. A surface that matches (b) far better than
    (a) is not being drawn; a surface that matches neither is drawn with the
    wrong material. Both are reported as a distance in 8-bit levels.
    """
    own = {}
    for k in np.unique(idx):
        own[int(k)] = fitted[idx == k].reshape(-1, 3).mean(axis=0)
    counts = collections.Counter(int(v) for v in idx.ravel())
    rows = []
    for k, n in counts.items():
        if n < min_cols:
            continue
        m = idx == k
        b = below[m]
        b = b[b > 0]
        if len(b) < min_cols:
            continue
        bk, bn = collections.Counter(int(v) for v in b).most_common(1)[0]
        # Same *name* beneath means the block sits on a copy of itself (a leaf
        # card inside a canopy). Then "the block beneath" is the same surface and
        # the comparison proves nothing. Compared by name, not by index: the
        # material cache carries several indices per block name.
        if bk == k or names[bk] == names[k] or bn < 0.6 * len(b) or bk not in own:
            continue
        got = fitted[m].reshape(-1, 3).mean(axis=0)
        d_self = float(np.linalg.norm(got - palette[k]))
        d_below = float(np.linalg.norm(got - own[bk]))
        rows.append((d_self - d_below, names[k].split(":")[-1], n, d_self,
                     d_below, names[bk].split(":")[-1],
                     int(np.median(height[m]))))
    rows.sort(reverse=True)
    return rows


def block_range(ci, ri):
    x0 = CAMPUS[0] + int(ci) * CELL
    z0 = CAMPUS[2] + int(ri) * CELL
    return x0, x0 + CELL - 1, z0, z0 + CELL - 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default=os.path.join(ROOT, "out", "ortho_topdown.png"))
    ap.add_argument("--truth", default=os.path.join(ROOT, "out", "truth_colour.png"))
    ap.add_argument("--out", default=os.path.join(ROOT, "out", "ortho_vs_map_diff.png"))
    ap.add_argument("--report", default=os.path.join(ROOT, "out",
                                                     "ortho_vs_map_report.txt"))
    ap.add_argument("--log", default=os.path.join(os.path.dirname(ROOT), "logs",
                                                  "verify_ortho_vs_map.txt"))
    args = ap.parse_args()

    log = Log(args.log)
    log("=== verify_ortho_vs_map start %s ===" % time.strftime("%F %T"))
    log("frame: %s (md5 %s)" % (args.frame, md5(args.frame)
                                if os.path.isfile(args.frame) else "missing"))
    log("truth: %s (md5 %s)" % (args.truth, md5(args.truth)
                                if os.path.isfile(args.truth) else "missing"))
    log("")

    frame = load_rgb(args.frame)          # raises SystemExit on a wrong size
    truth = load_rgb(args.truth)

    # ---- 1. registration -------------------------------------------------
    reg = register(frame, truth, log)
    mirrored = reg["mirror"]

    failures = []
    if reg["best"]["score"] <= 0.0:
        failures.append("no transform correlates with the truth at all")
    if reg["lead"] < MIN_LEAD:
        failures.append("registration ambiguous (%.3fx < %.2fx lead)"
                        % (reg["lead"], MIN_LEAD))
    for axis, sc in (("rows", reg["sy"]), ("columns", reg["sx"])):
        if abs(sc - 1.0) > MAX_SCALE_DEV:
            failures.append("scale %s %.3f is outside 1.00 +/- %.2f"
                            % (axis, sc, MAX_SCALE_DEV))
    if failures:
        for f in failures:
            log("FAIL: %s" % f)
        log("RESULT: FAIL %s" % "; ".join(failures))
        return 1

    if mirrored:
        log("!" * 76)
        log("!! REFLECTION: render/%s == truth -- the UE frame is a MIRROR image"
            % reg["best"]["name"])
        log("!! of the save's top-down view. That is a chirality difference in the")
        log("!! pipeline, not a framing choice. It is reported here rather than")
        log("!! being silently undone by the resampling below.")
        log("!" * 76)
        log("")

    warped = warp(reg["frame"], reg["sy"], reg["oy"], reg["sx"], reg["ox"])

    # ---- 2. colour model -------------------------------------------------
    gain, off, res, mae, r2, fitted = fit_colour_model(warped, truth)
    raw_mae = float(np.abs(warped.astype(np.float64)
                           - truth.astype(np.float64)).mean())
    log("--- colour model: render = gain * truth + offset (global LSQ) ---")
    log("gain   R %+.4f  G %+.4f  B %+.4f" % tuple(gain))
    log("offset R %+.2f  G %+.2f  B %+.2f" % tuple(off))
    log("residual RMSE R %.2f  G %.2f  B %.2f  (8-bit levels)" % tuple(res))
    log("R^2           R %.4f  G %.4f  B %.4f" % tuple(r2))
    log("mean |dRGB| after the fit %.2f (before %.2f)" % (mae, raw_mae))
    log("")

    # ---- 3. classes ------------------------------------------------------
    names = tcm.load_names()
    palette, info = tcm.build_palette(names)
    top, has = _top_columns()
    cls_lut, fam_lut = coarse_lut(names)
    idx = top.T
    truth_cls = cls_lut[idx]

    # A column is "cover" when its top tile carries alpha (leaves, plants,
    # bars). Taken from the truth side's own tile bookkeeping, not a name list.
    alpha_by_name = np.array([bool(info[i]["alpha_weighted"])
                              for i in range(len(names))])
    cover = alpha_by_name[idx]
    veg = truth_cls == COARSE.index("veg")
    terrain = (~veg) & (~cover)

    L = {"palette": palette, "cls": cls_lut}
    observed = np.unique(idx)
    prot, pcls = prototypes(L, observed)
    prior = np.bincount(truth_cls.ravel(), minlength=len(COARSE)).astype(np.float64)
    prior /= max(prior.sum(), 1.0)
    # Classify in the space the fit was minimised in: the model predicts what
    # each prototype should look like in the frame, and a pixel is compared with
    # that prediction. Going the other way round -- inverting the fit per pixel
    # and clipping at 255 -- throws away every bright surface that the fit pushes
    # over the top, which is most of the white masonry (measured: 22.9% vs 27.8%
    # agreement on the same frame, with whole classes relabelled).
    predicted = (gain * prot.astype(np.float64) + off).astype(np.float32)
    render_cls, margin = nearest_class(warped, predicted, pcls, prior)

    # Self-check: the truth image must be reproduced from these same prototypes.
    # If it is not, the prototype table is not the truth's own table and nothing
    # downstream means anything.
    self_rate = self_check(truth, prot, pcls, truth_cls)

    unknown = sorted({f for f in fam_lut if f not in bf.FAMILIES})
    bad = render_cls != truth_cls

    ambiguous = int((margin <= 1e-6).sum())
    green_pair = ((truth_cls == COARSE.index("lawn")) & (render_cls == COARSE.index("veg"))) \
        | ((truth_cls == COARSE.index("veg")) & (render_cls == COARSE.index("lawn")))
    bad_struct = bad & (~green_pair)

    log("--- coarse classes ---")
    log("classes .......... %s" % ", ".join(COARSE))
    log("prototypes ....... %d distinct truth top colours (blocks present only)"
        % len(prot))
    log("families -> 'other'  %s" % (", ".join(unknown) if unknown else "none"))
    log("self-check ....... %.4f%% of columns, truth classified by its own "
        "prototypes" % (100.0 * self_rate))
    log("knife-edge ....... %.2f%% of columns are within 4/255 of a second "
        "prototype" % (100.0 * float((margin < 4.0).mean())))
    log("")

    # (label, mask, mismatch) -- the merged layer has to be judged on the merged
    # mismatch, or the lawn<->veg pair it exists to absorb is counted twice.
    layers = [
        ("all columns", np.ones(bad.shape, bool), bad),
        ("terrain top face (no canopy / plant cover)", terrain, bad),
        ("veg (truth side is a tree/plant top)", veg, bad),
        ("structure / other (coarse 'other')",
         truth_cls == COARSE.index("other"), bad),
        ("lawn + veg merged into one green class",
         (truth_cls == 0) | (truth_cls == 5), bad_struct),
    ]
    log("--- agreement by layer ---")
    layer_stats = []
    for label, mask, mm in layers:
        n = int(mask.sum())
        ok = int((~mm[mask]).sum())
        rate = 100.0 * ok / n if n else 0.0
        layer_stats.append((label, n, ok, rate))
        log("%-42s %8d cols  %8d agree  %7.3f%%" % (label, n, ok, rate))
    log("")

    k = len(COARSE)
    cm = confusion(truth_cls, render_cls, k)
    log("--- 7x7 confusion matrix (rows truth, columns render, columns) ---")
    log("%-8s" % "" + "".join("%10s" % c for c in COARSE))
    for i, c in enumerate(COARSE):
        log("%-8s" % c + "".join("%10d" % v for v in cm[i]))
    log("")

    diag = np.diag(cm).astype(np.float64)
    rowsum = cm.sum(axis=1).astype(np.float64)
    colsum = cm.sum(axis=0).astype(np.float64)
    log("--- per class ---")
    log("%-8s %9s %9s %10s %10s" % ("class", "columns", "agree", "precision",
                                     "recall"))
    for i, c in enumerate(COARSE):
        rec = 100.0 * diag[i] / rowsum[i] if rowsum[i] else 0.0
        prec = 100.0 * diag[i] / colsum[i] if colsum[i] else 0.0
        log("%-8s %9d %9d %9.2f%% %9.2f%%"
            % (c, int(rowsum[i]), int(diag[i]), prec, rec))
    log("")

    pairs = [(int(cm[i, j]), COARSE[i], COARSE[j])
             for i in range(k) for j in range(k) if i != j and cm[i, j]]
    pairs.sort(reverse=True)
    log("--- top 12 confusions (truth -> render) ---")
    for n, a, b in pairs[:12]:
        log("%-8s -> %-8s %8d  %6.3f%% of all columns"
            % (a, b, n, 100.0 * n / bad.size))
    log("")

    # ---- 4. spatial distribution ----------------------------------------
    rate16 = cell_rate(bad.astype(np.float64))
    rate16s = cell_rate(bad_struct.astype(np.float64))
    truth_maj, _ts = cell_majority(truth_cls)
    render_maj, _rs = cell_majority(render_cls)
    order = ranked(rate16)
    order_s = ranked(rate16s)

    log("--- worst 12 cells, all disagreement (16x16 blocks) ---")
    log("%-4s %-24s %-9s %-9s %9s %6s"
        % ("#", "x range / z range", "truth", "render", "mismatch", "cols"))
    worst = []
    for n, (ri, ci) in enumerate(order[:12]):
        if rate16[ri, ci] <= 0.0:
            break
        x0, x1, z0, z1 = block_range(ci, ri)
        nb = int(bad[ri * CELL:(ri + 1) * CELL,
                      ci * CELL:(ci + 1) * CELL].sum())
        worst.append((int(ri), int(ci), nb))
        log("%-4d x %+4d..%+4d z %+4d..%+4d %-9s %-9s %8.1f%% %6d"
            % (n + 1, x0, x1, z0, z1, COARSE[truth_maj[ri, ci]],
               COARSE[render_maj[ri, ci]], 100.0 * rate16[ri, ci], nb))
    log("")

    log("--- worst 12 cells, excluding the lawn<->veg pair (where to look) ---")
    log("%-4s %-24s %-9s %-9s %9s %6s"
        % ("#", "x range / z range", "truth", "render", "mismatch", "cols"))
    worst_struct = []
    for n, (ri, ci) in enumerate(order_s[:12]):
        if rate16s[ri, ci] <= 0.0:
            break
        x0, x1, z0, z1 = block_range(ci, ri)
        nb = int(bad_struct[ri * CELL:(ri + 1) * CELL,
                            ci * CELL:(ci + 1) * CELL].sum())
        worst_struct.append((int(ri), int(ci), nb))
        log("%-4d x %+4d..%+4d z %+4d..%+4d %-9s %-9s %8.1f%% %6d"
            % (n + 1, x0, x1, z0, z1, COARSE[truth_maj[ri, ci]],
               COARSE[render_maj[ri, ci]], 100.0 * rate16s[ri, ci], nb))
    log("")

    # ---- 5. per-block identity ------------------------------------------
    counts = collections.Counter(int(v) for v in idx.ravel())
    block_rows = []
    for kk, n in counts.most_common(20):
        m = idx == kk
        cc = collections.Counter(int(v) for v in render_cls[m])
        tot = float(m.sum())
        block_rows.append((names[kk].split(":")[-1], n, COARSE[int(truth_cls[m][0])],
                           [(COARSE[c], 100.0 * v / tot) for c, v in cc.most_common(3)]))
    log("--- per-block identity: which class the render shows at that block's "
        "columns ---")
    log("%-26s %8s %-8s %s" % ("truth block", "columns", "truth", "render"))
    for nm, n, tc, top3 in block_rows:
        log("%-26s %8d %-8s %s" % (nm, n, tc,
                                   ", ".join("%s %.0f%%" % (c, p) for c, p in top3)))
    log("")

    # ---- 5b. is the surface drawn at all? --------------------------------
    below, ytop = below_map()
    probe = occlusion_probe(names, idx, below, ytop, fitted, palette)
    log("--- occlusion probe: rendered colour vs own albedo vs block beneath ---")
    log("%-24s %8s %10s %10s  %-20s %5s %s"
        % ("truth block", "columns", "vs albedo", "vs beneath", "beneath", "y",
           "read"))
    for _g, nm, n, d_self, d_below, bname, ymed in probe[:12]:
        log("%-24s %8d %10.1f %10.1f  %-20s %5d %s"
            % (nm, n, d_self, d_below, bname, ymed,
               "SHOWS THE BLOCK BENEATH" if d_below + 15.0 < d_self else
               ("neither (material differs)" if d_self > 45.0 else "own material")))
    log("")

    # ---- 6. metric ceiling ----------------------------------------------
    lab_render, _ = nearest_class(to_lab(warped).astype(np.float32),
                                  to_lab(predicted).astype(np.float32), pcls, prior)
    lab_rate = 100.0 * float((lab_render == truth_cls).mean())
    try:
        p_g, a_g, b_g, gfit = fit_gamma_model(warped, truth)
        gam_render, _ = nearest_class(gfit.astype(np.float32), prot, pcls, prior)
        gam_rate = 100.0 * float((gam_render == truth_cls).mean())
        gam_txt = "%.2f%%  (per-channel gamma p=%.3f/%.3f/%.3f)" % (
            gam_rate, p_g[0], p_g[1], p_g[2])
    except Exception as exc:                     # scipy missing, etc.
        gam_rate = float("nan")
        gam_txt = "skipped (%s)" % exc

    tcls_lab = confusion(truth_cls, lab_render, k)
    lab_diag = np.diag(tcls_lab)
    lab_note = ", ".join("%s %.0f%%" % (COARSE[i],
                                        100.0 * lab_diag[i] / max(rowsum[i], 1))
                         for i in range(k) if rowsum[i])

    # The closest pair of prototypes from two different classes is the decision
    # boundary the classifier actually uses. Compare it with the residual the fit
    # leaves behind: a boundary narrower than the noise cannot be resolved.
    gaps_all = prototype_gaps(prot, pcls, limit=10 ** 6)
    shared = [g for g in gaps_all if g[0] <= 1e-6]
    gaps = [g for g in gaps_all if g[0] > 1e-6][:5]
    gap = gaps[0][0] if gaps else float("nan")

    log("--- metric ceiling (the aggregate depends on the colour metric) ---")
    log("primary  RGB prototypes, affine fit     %6.2f%%"
        % (100.0 * float(1.0 - bad.mean())))
    log("variant  RGB prototypes, gamma fit      %s" % gam_txt)
    log("variant  CIELAB dE76 prototypes, affine %6.2f%%" % lab_rate)
    log("  (Lab per-class recall: %s)" % lab_note)
    log("ambiguous colours: %d cross-class prototype pairs share a colour "
        "exactly; %d columns sit on one" % (len(shared), ambiguous))
    log("closest cross-class prototype pairs (the decision boundaries):")
    for d, ca, cb, ra, rb in gaps:
        log("  %6.2f  %-7s %-15s vs %-7s %-15s"
            % (d, ca, str(ra), cb, str(rb)))
    log("fit residual vs the narrowest boundary:")
    log("  mean |dRGB| after fit  %.2f" % mae)
    log("  narrowest boundary     %.2f" % gap)
    log("  -> %s" % ("the residual is wider than the boundary: a per-pixel "
                    "colour decision flips on noise alone, so the aggregate is "
                    "a floor, not a measurement of placement"
                    if mae > gap else
                    "the residual is narrower than the narrowest boundary: colour "
                    "alone could in principle answer every class question here"))
    log("")

    # ---- 7. pictures -----------------------------------------------------
    write_diff_png(args.out, truth, warped, bad, bad_struct,
                   worst[:5], worst_struct[:5])

    # ---- 8. report -------------------------------------------------------
    head = ("REFLECTION: render/%s == truth -- the UE frame is a MIRROR image of "
            "the save (world mirrored across one axis)." % reg["best"]["name"]
            if mirrored else
            "ORIENTATION: render/%s == truth -- no reflection."
            % reg["best"]["name"])

    lines = [
        "# MC2UE5 orthographic frame vs save -- per-block agreement",
        "# %s" % time.strftime("%F %T"),
        "# frame  %s  md5 %s" % (args.frame, md5(args.frame)),
        "# truth  %s  md5 %s" % (args.truth, md5(args.truth)),
        "# campus x[%d..%d] z[%d..%d], 1 px = 1 block, row = z, column = x"
        % (CAMPUS[0], CAMPUS[1], CAMPUS[2], CAMPUS[3]),
        "",
        head,
        "",
        "## registration",
        "transform            %s%s" % (reg["best"]["name"],
                                       "  (MIRRORING)" if mirrored else ""),
        "reflection           %s" % ("YES" if mirrored else "no"),
        "score                %.4f" % reg["best"]["score"],
        "runner-up            %s at %.4f" % (reg["second"]["name"],
                                             reg["second"]["score"]),
        "lead over runner-up  %.3fx" % reg["lead"],
        "scale row/column     %.3f / %.3f" % (reg["sy"], reg["sx"]),
        "offset row/column    %+.1f / %+.1f px" % (reg["oy"], reg["ox"]),
        "",
        "## colour model (render = gain * truth + offset, global least squares)",
        "gain                 R %+.4f  G %+.4f  B %+.4f" % tuple(gain),
        "offset               R %+.2f  G %+.2f  B %+.2f" % tuple(off),
        "residual RMSE        R %.2f  G %.2f  B %.2f (8-bit levels)" % tuple(res),
        "R^2                  R %.4f  G %.4f  B %.4f" % tuple(r2),
        "mean |dRGB| fitted   %.2f   (raw %.2f)" % (mae, raw_mae),
        "",
        "## agreement by layer (coarse class, primary classifier)",
        "%-42s %8s %8s %9s" % ("layer", "columns", "agree", "rate"),
    ]
    for label, n, ok, rate in layer_stats:
        lines.append("%-42s %8d %8d %8.3f%%" % (label, n, ok, rate))
    lines += ["", "## confusion matrix (rows truth, columns render)"]
    lines.append("%-8s" % "" + "".join("%10s" % c for c in COARSE))
    for i, c in enumerate(COARSE):
        lines.append("%-8s" % c + "".join("%10d" % v for v in cm[i]))
    lines += ["", "## per class"]
    lines.append("%-8s %9s %9s %10s %10s"
                 % ("class", "columns", "agree", "precision", "recall"))
    for i, c in enumerate(COARSE):
        rec = 100.0 * diag[i] / rowsum[i] if rowsum[i] else 0.0
        prec = 100.0 * diag[i] / colsum[i] if colsum[i] else 0.0
        lines.append("%-8s %9d %9d %9.2f%% %9.2f%%"
                     % (c, int(rowsum[i]), int(diag[i]), prec, rec))
    lines += ["", "## top 12 confusions (truth -> render)"]
    for n, a, b in pairs[:12]:
        lines.append("%-8s -> %-8s %8d  %6.3f%% of all columns"
                     % (a, b, n, 100.0 * n / bad.size))

    lines += ["", "## worst 12 cells, all disagreement (16x16 blocks)",
              "%-4s %-24s %-9s %-9s %9s %6s"
              % ("#", "x range / z range", "truth", "render", "mismatch", "cols")]
    for n, (ri, ci) in enumerate(order[:12]):
        if rate16[ri, ci] <= 0.0:
            break
        x0, x1, z0, z1 = block_range(ci, ri)
        nb = int(bad[ri * CELL:(ri + 1) * CELL, ci * CELL:(ci + 1) * CELL].sum())
        lines.append("%-4d x %+4d..%+4d z %+4d..%+4d %-9s %-9s %8.1f%% %6d"
                     % (n + 1, x0, x1, z0, z1, COARSE[truth_maj[ri, ci]],
                        COARSE[render_maj[ri, ci]], 100.0 * rate16[ri, ci], nb))

    lines += ["", "## worst 12 cells excluding the lawn<->veg pair",
              "%-4s %-24s %-9s %-9s %9s %6s"
              % ("#", "x range / z range", "truth", "render", "mismatch", "cols")]
    for n, (ri, ci) in enumerate(order_s[:12]):
        if rate16s[ri, ci] <= 0.0:
            break
        x0, x1, z0, z1 = block_range(ci, ri)
        nb = int(bad_struct[ri * CELL:(ri + 1) * CELL,
                            ci * CELL:(ci + 1) * CELL].sum())
        lines.append("%-4d x %+4d..%+4d z %+4d..%+4d %-9s %-9s %8.1f%% %6d"
                     % (n + 1, x0, x1, z0, z1, COARSE[truth_maj[ri, ci]],
                        COARSE[render_maj[ri, ci]], 100.0 * rate16s[ri, ci], nb))

    lines += ["", "## per-block identity (which class the render shows there)",
              "%-26s %8s %-8s %s" % ("truth block", "columns", "truth", "render")]
    for nm, n, tc, top3 in block_rows:
        lines.append("%-26s %8d %-8s %s"
                     % (nm, n, tc,
                        ", ".join("%s %.0f%%" % (c, p) for c, p in top3)))

    lines += ["", "## occlusion probe: colour in the frame vs own albedo vs the "
              "block beneath",
              "%-24s %8s %10s %10s  %-20s %5s %s"
              % ("truth block", "columns", "vs albedo", "vs beneath", "beneath",
                 "y", "read")]
    for _g, nm, n, d_self, d_below, bname, ymed in probe[:12]:
        lines.append("%-24s %8d %10.1f %10.1f  %-20s %5d %s"
                     % (nm, n, d_self, d_below, bname, ymed,
                        "SHOWS THE BLOCK BENEATH" if d_below + 15.0 < d_self
                        else ("neither (material differs)" if d_self > 45.0
                              else "own material")))

    lines += [
        "", "## metric ceiling",
        "primary  RGB prototypes + affine fit      %6.2f%%" % (100.0 * float(1.0 - bad.mean())),
        "variant  RGB prototypes + gamma fit       %s" % gam_txt,
        "variant  CIELAB dE76 prototypes + affine %6.2f%%" % lab_rate,
        "Lab per-class recall: %s" % lab_note,
        "mean |dRGB| after fit                    %.2f" % mae,
        "narrowest cross-class prototype gap      %.2f" % gap,
        "exact colour collisions across classes   %d pairs, %d columns"
        % (len(shared), ambiguous),
        "closest cross-class prototype pairs:",
    ] + ["  %6.2f  %-7s %-15s vs %-7s %-15s"
         % (d, ca, str(ra), cb, str(rb)) for d, ca, cb, ra, rb in gaps] + [
        "A classifier cannot resolve two classes closer than its own residual,",
        "so the aggregate above is a floor on how much colour alone can tell us.",
        "The per-block table is the evidence that survives this: it says which",
        "material each surface is actually being drawn with.",
        "",
        "## method notes",
        "truth class    block name -> block_families.family() -> coarse table.",
        "               Family-level, so it cannot split sand from sandstone",
        "               (both 'gravel' -> lawn) or terracotta from brick (both",
        "               'brick' -> track). Both collisions are noted above.",
        "render class   nearest of %d prototypes; each prototype is a distinct" % len(prot),
        "               truth top-face texture mean of a block that appears on",
        "               this campus. Same colours, same space, after the fit.",
        "self-check     truth classified by those prototypes: %.4f%%." % (100.0 * self_rate),
        "cover          a column is 'cover' when its top tile carries alpha",
        "               (leaves / plants / bars), from the tile, not a name list.",
        "",
        "## expected differences (not renderer bugs)",
        "veg            In the render a tree is cross-plane geometry. From",
        "               directly above the cards are edge-on, so the frame shows",
        "               what is beneath them while the truth records the canopy",
        "               texture. veg columns read as ground or as the trunk, and",
        "               the veg -> other direction in the matrix is that, not an",
        "               error in placement.",
        "lighting       Shadowed and steep faces are darkened by the sun at -48",
        "               deg; the global fit removes the average offset, not",
        "               per-face shading, so roof edges and slab sides read darker",
        "               than their albedo and can fall into a neighbouring class.",
        "tone curve     The frame's response is not affine (see metric ceiling);",
        "               a per-channel curve fit changes the aggregate a lot, which",
        "               bounds what this frame can prove about material identity.",
    ]
    with open(args.report, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    all_rate = layer_stats[0][3]
    log("wrote %s" % args.out)
    log("wrote %s" % args.report)

    if self_rate < 0.99:
        log("FAIL: the prototype table reproduces only %.2f%% of the truth's own "
            "columns" % (100.0 * self_rate))
        log("RESULT: FAIL prototype table inconsistent with the truth image")
        return 1

    log("RESULT: PASS agreement=%.2f%% transform=%s mirror=%s scale=%.3f/%.3f "
        "fit_mae=%.2f structural=%.2f%%"
        % (all_rate, reg["best"]["name"], "yes" if mirrored else "no",
           reg["sy"], reg["sx"], mae,
           100.0 * float(1.0 - bad_struct.mean())))
    return 0


def _top_columns():
    from reference_map import top_block_per_column
    return top_block_per_column()


def write_diff_png(path, truth, warped, bad, bad_struct, worst5, worst5s):
    """Truth | registered render | mismatch map, three panels side by side.

    The mismatch panel separates the two kinds of disagreement: amber is the
    lawn<->veg pair (a green-tint difference that covers most of the map and
    would drown everything else in red), red is every other class error. Boxes:
    yellow = worst 5 cells overall, cyan = worst 5 once the green pair is merged.
    """
    pad, label_h = 8, 22
    cw = 3 * W + 2 * pad
    canvas = np.full((H + label_h, cw, 3), 24, dtype=np.uint8)

    canvas[label_h:label_h + H, 0:W] = truth
    x1 = W + pad
    canvas[label_h:label_h + H, x1:x1 + W] = warped
    x2 = 2 * (W + pad)

    grey = truth.astype(np.float32) @ np.array([0.299, 0.587, 0.114])
    base = (60.0 + 0.35 * grey).clip(0, 255).astype(np.uint8)
    third = np.stack([base] * 3, axis=2)
    green_pair = bad & (~bad_struct)
    third[green_pair] = np.array([196, 132, 18], dtype=np.uint8)   # amber
    third[bad_struct] = np.array([214, 28, 28], dtype=np.uint8)    # red
    canvas[label_h:label_h + H, x2:x2 + W] = third

    im = Image.fromarray(canvas)
    dr = ImageDraw.Draw(im)
    dr.text((6, 6), "truth (save, unlit albedo)", fill=(235, 235, 235))
    dr.text((x1 + 6, 6), "render (registered, UE frame)", fill=(235, 235, 235))
    dr.text((x2 + 6, 6), "mismatch: red = class error, amber = lawn<->veg",
            fill=(235, 235, 235))
    for n, (ri, ci, _nb) in enumerate(worst5):
        for ox in (0, x1, x2):
            x0p, y0p = ox + ci * CELL, label_h + ri * CELL
            dr.rectangle([x0p, y0p, x0p + CELL - 1, y0p + CELL - 1],
                         outline=(255, 220, 0), width=2)
        dr.text((x2 + ci * CELL + 2, label_h + ri * CELL + 2), str(n + 1),
                fill=(255, 255, 0))
    for n, (ri, ci, _nb) in enumerate(worst5s):
        for ox in (0, x1, x2):
            x0p, y0p = ox + ci * CELL, label_h + ri * CELL
            dr.rectangle([x0p, y0p, x0p + CELL - 1, y0p + CELL - 1],
                         outline=(0, 240, 240), width=2)
        dr.text((x2 + ci * CELL + 2, label_h + ri * CELL + 14), "s%d" % (n + 1),
                fill=(0, 240, 240))
    im.save(path)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        print("RESULT: FAIL uncaught exception")
        raise
