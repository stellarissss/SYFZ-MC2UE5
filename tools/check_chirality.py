# -*- coding: utf-8 -*-
"""
check_chirality.py -- decide, offline, whether the built UE campus is a MIRROR
of the save's block grid.

Input
-----
  out/chirality_grab.png    window grab of the PIE window, camera pose from
                            out/chirality_pose.json (written by
                            tools/chirality_probe.py)
  out/truth_colour.png      448 x 768, col = +block_x, row = +block_z, the
                            save's own top-down truth (no UE content at all)
  out/ortho_topdown.png     the published 448 x 768 ortho frame (flip_v applied)
  out/ortho_topdown_raw.png the same frame before the flip was applied

Method
------
1. Reproject the grab onto the truth grid *analytically*. A nadir camera whose
   image plane is parallel to the ground sees the ground plane as a pure scale
   (no keystone, no perspective distortion), so the reprojection is one crop
   plus one area-average resize. The crop rectangle and the scale come from the
   recorded pose:

       px_per_cm = (W/2) / (tan(hfov/2) * height_above_plane)
       u = W/2 + (P - cam)·right * px_per_cm
       v = H/2 - (P - cam)·up    * px_per_cm

   The pose is exact (a spawned CameraActor on a static level), so this is a
   measurement, not a fit. Two parity-neutral corrections are still allowed and
   are logged when they fire: a constant translation (window client offset) and
   a constant scale (plane height / FOV rounding). Neither can turn a rotation
   into a reflection, which is the only thing being decided here.

2. Score all eight axis transforms against out/truth_colour.png with the same
   zero-parameter statistics tools/check_ortho_frame.py uses (per-row and
   per-column colour-signature correlation, plus the 4x4-block RGB Pearson r as
   an independent colour check). The winner has to beat the runner-up by >= 1.15x
   among the four transforms that preserve 448 x 768, or the answer is "cannot
   tell" -- not a conclusion.

3. Verdict, from the winner's *parity*:

       identity / rot90 / rot180 / rot270   -> the world is NOT mirrored
       flip_h / flip_v / transpose / antitranspose -> the world IS mirrored

   The camera pose fixes which rotation is expected, so the log also prints the
   pose's own prediction and a mismatch is called out.

4. Cross-check against out/ortho_topdown.png (expect identity if the published
   frame is the flipped raw frame) and against out/ortho_topdown_raw.png (expect
   flip_v: that is the export's vertical flip, isolated with no world change in
   between).

Run with the project venv (needs numpy and PIL):

    python tools/check_chirality.py
    python tools/check_chirality.py --grab out/chirality_grab_b.png --tag B
"""

import argparse
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "out")

CAMPUS = (-144, 303, -544, 223)      # blocks, inclusive
NX, NZ = CAMPUS[1] - CAMPUS[0] + 1, CAMPUS[3] - CAMPUS[2] + 1   # 448 x 768
BLOCK_CM = 100.0
GROUND_Z = 300.0                     # the plane the campus top surface sits on

ROTATIONS = ("identity", "rot90", "rot180", "rot270")
MIRRORS = ("flip_h", "flip_v", "transpose", "antitranspose")


# --------------------------------------------------------------------------- #
# the eight axis transforms
# --------------------------------------------------------------------------- #

def apply_transform(a, name):
    if name == "identity":
        return a
    if name == "flip_h":
        return a[:, ::-1]
    if name == "flip_v":
        return a[::-1]
    if name == "rot180":
        return a[::-1, ::-1]
    if name == "rot90":
        return np.rot90(a, 1)
    if name == "rot270":
        return np.rot90(a, 3)
    if name == "transpose":
        return a.transpose(1, 0, 2)
    if name == "antitranspose":
        return np.rot90(a, 2).transpose(1, 0, 2)
    raise ValueError(name)


TRANSFORMS = ROTATIONS + MIRRORS


def fit(a):
    if a.shape[:2] == (NZ, NX):
        return a
    return np.asarray(Image.fromarray(a.astype(np.uint8))
                      .resize((NX, NZ), Image.BOX)).astype(np.int16)


# --------------------------------------------------------------------------- #
# statistics
# --------------------------------------------------------------------------- #

def signature(a, axis):
    f = a.astype(np.float64)
    s = np.stack([f[:, :, 1].mean(axis=axis) - f[:, :, 2].mean(axis=axis),
                  f[:, :, 0].mean(axis=axis) - f[:, :, 1].mean(axis=axis)],
                 axis=1)
    s = s - s.mean(axis=0)
    sd = s.std(axis=0)
    sd[sd == 0] = 1.0
    return s / sd


def sig_corr(a, b, axis):
    x, y = signature(a, axis), signature(b, axis)
    if x.shape != y.shape:
        return 0.0
    return float((x * y).mean())


def pearson(x, y):
    x = x.ravel().astype(np.float64)
    y = y.ravel().astype(np.float64)
    x -= x.mean()
    y -= y.mean()
    d = math.sqrt(float((x * x).sum()) * float((y * y).sum()))
    return float((x * y).sum() / d) if d > 0 else 0.0


def cell_mean(img, cell=4):
    h, w = img.shape[:2]
    return img.reshape(h // cell, cell, w // cell, cell, 3).mean(axis=(1, 3))


def block_r(a, b):
    ma, mb = cell_mean(a).astype(np.float64), cell_mean(b).astype(np.float64)
    out = []
    for i in range(3):
        x, y = ma[:, :, i], mb[:, :, i]
        if x.std() == 0 or y.std() == 0:
            out.append(0.0)
        else:
            out.append(pearson(x, y))
    return float(np.mean(out))


def score_frame(a, ref):
    """-> (score, r_block, r_row, r_col) for one already-transformed frame."""
    t = fit(a)
    return (0.25 * ((1.0 + sig_corr(t, ref, 1)) + (1.0 + sig_corr(t, ref, 0))),
            block_r(t, ref), sig_corr(t, ref, 1), sig_corr(t, ref, 0))


# --------------------------------------------------------------------------- #
# geometry
# --------------------------------------------------------------------------- #

def camera_basis(pitch_deg, yaw_deg):
    """(forward, right, up) for a UE rotator with roll = 0."""
    p, y = math.radians(pitch_deg), math.radians(yaw_deg)
    forward = np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y),
                        math.sin(p)])
    right = np.array([-math.sin(y), math.cos(y), 0.0])
    up = np.cross(right, forward)
    return forward, right, up


def project(pose, w, h):
    """-> (px_per_cm, right, up, centre) for the ground plane."""
    cam = pose["camera"]
    fov = cam.get("fov_read") or cam.get("fov_expected") or 90.0
    height = float(cam["z"]) - GROUND_Z
    half = (w / 2.0) / math.tan(math.radians(float(fov)) / 2.0)
    px_per_cm = half / height
    _, right, up = camera_basis(float(cam["pitch"]), float(cam["yaw"]))
    return px_per_cm, right, up, np.array([float(cam["x"]), float(cam["y"])])


def truth_world(cols, rows):
    """Truth pixel centres -> world (X, Y) cm on the ground plane."""
    bx = CAMPUS[0] + cols + 0.5
    bz = CAMPUS[2] + rows + 0.5
    return BLOCK_CM * bx, -BLOCK_CM * bz


def content_bbox(img, cell=8):
    """bbox of everything that is not the modal (sky) colour, in pixels."""
    a = img.astype(np.int16)
    h, w = a.shape[:2]
    hh, ww = h // cell * cell, w // cell * cell
    cells = a[:hh, :ww].reshape(hh // cell, cell, ww // cell, cell, 3).mean(axis=(1, 3))
    key = (cells // 16).astype(np.int32)
    flat = key.reshape(-1, 3)
    uniq, counts = np.unique(flat, axis=0, return_counts=True)
    modal = uniq[counts.argmax()]
    mask = np.any(key != modal, axis=2)
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None, tuple(int(v) for v in modal)
    return (int(xs.min() * cell), int((xs.max() + 1) * cell - 1),
            int(ys.min() * cell), int((ys.max() + 1) * cell - 1)), tuple(int(v) for v in modal)


def reproject(grab, pose):
    """Crop the campus out of the grab and resample it onto the 448 x 768 grid."""
    h, w = grab.shape[:2]
    px_per_cm, right, up, cxy = project(pose, w, h)
    log = []
    log.append("grab %dx%d  fov %s  height above plane %.0f cm  -> %.5f px/cm "
               "(%.3f px/block)"
               % (w, h, pose["camera"].get("fov_read"), float(pose["camera"]["z"]) - GROUND_Z,
                  px_per_cm, px_per_cm * BLOCK_CM))

    cols = np.array([0, NX - 1], dtype=np.float64)
    rows = np.array([0, NZ - 1], dtype=np.float64)
    grid_c, grid_r = np.meshgrid(cols, rows)
    X, Y = truth_world(grid_c, grid_r)

    def uv(X, Y):
        d = np.stack([X - cxy[0], Y - cxy[1], np.zeros_like(X)], axis=-1)
        u = w / 2.0 + (d @ right) * px_per_cm
        v = h / 2.0 - (d @ up) * px_per_cm
        return u, v

    u, v = uv(X, Y)
    u0, u1 = float(u.min()), float(u.max())
    v0, v1 = float(v.min()), float(v.max())
    log.append("analytic campus rect: u[%.1f, %.1f] v[%.1f, %.1f]  (%.1f x %.1f px)"
               % (u0, u1, v0, v1, u1 - u0, v1 - v0))

    bb, modal = content_bbox(grab)
    if bb is None:
        log.append("WARNING: the grab is a single flat colour %s -- nothing rendered"
                   % (modal,))
        return None, log
    log.append("content bbox in grab: x[%d, %d] y[%d, %d]  (%.1f x %.1f px)  "
               "modal colour %s" % (bb[0], bb[1], bb[2], bb[3],
                                    bb[1] - bb[0] + 1, bb[3] - bb[2] + 1, modal))
    du = 0.5 * (bb[0] + bb[1]) - 0.5 * (u0 + u1)
    dv = 0.5 * (bb[2] + bb[3]) - 0.5 * (v0 + v1)
    su = (bb[1] - bb[0] + 1) / max(u1 - u0, 1e-6)
    sv = (bb[3] - bb[2] + 1) / max(v1 - v0, 1e-6)
    log.append("content bbox vs analytic: centre off by (%.1f, %.1f) px, "
               "size ratio (%.3f, %.3f)" % (du, dv, su, sv))

    # Parity-neutral corrections only: a constant translation (window client
    # offset, i.e. the crop origin) and a constant scale (plane height). Neither
    # can convert a rotation into a reflection.
    if max(abs(du), abs(dv)) > 6.0:
        log.append("NOTE: recentring the crop on the measured content bbox "
                   "(translation %.1f, %.1f px -- parity-neutral)" % (du, dv))
        u0, u1 = u0 + du, u1 + du
        v0, v1 = v0 + dv, v1 + dv
    if max(abs(su - 1.0), abs(sv - 1.0)) > 0.06:
        s = 0.5 * (su + sv)
        log.append("NOTE: rescaling the crop by the measured bbox (%.3f -- "
                   "parity-neutral)" % s)
        cu, cv = 0.5 * (u0 + u1), 0.5 * (v0 + v1)
        u0, u1 = cu + (u0 - cu) * s, cu + (u1 - cu) * s
        v0, v1 = cv + (v0 - cv) * s, cv + (v1 - cv) * s

    x0, x1 = int(round(min(u0, u1))), int(round(max(u0, u1)))
    y0, y1 = int(round(min(v0, v1))), int(round(max(v0, v1)))
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    if x1 - x0 < 32 or y1 - y0 < 32:
        log.append("WARNING: crop is only %dx%d px -- the campus is not in frame"
                   % (x1 - x0, y1 - y0))
        return None, log
    log.append("crop box x[%d, %d) y[%d, %d)  %dx%d px -> %dx%d"
               % (x0, x1, y0, y1, x1 - x0, y1 - y0, NX, NZ))
    crop = Image.fromarray(grab[y0:y1, x0:x1].astype(np.uint8))
    small = np.asarray(crop.resize((NX, NZ), Image.BOX)).astype(np.int16)
    return small, log


# --------------------------------------------------------------------------- #
# the eight-way pass
# --------------------------------------------------------------------------- #

def eight_way(img, ref, label, say):
    say("--- 8-way transforms of %s against out/truth_colour.png ---" % label)
    say("%-13s %-9s %8s %8s %8s %8s" % ("transform", "parity", "r_block",
                                        "r_row", "r_col", "score"))
    rows = []
    for name in TRANSFORMS:
        t = apply_transform(img, name)
        dims = "%dx%d" % (t.shape[1], t.shape[0])
        sc, rb, rr, rc = score_frame(t, ref)
        parity = "rotation" if name in ROTATIONS else "MIRROR"
        rows.append((sc, name, parity, dims, rb, rr, rc))
        say("%-13s %-9s %8.4f %8.4f %8.4f %8.4f   (%s)"
            % (name, parity, rb, rr, rc, sc, dims))
    return rows


def decide(rows, say):
    """Winner among the four transforms that preserve 448 x 768."""
    square = sorted([r for r in rows if r[3] == "%dx%d" % (NX, NZ)],
                    reverse=True)
    best, second = square[0], square[1]
    margin = best[0] / second[0] if second[0] > 0 else float("inf")
    say("winner (dimension-preserving): %-9s score %.4f  parity %s"
        % (best[1], best[0], best[2]))
    say("runner-up                     : %-9s score %.4f  margin %.3fx"
        % (second[1], second[0], margin))
    overall = max(rows)
    say("best of all eight             : %-9s score %.4f" % (overall[1], overall[0]))
    return best, second, margin, overall


PREDICTION = {
    -90.0: {"unmirrored": "identity", "mirrored": "flip_v"},
    90.0: {"unmirrored": "rot180", "mirrored": "flip_h"},
    0.0: {"unmirrored": "transpose", "mirrored": "rot90"},
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", default=os.path.join(OUT, "chirality_grab.png"))
    ap.add_argument("--grab-b", default=os.path.join(OUT, "chirality_grab_b.png"))
    ap.add_argument("--pose", default=os.path.join(OUT, "chirality_pose.json"))
    ap.add_argument("--truth", default=os.path.join(OUT, "truth_colour.png"))
    ap.add_argument("--ortho", default=os.path.join(OUT, "ortho_topdown.png"))
    ap.add_argument("--ortho-raw", default=os.path.join(OUT, "ortho_topdown_raw.png"))
    ap.add_argument("--report", default=os.path.join(OUT, "chirality_report.txt"))
    ap.add_argument("--tag", default="A")
    args = ap.parse_args()

    lines = []

    def say(s=""):
        lines.append(str(s))
        print(s)
        try:
            with open(args.report, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError:
            pass

    say("=== check_chirality start %s ===" % time.strftime("%F %T"))
    for p in (args.grab, args.pose, args.truth):
        if not os.path.isfile(p):
            say("MISSING %s -- nothing to check" % p)
            say("RESULT: FAIL missing input")
            return 1

    pose = json.load(open(args.pose, encoding="utf-8"))
    cam = pose["camera"]
    say("pose %s: loc=(%.1f, %.1f, %.1f) rot=(p %.2f, y %.2f, r %.2f) "
        "fov=%s window=%sx%s"
        % (pose.get("tag"), cam["x"], cam["y"], cam["z"], cam["pitch"],
           cam["yaw"], cam["roll"], cam.get("fov_read"),
           pose["window"]["width"], pose["window"]["height"]))
    say("")

    grab = np.asarray(Image.open(args.grab).convert("RGB")).astype(np.int16)
    truth = np.asarray(Image.open(args.truth).convert("RGB")).astype(np.int16)
    if truth.shape[:2] != (NZ, NX):
        say("truth map is %s, expected %dx%d" % (truth.shape, NX, NZ))
        say("RESULT: FAIL bad truth")
        return 1

    grid, log = reproject(grab, pose)
    for line in log:
        say(line)
    say("")
    if grid is None:
        say("RESULT: FAIL could not reproject the grab")
        return 1

    rows = eight_way(grid, truth, "grab %s" % args.tag, say)
    say("")
    best, second, margin, overall = decide(rows, say)

    yaw = float(cam["yaw"]) % 360.0
    key = -90.0 if abs(yaw - 270.0) < 1.0 else (yaw if yaw <= 180.0 else yaw - 360.0)
    pred = PREDICTION.get(key)
    say("")
    if pred:
        say("pose prediction for yaw %+.0f: unmirrored -> %-10s | mirrored -> %s"
            % (key, pred["unmirrored"], pred["mirrored"]))
        if best[1] == pred["unmirrored"]:
            say("the winner is the pose's own 'unmirrored' transform")
        elif best[1] == pred["mirrored"]:
            say("the winner is the pose's own 'mirrored' transform")
        else:
            say("WARNING: the winner is neither prediction for this yaw -- "
                "the pose record or the reprojection is suspect")

    verdict = None
    if margin < 1.15:
        say("")
        say("UNDECIDED: margin %.3f < 1.15 -- this grab cannot separate the "
            "transforms. Do not read a conclusion out of it." % margin)
        verdict = "undetermined"
    else:
        verdict = "mirrored" if best[1] in MIRRORS else "not_mirrored"
        say("")
        say("VERDICT: the winner %r is a %s -> the world is %s"
            % (best[1], best[2].lower(),
               "MIRRORED" if verdict == "mirrored" else "NOT mirrored"))

    # ---- cross-checks -------------------------------------------------- #
    say("")
    say("--- cross-check: the same grab against the published ortho frames ---")
    ortho_ok = None
    for label, path, expect in (("ortho_topdown.png", args.ortho, "identity"),
                                ("ortho_topdown_raw.png", args.ortho_raw, "flip_v")):
        if not os.path.isfile(path):
            say("%s: missing, skipped" % label)
            continue
        o = np.asarray(Image.open(path).convert("RGB")).astype(np.int16)
        if o.shape[:2] != (NZ, NX):
            say("%s: %s, skipped" % (label, o.shape))
            continue
        r2 = eight_way(grid, o, "grab vs %s" % label, say)
        sq = sorted([r for r in r2 if r[3] == "%dx%d" % (NX, NZ)], reverse=True)
        m2 = sq[0][0] / sq[1][0] if sq[1][0] > 0 else float("inf")
        say("%s: winner %s (%.4f) margin %.3fx  expected %s  %s"
            % (label, sq[0][1], sq[0][0], m2, expect,
               "AGREES" if sq[0][1] == expect else "DISAGREES"))
        if label.startswith("ortho_topdown.png"):
            ortho_ok = (sq[0][1] == expect and m2 >= 1.15)
        say("")

    if os.path.isfile(args.grab_b) and os.path.isfile(args.grab_b.replace(
            "chirality_grab_b", "chirality_pose_b")):
        say("--- control capture B (yaw +90) ---")
        pose_b = json.load(open(args.grab_b.replace(
            "chirality_grab_b", "chirality_pose_b"), encoding="utf-8"))
        gb, logb = reproject(np.asarray(
            Image.open(args.grab_b).convert("RGB")).astype(np.int16), pose_b)
        for line in logb:
            say(line)
        if gb is not None:
            rb = eight_way(gb, truth, "grab B", say)
            bb, s2, mb, ob = decide(rb, say)
            say("control B verdict: %r (margin %.3fx) -- must be 180 degrees "
                "from capture A's winner for a consistent answer"
                % (bb[1], mb))
        say("")

    say("")
    say("RESULT: %s winner=%s parity=%s margin=%.3fx "
        "truth=%s ortho_crosscheck=%s"
        % ("PASS" if verdict != "undetermined" else "UNDECIDED",
           best[1], best[2], margin, verdict,
           "n/a" if ortho_ok is None else ("ok" if ortho_ok else "disagrees")))
    return 0 if verdict != "undetermined" else 1


if __name__ == "__main__":
    sys.exit(main())
