# -*- coding: utf-8 -*-
"""
qa_persp_gates.py -- a pass/fail verdict on a ground-level frame, in numbers.

The acceptance bar is "MC 地图的标准贴图质量，远看近看都仿真". Until now the
verdict was a human looking at a PNG and some hand-typed python one-liners, so
the same defects kept slipping through: a navy night dome read as "a bit dark",
a blue-floodlit facade read as "atmospheric", and a frame with two black slabs
at the edges read as "fine, the buildings are there". This file replaces the
eyeballing with five measured gates on the same pixels, so a frame either
passes or it does not, and the reason is a number.

The five gates, and what each one is for
----------------------------------------
sky   the sky segment's sRGB luma must be 130..210 and its channels must run
      B > G > R. Thresholds copied from tools/fix_lighting_mc.py's "acceptance
      gates" block, which is the project's own ruler; they are not invented
      here. A deep navy dome is the failure this catches.
blue  no runaway blue on non-sky surfaces: at most 5% of visible non-sky
      pixels may have B/R > 1.6. This is the gate that the "blue floodlight at
      night" frame fails (a mid facade there measured B/R 2.30).
black nothing crushed: fewer than 5% of pixels may have luma < 12.
clip  nothing blown out: fewer than 3% of pixels may have max channel >= 250,
      and separately fewer than 3% may have luma > 235.
texel the "up close" half of the bar. A 16x16-per-block texture only survives
      to screen if the image keeps high-frequency detail, so the mean absolute
      Laplacian over the ground band is measured -- and then *not* gated,
      because no threshold for it can be justified from the frames we have.
      It is printed next to the same number for out/persp_ship.png so a
      regression shows up as a ratio rather than as an opinion.

Why the segments are content-derived
------------------------------------
The first draft masked "the upper 55% of the frame that is blue-dominant" and
measured that rectangle. It came out at 19.7% of the frame with luma 73.8 --
and half the sky in this frame sits below 55% of the height, behind the
roofline on the right, so 19,640 sky pixels fell on the "facade" side of the
blue gate, which read 23.3% purely from sky. Changing the rectangle to a
per-column run from the top fixed the coverage (23.5%) and broke it a worse
way: at columns 600-780 the run walked from the sky straight down the tall
block's shadowed navy wall to row 144. A navy wall and a navy sky are the same
colour; only the texture tells them apart, and the colour-only test had none.

So every segment here is found from the pixels and from a physical property,
never from a coordinate:

  sky   per column, the run of rows from the top that is blue-dominant
        (B-R >= 8), not crushed (luma >= 12, B >= 20) and SMOOTH (9x9 luma
        std < 5). Open sky measures 0.78 (p99 1.00) on that statistic and the
        shadows it has to be told apart from measure 8.9 here and 35.8 in the
        older frame. Up to two non-conforming rows are tolerated, because a
        zero-tolerance run ended at the first dither cluster and covered 14.0%
        of the frame. The run is capped at SKY_MAX_FRAC of the frame height,
        because a frame that is navy everywhere has no skyline at all and must
        not be reported as 100% sky.
  dark  luma < 12, i.e. the crushed pixels the black gate counts.
  vis   everything that is neither sky nor crushed: the surfaces whose colour
        a viewer can actually judge, which is the blue gate's universe.

The black edge slabs cost this 2,313 sky pixels (0.45% of the frame, the top
rows of the left one where its gradient is smooth and blue); the right one
contributes none. Both are blue (mean B-R 13.7 left, 64.6 right) but crushed
(luma 5.3 and 8.3), so a column over them ends its run inside its first rows.

Measured on out/persp_bounce.png, the segments come out as: sky 14.6% of the
frame at mean RGB (55, 92, 138), luma 87 -- a mask that covers the sky and the
horizon haze, stops at the rooflines and excludes both slabs. The older
out/persp_ship.png segments to 14.3% / luma 90, i.e. the same sky seen through
a different exposure.

Usage
  python tools/qa_persp_gates.py [--frame out/persp_bounce.png]
                                 [--log LOGS/qa_persp_gates/<stem>.txt]
                                 [--json LOGS/qa_persp_gates/<stem>.json]
                                 [--reference out/persp_ship.png]

Exit code 0 on PASS, 1 on FAIL. --json writes the same measurements as a dict
so another tool can trend them without parsing the table.
"""

import argparse
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

try:
    from mcpaths import LOGS                                # noqa: E402
except Exception:                                           # pragma: no cover
    LOGS = os.path.join(os.path.dirname(REPO), "logs")

from PIL import Image                                       # noqa: E402

# --------------------------------------------------------------------------- #
# thresholds
# --------------------------------------------------------------------------- #

#: sRGB luma window for a real daytime sky, and the channel order that goes
#: with it. Quoted from tools/fix_lighting_mc.py::report_calibration:
#: "sky  sRGB luma 130..210 and B > G > R (a real daytime sky)".
SKY_LUMA_LO = 130.0
SKY_LUMA_HI = 210.0

#: The blue gate. 1.6 is not a taste call: the shaded mid facade (rows
#: 180-240, cols 500-700) measures B/R 2.62 in this frame and 2.75 in the older
#: one, while the sunlit ground and path sit at 0.97-1.05, so 1.6 is the gap
#: between "slightly cool" and "lit by a blue floodlight". 5% allows a sky
#: reflection in glass or a blue awning without hiding a whole facade.
BLUE_RATIO_MAX = 1.6
BLUE_MAX_FRAC = 0.05

#: Black gate. 12 is the top of the sRGB toe at this exposure (the frame's
#: shadowed windows sit at 0-8 and only there); 5% because a doorway or a deep
#: shadow is legitimate and two ~130x270 slabs are not.
BLACK_LUMA = 12.0
BLACK_MAX_FRAC = 0.05

#: Clip gate. 250 on a channel catches values the tone curve has pinned; luma
#: 235 catches a large area that is not clipped per channel but has no texture
#: left in it. 3% is the size of a specular highlight, not of a path.
CLIP_CHANNEL = 250
CLIP_LUMA = 235.0
CLIP_MAX_FRAC = 0.03

#: Sky segmentation. BLUE_MIN is the smallest B-R that is not a neutral grey
#: (the concrete facades measure B-R 2-6 at this exposure); LUMA_MIN and
#: BLUE_CH = 20 keep the crushed edge slabs out (they are luma 5..9); TOL is
#: the dither allowance measured above; MAX_FRAC stops a uniformly navy frame
#: from being reported as all sky.
SKY_BLUE_MIN = 8.0
SKY_LUMA_MIN = 12.0
SKY_BLUE_CH = 20.0
SKY_TOL = 2
SKY_MAX_FRAC = 0.65

#: Sky smoothness. 9x9 box std of luma; open sky measures 0.78 (p99 1.00) and
#: the least-textured facade that matters measures 8.9, so 5 sits 5x above the
#: dither and 1.8x below the wall.
SKY_SD_WINDOW = 9
SKY_SD_MAX = 5.0

#: Below this the frame shows no sky at all (a wall, an interior, a fully
#: occluded camera) and the sky gate cannot be answered.
SKY_MIN_FRAC = 0.02

#: Ground band for the texel statistic: the lower third of the frame.
GROUND_BAND = 2.0 / 3.0

DEFAULT_FRAME = os.path.join(REPO, "out/persp_bounce.png")
DEFAULT_REFERENCE = os.path.join(REPO, "out/persp_ship.png")


def say(msg, lines):
    lines.append(str(msg))
    print(msg)


# --------------------------------------------------------------------------- #
# image maths (arrays in, arrays out -- the unit tests call these directly)
# --------------------------------------------------------------------------- #

def as_rgb(img):
    """-> float64 (H, W, 3) in 0..255. Accepts an array or a PIL image."""
    if isinstance(img, Image.Image):
        img = np.asarray(img.convert("RGB"))
    a = np.asarray(img)
    if a.ndim == 2:
        a = np.stack([a, a, a], axis=-1)
    if a.shape[-1] == 4:
        a = a[..., :3]
    return a.astype(np.float64)


def luma(rgb):
    """sRGB luma from the sRGB values directly (the weights are Rec.709's).

    Not linearised: every other number in this repo's QA tools is an 8-bit
    sRGB number, and the 130..210 sky window quoted from fix_lighting_mc.py is
    an sRGB window, so linearising here would silently move the ruler.
    """
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def box_mean(a, k):
    """Mean over a k x k box, edge-padded, same shape. Integral-image based."""
    p = k // 2
    ap = np.pad(a, ((p, p), (p, p)), mode="edge")
    c = np.cumsum(np.cumsum(ap, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / float(k * k)


def box_std(a, k):
    m = box_mean(a, k)
    m2 = box_mean(a * a, k)
    return np.sqrt(np.maximum(m2 - m * m, 0.0))


def laplacian_abs(a):
    """|4-neighbour Laplacian| of a 2-D array, cropped by 1 px per side.

    The 4-neighbour kernel (not the 8-neighbour one) because the texel grid is
    axis-aligned: a 16x16 tile puts its edges on rows and columns, so the
    diagonal term only adds the noise of the dither pattern.
    """
    c = a[1:-1, 1:-1]
    return np.abs(4.0 * c - a[:-2, 1:-1] - a[2:, 1:-1] - a[1:-1, :-2] - a[1:-1, 2:])


# --------------------------------------------------------------------------- #
# segmentation
# --------------------------------------------------------------------------- #

def segment(rgb):
    """-> dict of boolean masks: sky, dark, vis.

    ``vis`` is the blue gate's universe: visible (not crushed) pixels that are
    not sky. It is deliberately *not* "not black" -- a surface at luma 13 is a
    surface a viewer reads as a colour, and excluding it would let the frame
    hide its bluest facades in the toe, which is exactly what the older
    reference frame does.
    """
    rgb = as_rgb(rgb)
    L = luma(rgb)
    R, B = rgb[..., 0], rgb[..., 2]
    H, W = L.shape

    dark = L < BLACK_LUMA

    # A row is "still sky" if it is blue-dominant, not crushed, bright enough
    # in blue that a colour ratio is meaningful, and SMOOTH. The smoothness
    # test is what stops a navy wall from being taken for sky: measured on
    # these frames, open sky has a 9x9 luma std of 0.78 (p99 1.0) while the
    # shadowed upper facade of the tall block measures 8.9 in this frame and
    # 35.8 in the older one. Without it the per-column run walked from the
    # sky straight down that facade to row 144 -- a dark blue wall and a dark
    # blue sky are the same colour, only the texture tells them apart.
    sd = box_std(L, SKY_SD_WINDOW)
    skylike = (((B - R) >= SKY_BLUE_MIN) & (L >= SKY_LUMA_MIN)
               & (B >= SKY_BLUE_CH) & (sd < SKY_SD_MAX))

    # Per column, run from the top and stop at the third consecutive row that
    # is not sky-like. Vectorised as a run-length counter down the rows.
    run = np.zeros((H, W), dtype=np.int16)
    for r in range(H):
        run[r] = np.where(skylike[r], 0, (run[r - 1] if r else 0) + 1)
    broke = run >= (SKY_TOL + 1)
    has_break = broke.any(axis=0)
    brk = np.argmax(broke, axis=0)
    brk = np.where(has_break, brk, H)                  # no break: use the cap
    brk = np.minimum(brk, int(SKY_MAX_FRAC * H))
    sky = np.arange(H)[:, None] < brk[None, :]

    return {"sky": sky, "dark": dark, "vis": (~sky) & (~dark)}


# --------------------------------------------------------------------------- #
# measurements
# --------------------------------------------------------------------------- #

def measure(rgb):
    """-> dict of the five gates' numbers. No file access, no printing."""
    rgb = as_rgb(rgb)
    H, W = rgb.shape[:2]
    L = luma(rgb)
    R, B = rgb[..., 0], rgb[..., 2]
    seg = segment(rgb)
    sky, vis = seg["sky"], seg["vis"]

    out = {"width": int(W), "height": int(H), "pixels": int(H * W)}

    # ---- sky ---- #
    n_sky = int(sky.sum())
    out["sky_pixels"] = n_sky
    out["sky_frac"] = n_sky / float(H * W)
    if n_sky:
        mr, mg, mb = (float(rgb[sky][:, i].mean()) for i in range(3))
        out["sky_rgb"] = [mr, mg, mb]
        out["sky_luma"] = 0.2126 * mr + 0.7152 * mg + 0.0722 * mb
        out["sky_b_gt_g"] = float((rgb[sky][:, 2] > rgb[sky][:, 1]).mean())
        out["sky_g_gt_r"] = float((rgb[sky][:, 1] > rgb[sky][:, 0]).mean())
    else:
        out["sky_rgb"] = None
        out["sky_luma"] = None
        out["sky_b_gt_g"] = 0.0
        out["sky_g_gt_r"] = 0.0

    # ---- blue on visible non-sky surfaces ---- #
    n_vis = int(vis.sum())
    out["vis_pixels"] = n_vis
    if n_vis:
        # R floored at 1 so the ratio stays finite. In the older frame 1% of
        # the visible pixels sit at R == 1 with B 66..100, so the floor caps
        # their ratio at B; that compresses only ratios far over the gate.
        ratio = B[vis] / np.maximum(R[vis], 1.0)
        out["blue_ratio_median"] = float(np.median(ratio))
        out["blue_ratio_p90"] = float(np.percentile(ratio, 90))
        out["blue_ratio_p99"] = float(np.percentile(ratio, 99))
        out["blue_frac"] = float((ratio > BLUE_RATIO_MAX).mean())
    else:
        out["blue_ratio_median"] = None
        out["blue_ratio_p90"] = None
        out["blue_ratio_p99"] = None
        out["blue_frac"] = 1.0                  # nothing measurable: no credit

    # ---- black ---- #
    out["black_frac"] = float(seg["dark"].mean())

    # Reported, not gated: the same blue ratio with the luma floor removed.
    # It is the diagnostic that explains a frame whose gated blue fraction
    # *fell*: lifting the toe moves crushed blue pixels above the floor and
    # into the gate's universe, so an "improved" frame can legitimately show
    # a higher gated blue fraction while its total blue is unchanged. On the
    # frames here this number is 40.1% / 40.0% / 36.4% while the gated one is
    # 28.8% / 28.8% / 9.8% -- the same defect on either side of the floor.
    nonsky = ~seg["sky"]
    if nonsky.any():
        ratio_all = B[nonsky] / np.maximum(R[nonsky], 1.0)
        out["blue_frac_no_floor"] = float((ratio_all > BLUE_RATIO_MAX).mean())
    else:
        out["blue_frac_no_floor"] = None

    # ---- clip ---- #
    out["clip_channel_frac"] = float((rgb.max(axis=2) >= CLIP_CHANNEL).mean())
    out["clip_luma_frac"] = float((L > CLIP_LUMA).mean())

    # ---- texel evidence (reported, not gated) ---- #
    g0 = int(GROUND_BAND * H)
    band = L[g0:, :]
    if band.size > 4:
        lap = laplacian_abs(band)
        out["texel_lap"] = float(lap.mean() / 255.0)
        out["texel_lap_rel"] = float(lap.mean() / max(float(band.mean()), 1e-6))
        out["texel_ground_luma"] = float(band.mean())
    else:
        out["texel_lap"] = None
        out["texel_lap_rel"] = None
        out["texel_ground_luma"] = None

    # ---- reported, not gated: how much of the frame is at the top of the
    # usable range. The foreground path measures luma 216 here, which passes
    # the clip gate (nothing is at 250) but is 36% of the frame with no
    # texture left in it. It is the number to watch if the exposure moves. ---- #
    out["bright_frac_luma_210"] = float((L > 210).mean())

    return out


def gate_rows(m):
    """-> list of (key, label, value, threshold, ok) for everything gated."""
    rows = []

    # sky: three conditions, reported as three rows so the log says *which*
    # part of "a real daytime sky" failed.
    rows.append(("sky_present", "sky segment found", m["sky_frac"],
                  ">= %.0f%% of frame" % (100 * SKY_MIN_FRAC),
                  m["sky_frac"] >= SKY_MIN_FRAC))
    lo, hi = SKY_LUMA_LO, SKY_LUMA_HI
    ok_l = m["sky_luma"] is not None and lo <= m["sky_luma"] <= hi
    rows.append(("sky_luma", "sky sRGB luma", m["sky_luma"],
                  "%g..%g" % (lo, hi), ok_l))
    order = (m["sky_b_gt_g"] >= 0.90) and (m["sky_g_gt_r"] >= 0.90)
    rows.append(("sky_order", "sky B>G>R (pixel share)",
                  (m["sky_b_gt_g"], m["sky_g_gt_r"]), "both >= 0.90", order))

    rows.append(("blue", "B/R > %g on visible non-sky" % BLUE_RATIO_MAX,
                  m["blue_frac"], "< %.0f%%" % (100 * BLUE_MAX_FRAC),
                  m["blue_frac"] < BLUE_MAX_FRAC))
    rows.append(("black", "luma < %g" % BLACK_LUMA, m["black_frac"],
                  "< %.0f%%" % (100 * BLACK_MAX_FRAC),
                  m["black_frac"] < BLACK_MAX_FRAC))
    rows.append(("clip_channel", "max channel >= %d" % CLIP_CHANNEL,
                  m["clip_channel_frac"], "< %.0f%%" % (100 * CLIP_MAX_FRAC),
                  m["clip_channel_frac"] < CLIP_MAX_FRAC))
    rows.append(("clip_luma", "luma > %g" % CLIP_LUMA, m["clip_luma_frac"],
                  "< %.0f%%" % (100 * CLIP_MAX_FRAC),
                  m["clip_luma_frac"] < CLIP_MAX_FRAC))
    return rows


def fmt_value(v):
    if v is None:
        return "n/a"
    if isinstance(v, tuple):
        return "%.2f/%.2f" % v
    if isinstance(v, float):
        return "%.4f" % v
    return str(v)


def fmt_pct(v):
    if v is None:
        return "n/a"
    return "%.2f%%" % (100.0 * v)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def _load(path):
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def _rel(path):
    try:
        return os.path.relpath(path, REPO)
    except ValueError:
        return path


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--frame", default=DEFAULT_FRAME)
    ap.add_argument("--reference", default=DEFAULT_REFERENCE,
                    help="frame the texel statistic is reported against")
    ap.add_argument("--log", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    frame = os.path.abspath(args.frame)
    if not os.path.isfile(frame):
        print("RESULT: FAIL frame not found: %s" % frame)
        return 1
    stem = os.path.splitext(os.path.basename(frame))[0]
    log_path = args.log or os.path.join(LOGS, "qa_persp_gates", stem + ".txt")
    # Both artefacts land under the logs dir, not under out/: out/ holds
    # rendered frames, and a QA run must not be able to leave anything there
    # that a later capture could be blamed for.
    json_path = args.json or os.path.join(LOGS, "qa_persp_gates",
                                          stem + ".json")

    lines = []
    say("=== ground-level frame gates: %s ===" % _rel(frame), lines)

    rgb = _load(frame)
    m = measure(rgb)
    say("frame %dx%d  %d px" % (m["width"], m["height"], m["pixels"]), lines)
    say("", lines)

    rows = gate_rows(m)

    # ---- table ---- #
    say("  %-34s %-16s %-16s %s" % ("gate", "measured", "threshold", "verdict"),
        lines)
    say("  " + "-" * 82, lines)
    for key, label, value, thresh, ok in rows:
        if key == "sky_luma":
            shown = "n/a" if value is None else "%.1f" % value
        elif key in ("sky_present", "blue", "black", "clip_channel",
                     "clip_luma"):
            shown = fmt_pct(value)
        else:
            shown = fmt_value(value)
        say("  %-34s %-16s %-16s %s"
            % (label, shown, thresh, "OK" if ok else "OVER"), lines)

    # ---- the distributions behind the blue gate ---- #
    say("", lines)
    say("  blue gate detail (visible non-sky pixels, n=%d):" % m["vis_pixels"],
        lines)
    if m["vis_pixels"]:
        say("    B/R median %.2f   p90 %.2f   p99 %.2f   over %g: %s"
            % (m["blue_ratio_median"], m["blue_ratio_p90"],
               m["blue_ratio_p99"], BLUE_RATIO_MAX, fmt_pct(m["blue_frac"])),
            lines)
    if m["blue_frac_no_floor"] is not None:
        say("    same ratio with the luma floor removed (all non-sky, "
            "not gated): %s" % fmt_pct(m["blue_frac_no_floor"]), lines)
    say("  black gate detail: %s of the frame is below luma %g"
        % (fmt_pct(m["black_frac"]), BLACK_LUMA), lines)
    say("  clip detail: max channel >= %d is %s, luma > %g is %s"
        % (CLIP_CHANNEL, fmt_pct(m["clip_channel_frac"]), CLIP_LUMA,
           fmt_pct(m["clip_luma_frac"])), lines)
    say("  for reference (not gated): luma > 210 is %s of the frame"
        % fmt_pct(m["bright_frac_luma_210"]), lines)
    if m["sky_rgb"]:
        say("  sky segment: %s of frame, mean RGB (%.0f, %.0f, %.0f), luma %.1f"
            % (fmt_pct(m["sky_frac"]), m["sky_rgb"][0], m["sky_rgb"][1],
               m["sky_rgb"][2], m["sky_luma"]), lines)

    # ---- texel evidence: measured, compared, deliberately not gated ---- #
    say("", lines)
    say("  texel evidence -- mean |Laplacian| over the lower third, reported",
        lines)
    say("  as a trend against %s, NOT gated:" % _rel(os.path.abspath(
        args.reference)), lines)
    ref = None
    ref_path = os.path.abspath(args.reference)
    if os.path.isfile(ref_path):
        ref = measure(_load(ref_path))
    if m["texel_lap"] is None:
        say("    no ground band in this frame -- nothing to measure", lines)
    else:
        say("    %-24s %8s %8s" % ("frame", "lap/255", "lap/luma"), lines)
        say("    %-24s %8.4f %8.5f"
            % (stem, m["texel_lap"], m["texel_lap_rel"]), lines)
        if ref and ref["texel_lap"] is not None:
            say("    %-24s %8.4f %8.5f"
                % (os.path.basename(ref_path), ref["texel_lap"],
                   ref["texel_lap_rel"]), lines)
            say("    ratio vs reference: %.3f (lap/255)  %.3f (lap/luma)"
                % (m["texel_lap"] / max(ref["texel_lap"], 1e-9),
                   m["texel_lap_rel"] / max(ref["texel_lap_rel"], 1e-9)), lines)
        else:
            say("    reference frame not readable -- no ratio", lines)
        say("    No pass threshold is asserted for this number. A 16x16 tile",
            lines)
        say("    is only 'sharp enough' relative to another frame from the",
            lines)
        say("    same camera, and one frame is not a standard: the two",
            lines)
        say("    ground-level frames on disk here differ by under 2%, so any",
            lines)
        say("    line drawn between them would be a line invented to make",
            lines)
        say("    today's frame pass. Read the ratio instead: a fall means the",
            lines)
        say("    latest frame lost texel detail the reference still had.",
            lines)

    failed = [r for r in rows if not r[4]]

    # ---- write the json ---- #
    payload = {
        "frame": _rel(frame),
        "reference": _rel(ref_path),
        "measured": m,
        "reference_texel": (None if ref is None else
                            {"texel_lap": ref["texel_lap"],
                             "texel_lap_rel": ref["texel_lap_rel"]}),
        "gates": [{"key": k, "label": l, "value": v, "threshold": th,
                   "ok": bool(ok)} for k, l, v, th, ok in rows],
        "result": "PASS" if not failed else "FAIL",
    }
    try:
        d = os.path.dirname(json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
    except OSError as exc:                              # pragma: no cover
        print("warning: could not write json %s (%s)" % (json_path, exc))

    say("", lines)
    if failed:
        parts = []
        for k, _l, v, _t, _o in failed:
            if k == "sky_luma":
                parts.append("%s=%.1f" % (k, v))
            elif k == "sky_order":
                parts.append("%s=%s" % (k, fmt_value(v)))
            else:
                parts.append("%s=%s" % (k, fmt_pct(v)))
        say("RESULT: FAIL %s" % ", ".join(parts), lines)
    else:
        say("RESULT: PASS all %d gates" % len(rows), lines)

    # The log is written after the RESULT line so the file ends the same way
    # the terminal output does -- the earlier draft wrote it before, and a
    # reader of the log could not tell pass from fail.
    try:
        d = os.path.dirname(log_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:                              # pragma: no cover
        print("warning: could not write log %s (%s)" % (log_path, exc))

    print("wrote %s" % log_path)
    print("wrote %s" % json_path)

    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
