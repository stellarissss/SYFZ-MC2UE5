# -*- coding: utf-8 -*-
"""
check_ortho_frame.py -- decide, with numbers, whether out/ortho_topdown.png is
the frame out/ref_map.png says it should be.

Five questions, answered separately, because each of them has failed silently in
this project before:

1. **Which way is the image turned?** The render target's axis order is not a
   matter of taste: a UE camera's screen-right is local +Y and screen-up is
   local +Z, so yaw decides which world axis is horizontal, and the engine's PNG
   export may flip rows on top of that. Every candidate axis transform is scored
   against ref_map and the winner is pinned into
   `tools/ortho_topdown.py:AXIS_TRANSFORM`. The score is the mean of two
   zero-parameter colour-signature correlations at zero shift: a per-row
   signature (mean G-B, mean R-G) and the same per column. Rows alone cannot
   tell identity from flip_h (reversing a row does not change its mean) and
   columns alone cannot tell identity from flip_v; together they pin all four
   transforms that keep 448x768. The 4x4-block RGB correlation and the
   nearest-palette colour-class agreement are reported alongside as independent
   checks. The winner has to win by at least 1.15x or the frame is not trusted.

   Run it on `out/ortho_topdown_raw.png` to *derive* the transform, and on
   `out/ortho_topdown.png` to confirm the transform actually got applied
   (expected: flip_v on the raw frame, identity on the published one).

2. **Is it 1:1?** A frame can be right-side-up and still be zoomed. The vertical
   scale and offset are fitted directly by cross-correlating a per-row colour
   signature against ref_map's; the horizontal scale is checked by comparing the
   pitch's column extent. A correct frame fits at scale 1.00 +/- 0.03, offset
   +/- 4 px.

3. **Is the picture real?** Size, mean colour, pure-black share, distinct
   colours, how much of the frame is a single flat colour (a stalled render is
   uniform), and the mean absolute difference against ref_map on an 8x8 block
   grid -- the number that says "same campus", not "same average colour". Then
   the same grid is compared against `out/truth_colour.png`, the unlit per-block
   albedo map, which is the right reference for *colour* (ref_map is a collapsed
   map palette whose own difference to the texture truth is ~29/255).

4. **Are the landmarks where ref_map says?** The running track (a big red
   terracotta ring) and the pitch (bright green, inside it) are located in both
   images by hue family, and compared by IoU *inside ref_map's bounding box* so
   that unrelated red or green elsewhere cannot rescue a wrong frame. The ring is
   also required to be hollow.

Run with the project venv (needs PIL and numpy):

    python tools/check_ortho_frame.py
    python tools/check_ortho_frame.py --frame out/ortho_topdown_raw.png
"""

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAMPUS = (-144, 303, -544, 223)
W, H = 448, 768
CELL = 4                     # block-vote cell, -> 112 x 192


# --------------------------------------------------------------------------- #
# io
# --------------------------------------------------------------------------- #

def load(path):
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    a = np.asarray(im)
    if a.shape[:2] != (H, W):
        raise SystemExit("%s is %dx%d, expected %dx%d"
                         % (path, a.shape[1], a.shape[0], W, H))
    return a.astype(np.int16)


def palette():
    entries = json.load(open(os.path.join(ROOT, "out", "ref_map_legend.json")))
    rgb = np.array([e["rgb"] for e in entries], dtype=np.int16)
    chroma = rgb / np.maximum(rgb.sum(axis=1, keepdims=True), 1)
    return rgb, chroma, entries


def class_map(img, chroma):
    """-> (H, W) index of the nearest palette entry in chromaticity."""
    f = img.astype(np.float32)
    s = f.sum(axis=2, keepdims=True)
    c = np.where(s > 8.0, f / np.maximum(s, 1.0), -1.0)
    d = ((c[:, :, None, :] - chroma[None, None, :, :]) ** 2).sum(axis=3)
    d[c[:, :, 0] < 0] = 1e9
    return d.argmin(axis=2)


def cell_majority(cls, k):
    h, w = cls.shape
    oh = np.zeros((h, w, k), dtype=np.float32)
    r, c = np.indices((h, w))
    oh[r, c, cls] = 1.0
    cnt = oh.reshape(h // CELL, CELL, w // CELL, CELL, k).sum(axis=(1, 3))
    return cnt.argmax(axis=2), cnt.max(axis=2) / float(CELL * CELL)


def cell_mean(img):
    h, w = img.shape[:2]
    return img.reshape(h // CELL, CELL, w // CELL, CELL, 3).mean(axis=(1, 3))


def pearson(x, y):
    x = x.ravel().astype(np.float64)
    y = y.ravel().astype(np.float64)
    x = x - x.mean()
    y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 0 else 0.0


# --------------------------------------------------------------------------- #
# the 8 axis transforms
# --------------------------------------------------------------------------- #

TRANSFORMS = [
    ("identity",   lambda a: a),
    ("flip_h",     lambda a: a[:, ::-1]),
    ("flip_v",     lambda a: a[::-1]),
    ("rot180",     lambda a: a[::-1, ::-1]),
    ("rot90",      lambda a: np.rot90(a, 1)),
    ("rot270",     lambda a: np.rot90(a, 3)),
    ("transpose",  lambda a: a.transpose(1, 0, 2)),
    ("antitrans",  lambda a: np.rot90(a, 2).transpose(1, 0, 2)),
]


def fit(a):
    """Nearest-neighbour fit to 448x768 when a transform changed the aspect."""
    if a.shape[:2] == (H, W):
        return a
    return np.asarray(Image.fromarray(a.astype(np.uint8))
                      .resize((W, H), Image.NEAREST)).astype(np.int16)


# --------------------------------------------------------------------------- #
# 1:1 evidence
# --------------------------------------------------------------------------- #

def row_signature(a):
    f = a.astype(np.float64)
    return np.stack([f[:, :, 1].mean(axis=1) - f[:, :, 2].mean(axis=1),
                     f[:, :, 0].mean(axis=1) - f[:, :, 1].mean(axis=1)], axis=1)


def row_corr_1to1(a, ref):
    """Pearson r of the per-row colour signature at zero shift.

    Independent of the block score: it asks whether row r of the frame is the
    same *kind* of row as row r of ref_map. A row flip drives it negative, and
    unlike fit_rows() there is no free scale/offset to hide behind.
    """
    return _sig_corr(a, ref, axis=1)


def col_corr_1to1(a, ref):
    """Same for columns -- a horizontal flip drives *this* one negative.

    Needed because the row signature is invariant to a left-right flip (row
    means do not change when a row is reversed), so rows alone cannot tell
    identity from flip_h. Rows plus columns pin all four 448x768 transforms.
    """
    return _sig_corr(a, ref, axis=0)


def _sig_corr(a, ref, axis):
    def sig(img):
        f = img.astype(np.float64)
        s = np.stack([f[:, :, 1].mean(axis=axis) - f[:, :, 2].mean(axis=axis),
                      f[:, :, 0].mean(axis=axis) - f[:, :, 1].mean(axis=axis)],
                     axis=1)
        s = s - s.mean(axis=0)
        sd = s.std(axis=0)
        sd[sd == 0] = 1.0
        return s / sd

    x, y = sig(a), sig(ref)
    if x.shape != y.shape:                 # aspect-rotating transforms
        return 0.0
    return float((x * y).mean())


def fit_rows(frame, ref):
    """-> (corr, scale, offset) best fitting ref_row = scale*row + offset.

    Uses the per-row colour signature (G-B and R-G), standardised per image, so
    brightness and exposure drop out and only the vertical arrangement of
    grass / track / concrete matters.
    """
    def norm(p):
        p = p - p.mean(axis=0)
        s = p.std(axis=0)
        s[s == 0] = 1.0
        return p / s

    fr = norm(row_signature(frame))
    rr = norm(row_signature(ref))
    rows = np.arange(float(H))
    best = (-2.0, 1.0, 0)
    for scale in np.arange(0.85, 1.85, 0.005):
        idx = scale * rows
        if idx[-1] >= H:
            continue
        for off in range(-160, 161, 2):
            j = idx + off
            if j[0] < 0 or j[-1] > H - 1:
                continue
            cand = np.stack([np.interp(j, rows, fr[:, 0]),
                             np.interp(j, rows, fr[:, 1])], axis=1)
            c = (cand - cand.mean(axis=0)) / np.maximum(cand.std(axis=0), 1e-6)
            sc = float((c * rr).mean())
            if sc > best[0]:
                best = (sc, float(scale), int(off))
    return best


# --------------------------------------------------------------------------- #
# landmark masks by hue family (they survive a different albedo / exposure)
# --------------------------------------------------------------------------- #

def hue_masks(a):
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    red = (r > 100) & (r - g > 40) & (r - b > 30)
    green = (g > 110) & (g - r > 10) & (g - b > 55)
    return {"red": red, "green": green}


def bbox_of(mask):
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    return (int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max()))


def dilate(bb, pad, shape):
    x0, x1, y0, y1 = bb
    return (max(0, x0 - pad), min(shape[1] - 1, x1 + pad),
            max(0, y0 - pad), min(shape[0] - 1, y1 + pad))


def local_iou(a, b, bb):
    x0, x1, y0, y1 = bb
    sa = a[y0:y1 + 1, x0:x1 + 1]
    sb = b[y0:y1 + 1, x0:x1 + 1]
    inter = np.logical_and(sa, sb).sum()
    union = np.logical_or(sa, sb).sum()
    return float(inter) / float(union) if union else 0.0


def contrast(a, mask, stat):
    """Mean of `stat` inside vs outside `mask` -- does the frame agree with the
    landmark's *position* even when its colour is a different shade?"""
    f = a.astype(np.float64)
    v = stat(f)
    inside = float(v[mask].mean()) if mask.any() else 0.0
    outside = float(v[~mask].mean()) if (~mask).any() else 0.0
    return inside, outside


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default=os.path.join(ROOT, "out",
                                                    "ortho_topdown.png"))
    ap.add_argument("--ref", default=os.path.join(ROOT, "out", "ref_map.png"))
    ap.add_argument("--log", default=os.path.join(
        os.path.dirname(ROOT), "logs", "check_ortho_frame.txt"))
    args = ap.parse_args()

    lines = []

    def say(s=""):
        lines.append(str(s))
        print(s)
        try:
            os.makedirs(os.path.dirname(args.log), exist_ok=True)
            with open(args.log, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    say("=== check_ortho_frame start %s ===" % time.strftime("%F %T"))
    say("frame: %s" % args.frame)
    say("ref  : %s" % args.ref)

    ref = load(args.ref)
    frame = load(args.frame)
    palette_rgb, palette_chroma, entries = palette()
    k = len(palette_rgb)
    ref_cls, ref_cls_conf = cell_majority(class_map(ref, palette_chroma), k)
    ref_means = cell_mean(ref).astype(np.float64)
    ref_std = (ref_means - ref_means.reshape(-1, 3).mean(axis=0)) / \
              np.maximum(ref_means.reshape(-1, 3).std(axis=0), 1e-6)
    ref_std = ref_std.reshape(ref_cls.shape[0], ref_cls.shape[1], 3)

    # ---- 1. orientation ------------------------------------------------
    say("")
    say("--- orientation: 8 axis transforms scored against ref_map ---")
    say("%-11s %-8s %8s %8s %8s %8s %8s"
        % ("transform", "dims", "class", "r_block", "r_row", "r_col", "score"))
    results = []
    for name, fn in TRANSFORMS:
        t = fn(frame)
        dims = "%dx%d" % (t.shape[1], t.shape[0])
        t = fit(t)
        cls, _conf = cell_majority(class_map(t, palette_chroma), k)
        agree = float((cls == ref_cls).mean())
        means = cell_mean(t).astype(np.float64)
        means = (means - means.reshape(-1, 3).mean(axis=0)) / \
                np.maximum(means.reshape(-1, 3).std(axis=0), 1e-6)
        means = means.reshape(ref_std.shape)
        r = float(np.mean([pearson(means[:, :, i], ref_std[:, :, i])
                           for i in range(3)]))
        rr = row_corr_1to1(t, ref)
        rc = col_corr_1to1(t, ref)
        # Score = the two zero-parameter signature correlations, mapped from
        # [-1,1] to [0,1] so that "second best / best" is a meaningful margin.
        # Rows alone cannot tell identity from flip_h (reversing a row does not
        # change its mean), columns alone cannot tell identity from flip_v;
        # together they pin all four 448x768 transforms. r_block is reported as
        # the independent colour-class check.
        score = 0.25 * ((1.0 + rr) + (1.0 + rc))
        results.append((score, name, dims, agree, r, rr, rc))
        say("%-11s %-8s %8.4f %8.4f %8.4f %8.4f %8.4f"
            % (name, dims, agree, r, rr, rc, score))
    results.sort(reverse=True)
    # The four rotations that change the aspect can only be compared after a
    # resample to 448x768, which smears the block means; they are a sanity
    # cross-check, not candidates. The real decision is among the four
    # dimension-preserving transforms.
    square = [x for x in results if x[1] in
              ("identity", "flip_h", "flip_v", "rot180")]
    best, second = square[0], square[1]
    margin = best[0] / second[0] if second[0] > 0 else float("inf")
    say("winner: %s (score %.4f)  runner-up: %s (%.4f)  margin %.3fx"
        % (best[1], best[0], second[1], second[0], margin))
    say("(margin is among the four transforms that preserve 448x768; the four "
        "that rotate the aspect are shown only as a cross-check)")
    orientation_ok = margin >= 1.15
    if not orientation_ok:
        say("FAIL: the winning transform is not distinguishable (margin %.3f, "
            "winner %s)" % (margin, best[1]))

    # ---- 2. is it 1:1? -------------------------------------------------
    say("")
    say("--- 1:1 check (ref_row = scale*row + offset, from row signatures) ---")
    turned = fit(TRANSFORMS[[n for n, _ in TRANSFORMS].index(best[1])][1](frame))
    corr, scale, off = fit_rows(turned, ref)
    say("row-signature fit: corr %.4f  scale %.3f  offset %+d px  "
        "(1:1 means scale 1.00 +/- 0.03, |offset| <= 4)"
        % (corr, scale, off))
    # horizontal scale from the pitch's column extent (both images, hue family)
    fm = hue_masks(turned)
    rm = hue_masks(ref)
    idx = {e["colour_name"]: i for i, e in enumerate(entries)}
    pitch_ci = idx.get("minecraft:lime_wool")
    hw_ok = True
    if pitch_ci is not None:
        pitch_mask = (class_map(ref, palette_chroma) == pitch_ci)
        pwin = dilate(bbox_of(pitch_mask), 6, ref.shape)
        # the frame's greens are its own albedo, not the map palette, so locate
        # them by hue family -- but only inside ref's pitch window, otherwise
        # the surrounding grass swamps the extent
        sub = np.zeros_like(fm["green"])
        sub[pwin[2]:pwin[3] + 1, pwin[0]:pwin[1] + 1] = \
            fm["green"][pwin[2]:pwin[3] + 1, pwin[0]:pwin[1] + 1]
        pitch_ref_bb = bbox_of(pitch_mask)
        pitch_fr_bb = bbox_of(sub)
        say("pitch (lime, ref palette) columns: ref %s  frame %s"
            % ((pitch_ref_bb[0], pitch_ref_bb[1]) if pitch_ref_bb else None,
               (pitch_fr_bb[0], pitch_fr_bb[1]) if pitch_fr_bb else None))
        if pitch_fr_bb:
            rw = pitch_ref_bb[1] - pitch_ref_bb[0] + 1
            fw = pitch_fr_bb[1] - pitch_fr_bb[0] + 1
            say("pitch width: ref %d px, frame %d px, ratio %.3f"
                % (rw, fw, fw / float(rw)))
            hw_ok = abs(fw / float(rw) - 1.0) <= 0.06
        else:
            say("pitch width: no green inside ref's pitch window")
            hw_ok = False
    one_to_one = (abs(scale - 1.0) <= 0.03 and abs(off) <= 4 and hw_ok
                  and corr >= 0.60)
    if not one_to_one:
        say("FAIL: the frame is not 1:1 with ref_map")

    # ---- 3. quantitative ----------------------------------------------
    say("")
    say("--- frame statistics ---")
    flat = frame.reshape(-1, 3).astype(np.float64)
    mean = flat.mean(axis=0)
    refmean = ref.reshape(-1, 3).mean(axis=0)
    black = float((frame.reshape(-1, 3).max(axis=1) <= 2).mean())
    distinct = len(np.unique(frame.reshape(-1, 3), axis=0))
    fcls, _ = cell_majority(class_map(frame, palette_chroma), k)
    vals, counts = np.unique(fcls, return_counts=True)
    frame_modal = float(counts.max()) / float(fcls.size)
    say("size            %dx%d  (need 448x768)  %s"
        % (frame.shape[1], frame.shape[0], "OK"
           if frame.shape[:2] == (H, W) else "WRONG"))
    say("mean rgb        (%.1f, %.1f, %.1f)   ref (%.1f, %.1f, %.1f)"
        % (mean[0], mean[1], mean[2], refmean[0], refmean[1], refmean[2]))
    say("pure black      %.4f%%  (limit 1%%)" % (100.0 * black))
    say("distinct colours %d  (need >= 200)" % distinct)
    say("modal class     %.1f%% of 4x4 cells (a stalled render is ~100%%)"
        % (100.0 * frame_modal))
    f8 = frame.reshape(H // 8, 8, W // 8, 8, 3).mean(axis=(1, 3))
    r8 = ref.reshape(H // 8, 8, W // 8, 8, 3).mean(axis=(1, 3))
    mad = float(np.abs(f8 - r8).mean())
    lum = lambda a: 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]
    lmad = float(np.abs(lum(f8) - lum(r8)).mean())
    say("8x8 block MAD   %.2f /255  (per-channel mean |frame - ref|)" % mad)
    say("8x8 luma MAD    %.2f /255" % lmad)
    say("8x8 mean rgb    frame (%.1f, %.1f, %.1f)  ref (%.1f, %.1f, %.1f)"
        % (f8[..., 0].mean(), f8[..., 1].mean(), f8[..., 2].mean(),
           r8[..., 0].mean(), r8[..., 1].mean(), r8[..., 2].mean()))
    real = black < 0.01 and distinct >= 200 and frame_modal < 0.85

    # ---- 3b. colour against the voxel-derived truth map ------------------
    # out/truth_colour.png is the per-block mean of the top solid block's 16x16
    # texture (unlit albedo, same 448x768, row=z). It is the right thing to
    # compare *colour* against: ref_map is a collapsed map palette whose own
    # mean absolute difference to the texture truth is ~29/255. A render is lit
    # and shadowed, so a systematic gap is expected; what matters is whether the
    # per-block variation lines up (r) and how big the gap is.
    say("")
    say("--- colour vs out/truth_colour.png (unlit per-block albedo) ---")
    truth_path = os.path.join(ROOT, "out", "truth_colour.png")
    colour_stats = None
    if not os.path.exists(truth_path):
        say("truth_colour.png not found -- skipped")
    else:
        truth = load(truth_path)
        t8 = truth.reshape(H // 8, 8, W // 8, 8, 3).mean(axis=(1, 3))
        d = f8 - t8
        mad_t = float(np.abs(d).mean())
        bias_ = d.reshape(-1, 3).mean(axis=0)
        rs = []
        for i in range(3):
            rs.append(pearson(f8[:, :, i] * 1.0, t8[:, :, i] * 1.0))
        r_mean = float(np.mean(rs))
        per_block = np.abs(d).mean(axis=2)
        close = float((per_block <= 40.0).mean())
        say("8x8 block MAD  %.2f /255   (ref_map's own MAD vs truth is ~28.7)"
            % mad_t)
        say("signed bias    frame - truth = (%+.1f, %+.1f, %+.1f)"
            % (bias_[0], bias_[1], bias_[2]))
        say("Pearson r      R %.3f  G %.3f  B %.3f  (mean %.3f)"
            % (rs[0], rs[1], rs[2], r_mean))
        say("blocks within 40/255 of truth: %.1f%%" % (100.0 * close))
        colour_stats = (mad_t, r_mean, close)

    # ---- 4. landmarks --------------------------------------------------
    say("")
    say("--- landmarks, compared inside ref_map's own bounding box ---")
    say("%-26s %-30s %8s %8s %8s"
        % ("landmark", "bbox ref -> frame", "IoU", "in", "out"))
    landmark_ok = True
    checks = [
        ("running track (red ring)", "red", "minecraft:pink_terracotta", 0.35),
        ("pitch (bright green)", "green", "minecraft:lime_wool", 0.30),
    ]
    idx = {e["colour_name"]: i for i, e in enumerate(entries)}
    track_ref_bb = None
    for label, fam, colour, need in checks:
        ci = idx.get(colour)
        if ci is None:
            say("  %-24s not in the palette, skipped" % label)
            continue
        refmask = (class_map(ref, palette_chroma) == ci)
        win = dilate(bbox_of(refmask), 6, ref.shape)
        if "track" in label:
            stat = lambda f: f[:, :, 0] - 0.5 * (f[:, :, 1] + f[:, :, 2])
        else:
            stat = lambda f: f[:, :, 1] - 0.5 * (f[:, :, 0] + f[:, :, 2])
        inside, outside = contrast(turned, refmask, stat)
        fmask = fm[fam]
        iou = local_iou(refmask, fmask, win)
        rf = bbox_of(refmask)
        ff = bbox_of(fmask)
        if "track" in label:
            track_ref_bb = rf
        say("%-26s %s -> %s  %.3f %8.1f %8.1f"
            % (label, rf, ff, iou, inside, outside))
        if iou < need:
            landmark_ok = False
            say("  FAIL: %s IoU %.3f < %.2f" % (label, iou, need))
        if inside <= outside:
            landmark_ok = False
            say("  FAIL: %s is not more %s inside ref's mask than outside"
                % (label, fam))
    # the track has to be a ring, not a disc. Centre it on ref's terracotta
    # mask (the ring itself), not on the hue-red mask, which also picks up red
    # roofs elsewhere on the map and moves the centre off the oval.
    rb = track_ref_bb or bbox_of(rm["red"])
    if rb:
        cx, cy = (rb[0] + rb[1]) // 2, (rb[2] + rb[3]) // 2
        box = fm["red"][max(0, cy - 24):cy + 24, max(0, cx - 24):cx + 24]
        share = float(box.mean())
        say("ring hollowness: ref track centre (%d,%d) is %.1f%% red in the frame"
            % (cx, cy, 100.0 * share))
        if share > 0.30:
            landmark_ok = False

    # spot readings, so a passing verdict can be eyeballed
    say("")
    say("--- spot readings: (col,row) -> block(x,z) -> frame rgb / ref rgb ---")
    for col, row, why in [(192, 296, "track centre / pitch"),
                          (30, 300, "west of track"),
                          (400, 100, "north-east"),
                          (60, 380, "south-west"),
                          (300, 420, "south pitch edge")]:
        fr = turned[row, col]
        rr = ref[row, col]
        say("  (%3d,%3d) blk(%4d,%4d) frame (%3d,%3d,%3d) ref (%3d,%3d,%3d)  %s"
            % (col, row, CAMPUS[0] + col, CAMPUS[2] + row,
               fr[0], fr[1], fr[2], rr[0], rr[1], rr[2], why))

    base = os.path.basename(args.frame)
    ok = (frame.shape[:2] == (H, W) and real and one_to_one and orientation_ok
          and landmark_ok)
    say("")
    if ok:
        say("RESULT: PASS %s 448x768 transform=%s margin=%.2fx "
            "scale=%.3f off=%+d corr=%.2f blockMAD=%.1f black=%.3f%% "
            "landmarks=ok truthMAD=%s truthr=%s"
            % (base, best[1], margin, scale, off, corr, mad, 100.0 * black,
               "%.1f" % colour_stats[0] if colour_stats else "n/a",
               "%.3f" % colour_stats[1] if colour_stats else "n/a"))
    else:
        say("RESULT: FAIL %s size=%s real=%s one_to_one=%s orientation=%s "
            "landmarks=%s winner=%s"
            % (base, frame.shape[:2], real, one_to_one, orientation_ok,
               landmark_ok, best[1]))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
