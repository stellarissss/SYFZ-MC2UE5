# -*- coding: utf-8 -*-
"""
test_qa_persp_gates.py -- the synthetic frames each gate must reject.

tools/qa_persp_gates.py is only worth having if its gates actually fire. A
gate that passes everything looks exactly like a working gate on a good frame,
and a gate whose mask is wrong looks exactly like a working gate on a frame
that happens to fail. So each test here builds a frame whose defect is known
by construction -- a navy night, a black slab, a clipped wall, a blurred
ground -- and asserts that the gate named for that defect fails on it, and
that a healthy frame passes all of them.

The frames are arrays, never files: segment() and measure() take an array in
and give masks and numbers out, so these tests never touch out/ and cannot be
broken by a capture moving a camera.

Run with the project venv:
  /g/BaiduNetdiskDownload/real/venv/Scripts/python.exe tools/test_qa_persp_gates.py
"""

import os
import sys
import unittest

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import qa_persp_gates as q                                   # noqa: E402

W, H = 480, 270


def _noise(rng, shape, amp=1.5):
    return rng.normal(0.0, amp, shape)


def textured_ground(rng, rows, cell=16, base=112, amp=14):
    """A blocky 16-px tile pattern, i.e. what a 16x16 texture looks like when
    it is big enough on screen to be judged. Grey on purpose: B-R = 0 keeps it
    out of the sky mask, which is a property the tests rely on."""
    y, x = np.mgrid[0:rows, 0:W]
    level = np.where(((y // cell) + (x // cell)) % 2 == 0, base + amp,
                     base - amp)
    rgb = np.empty((rows, W, 3), dtype=np.float64)
    for ch, mul in enumerate((0.98, 1.00, 1.02)):
        rgb[..., ch] = level * mul + _noise(rng, (rows, W))
    return rgb


def healthy_frame(seed=7):
    """Pale-blue daytime sky on top, textured mid-grey ground, one green patch.

    Every gate should pass: sky luma 176 with B>G>R, ground B/R 1.04, no
    pixel below luma 12, nothing above channel 220.
    """
    rng = np.random.default_rng(seed)
    top = H // 2
    sky = np.zeros((top, W, 3), dtype=np.float64)
    sky[...] = (150.0, 180.0, 220.0)
    sky += _noise(rng, (top, W, 1))                  # dither, amplitude ~1.5
    ground = textured_ground(rng, H - top)
    img = np.concatenate([sky, ground], axis=0)
    img[H - 60:H - 20, 20:60] = (60.0, 120.0, 60.0)   # a lawn patch
    return np.clip(img, 0, 255)


def night_frame():
    """A night dome: dark navy everywhere, B >> R, and no skyline at all.

    Both the sky gate (luma 26, not 130..210) and the blue gate (ground B/R 5)
    must fire on this one.
    """
    img = np.zeros((H, W, 3), dtype=np.float64)
    img[: int(H * 0.65)] = (10.0, 25.0, 70.0)
    img[int(H * 0.65):] = (16.0, 30.0, 80.0)
    return img


def _gates(img):
    return {k: ok for k, _l, _v, _t, ok in q.gate_rows(q.measure(img))}


class TestSegmentation(unittest.TestCase):
    def test_masks_have_frame_shape(self):
        seg = q.segment(healthy_frame())
        self.assertEqual(set(seg), {"sky", "dark", "vis"})
        for m in seg.values():
            self.assertEqual(m.shape, (H, W))
            self.assertEqual(m.dtype, np.bool_)

    def test_sky_is_the_top_of_a_healthy_frame(self):
        seg = q.segment(healthy_frame())
        self.assertTrue(seg["sky"][2, W // 2])
        self.assertFalse(seg["sky"][H - 2, W // 2])
        # roughly the top half, and not the whole frame
        self.assertGreater(seg["sky"].mean(), 0.40)
        self.assertLess(seg["sky"].mean(), 0.55)

    def test_a_black_slab_is_dark_not_sky(self):
        img = healthy_frame()
        img[40:200, 0:80] = 0.0
        seg = q.segment(img)
        self.assertTrue(seg["dark"][100, 40])
        self.assertFalse(seg["sky"][100, 40])
        self.assertFalse(seg["vis"][100, 40])

    def test_a_navy_frame_is_not_all_sky(self):
        """The cap in the sky run exists for this: a frame with no skyline
        must not come back as 100% sky, or the blue gate has no universe."""
        seg = q.segment(night_frame())
        self.assertLess(seg["sky"].mean(), q.SKY_MAX_FRAC + 0.01)
        self.assertGreater(seg["vis"].mean(), 0.3)


class TestGatesFire(unittest.TestCase):
    def test_healthy_frame_passes_every_gate(self):
        m = q.measure(healthy_frame())
        bad = [k for k, ok in _gates(healthy_frame()).items() if not ok]
        self.assertEqual(bad, [], "healthy frame failed %s (metrics %r)"
                         % (bad, {k: m[k] for k in
                                  ("sky_luma", "blue_frac", "black_frac",
                                   "clip_channel_frac", "clip_luma_frac")}))

    def test_night_frame_fails_sky_and_blue(self):
        g = _gates(night_frame())
        m = q.measure(night_frame())
        self.assertFalse(g["sky_luma"], "night sky luma %.1f passed"
                         % m["sky_luma"])
        self.assertFalse(g["blue"], "night blue fraction %.3f passed"
                         % m["blue_frac"])
        self.assertGreater(m["blue_frac"], 0.5)

    def test_black_rectangle_fails_black_gate(self):
        img = healthy_frame()
        r0, r1 = int(H * 0.45), int(H * 0.85)
        c0, c1 = int(W * 0.10), int(W * 0.60)
        img[r0:r1, c0:c1] = 0.0
        m = q.measure(img)
        g = _gates(img)
        self.assertAlmostEqual(m["black_frac"], 0.20, delta=0.02)
        self.assertFalse(g["black"], "20%% black passed: %.3f"
                         % m["black_frac"])

    def test_clipped_area_fails_clip_gates(self):
        img = healthy_frame()
        img[int(H * 0.55):int(H * 0.75), int(W * 0.2):int(W * 0.8)] = 255.0
        m = q.measure(img)
        g = _gates(img)
        self.assertGreater(m["clip_channel_frac"], 0.05)
        self.assertFalse(g["clip_channel"], "clipped channel area passed")
        self.assertFalse(g["clip_luma"], "clipped luma area passed")

    def test_blur_lowers_texel_evidence(self):
        sharp = healthy_frame()
        blur = sharp.copy()
        g0 = int(q.GROUND_BAND * H)
        band = blur[g0:, :, :]
        k = 7
        pad = np.pad(band, ((k // 2, k // 2), (k // 2, k // 2), (0, 0)),
                     mode="edge")
        acc = np.zeros_like(band)
        for dy in range(k):
            for dx in range(k):
                acc += pad[dy:dy + band.shape[0], dx:dx + band.shape[1]]
        blur[g0:, :, :] = acc / float(k * k)

        s = q.measure(sharp)["texel_lap"]
        b = q.measure(blur)["texel_lap"]
        self.assertIsNotNone(s)
        self.assertLess(b, s * 0.5,
                        "blurred ground measured %.5f vs sharp %.5f" % (b, s))


class TestRealFrameShape(unittest.TestCase):
    """The gates must not depend on the frame being 960x540 or on a file."""

    def test_other_resolutions(self):
        img = healthy_frame()[:, :, :]
        for w, h in ((160, 90), (960, 540)):
            small = np.asarray(q.Image.fromarray(img.astype(np.uint8))
                               .resize((w, h)))
            m = q.measure(small)
            self.assertEqual((m["width"], m["height"]), (w, h))
            self.assertIn("sky_luma", m)
            self.assertGreater(m["sky_frac"], q.SKY_MIN_FRAC)


if __name__ == "__main__":
    unittest.main(verbosity=2)
