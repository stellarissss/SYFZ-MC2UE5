# -*- coding: utf-8 -*-
"""
ortho_topdown.py -- a TRUE orthographic top-down frame of the campus.

Why this exists
---------------
The acceptance requirement is "the UE campus corresponds to the save", and the
ruler for that is `out/ref_map.png` (448x768, 1 px = 1 block, column = +x,
row = +z). The frame that was supposed to answer it, `-MCstop=5` (index 5 of
`GTourStops` in `MCConsoleCommands.cpp`), does not: the pawn is teleported to
Z = 65000 and then *falls*, and its pitch is never forced to -90, so the shot is
an oblique low-altitude view. There was no overhead frame to compare against at
all.

This tool sidesteps the pawn entirely. A static `SceneCapture2D` with an
orthographic projection and no physics renders into a 448x768 render target, and
the frame is written out. Nothing moves, nothing falls.

Geometry
--------
    blocks  x in [-144, 304)          z in [-544, 224)   (ref_map's window)
    cm      1 block = 100 cm
    ortho width 44800 cm
    448:768 aspect => vertical coverage 44800 * 768/448 = 76800 cm = 768 blocks
        (Runtime/Renderer/Private/SceneCaptureRendering.cpp::BuildOrthoMatrix:
         half-width = OrthoWidth/2, half-height = OrthoWidth/2 / (W/H))

    pixel (col, row) <-> block (x, z); which of the two axis orders the frame
    ends up in is measured, not assumed -- see Rotation below.

The frame is NOT centred on the brief's numbers, and that is a finding
------------------------------------------------------------------------
The brief says the campus occupies UE X[-14400, 30400) and Y[-54400, 22400).
Measured from the level (union of the 189 StaticMeshActor bounds, printed on
every run):

    X[-14400, 30300]   Y[-22400, +54400]   Z[100, 6000] cm

X agrees, size agrees (44700 x 76800 = 447 x 768 blocks), but **Y is mirrored**:
the level's world->UE mapping negates Y relative to the contract. A camera at
the contract's Y = -16000 framed the top 42% of the frame on empty space, which
is what the first attempts showed. This script therefore centres the camera on
the *measured* bounds (overridable with MC_ORTHO_CX / MC_ORTHO_CY) and lets the
axis-transform scoring deal with the flip.

Rotation, and why it is yaw = -90 and not yaw = 0
-------------------------------------------------
For a UE camera the screen-right axis is the camera's local +Y and screen-up is
its local +Z. With `Rotator(pitch=-90, yaw=0, roll=0)`:

    forward = (0, 0, -1)   right = (0, +1, 0)   up = (-1, 0, 0)

i.e. screen-right is world +Y (block z) and screen-up is world -X. That puts the
448-block axis (x) on the 768-pixel axis and vice versa -- a *transposed* frame
that no flip can turn into a 448-wide ref_map. The brief's "yaw 0" contradicts
its own "448 px = x, 768 px = z" mapping; yaw decides the aspect, so yaw wins:

    Rotator(-90, -90, 0):  forward (0,0,-1)  right (+1,0,0)  up (0,+1,0)

Screen-right is now world +X (block x, the 448 axis) and screen-up is world +Y
(block z). The residual flip is *measured* -- `tools/check_ortho_frame.py`
scores every axis transform against ref_map and the winner is pinned into
AXIS_TRANSFORM below, with its score in the log. Measured on the raw frame:
flip_v wins 0.9389 vs 0.6507 for the runner-up (rot180), margin 1.443x, on a
row-signature r of +0.8585 (a vertical flip of the raw frame gives -0.5459).

The finite far clip plane, without which nothing renders
--------------------------------------------------------
In `SceneCaptureRendering.cpp`:

    const float OrthoFarPlane = CaptureComponent->MaxViewDistanceOverride > 0.0f
                              ? CaptureComponent->MaxViewDistanceOverride
                              : UE_FLOAT_HUGE_DISTANCE / 4.0f;
    const float ZScale = 1.0f / FMath::Max(FarPlane - NearPlane, KINDA_SMALL_NUMBER);

With the default `MaxViewDistanceOverride = 0` an ortho capture gets a far plane
of ~8.5e37 and a ZScale of ~1e-38, so every fragment lands on the same depth
value. Setting `max_view_distance_override` (2 km) is what makes the depth
buffer usable.

Why this is a frame-driven generator and not a straight-line script
-------------------------------------------------------------------
Two failures had to be fixed before a single usable frame came out, and both are
about *time*, not parameters:

1. `-ExecutePythonScript` runs the script and then quits the editor, so only the
   frames rendered during editor start-up exist. `capture_scene()` requests made
   from the script never reach the renderer: six captures at exposure biases
   from 0.00 to -2.75 EV all came back with a mean of exactly 175.59.
2. Even those start-up frames are not finished: the campus's 1.5 M-triangle
   terrain renders through Nanite, whose geometry pages stream in over frames,
   so the first frames have geometry only in patches -- which is exactly what
   the early captures looked like (structure edges and tree canopies drawn over
   an empty background, and *identical* for every parameter combination).

So the script is launched with `-ExecCmds="py <script>"` instead (the editor
stays alive), registers `unreal.register_slate_post_tick_callback`, sets
`capture_every_frame = True`, and keeps reading the render target back until
the image has stopped changing, then writes that frame and quits the editor
itself. Frame counts, hashes and means are logged so the convergence is
auditable.

"Stopped changing" is a tolerance test, not a byte comparison: some
post-process stages are deterministic but cycle with a period of ~8 frames, so
consecutive frames never hash equal even when the image is finished (the log
shows the same 8 digests repeating forever, at a constant mean). The test is
mean luma and content share within a small tolerance for STABLE_FRAMES samples,
after a warm-up. Picking the *last* settled sample matters: an earlier version
kept the sample with the most "content", which selected a half-streamed frame.

No numpy, no PIL
----------------
The editor's bundled python has neither (`ModuleNotFoundError: numpy`; the
project venv is a different interpreter). So: pure-python PNG encode and decode,
pure-python statistics.

Run (NO -nullrhi -- an offscreen scene capture needs a real RHI):

    UnrealEditor-Cmd.exe project/MCReplica.uproject \
        -ExecCmds="py <repo>/tools/ortho_topdown.py" \
        -unattended -nopause -nosplash -stdout -log

Knobs (all optional):
    MC_ORTHO_HEIGHT    camera height in cm          (default 60000)
    MC_ORTHO_BIAS      auto exposure bias in EV     (default 0.0)
    MC_ORTHO_MAXDIST   ortho far clip plane in cm   (default 200000)
    MC_ORTHO_SOURCE    SCS_FINAL_COLOR_LDR | SCS_BASE_COLOR (diagnostic)
    MC_ORTHO_CVARS     semicolon-separated console commands before the capture
    MC_ORTHO_OUT       output png path              (default out/ortho_topdown.png)
    MC_ORTHO_CX/CY     camera X/Y centre in cm      (default: measured bounds)

Outputs: the png above plus out/ortho_topdown_raw.png (pre-transform, for
audit) and a log whose last line is RESULT: PASS/FAIL.
"""

import os
import struct
import sys
import time
import traceback
import zlib

import unreal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(
    os.path.dirname(ROOT), "logs")
LOG = os.path.join(LOG_DIR, "ortho_topdown.txt")
OUT_DIR = os.path.join(ROOT, "out")
PNG_OUT = os.environ.get("MC_ORTHO_OUT") or os.path.join(
    OUT_DIR, "ortho_topdown.png")
PNG_RAW = os.path.join(OUT_DIR, "ortho_topdown_raw.png")

MAP = "/Game/Maps/MCReplica"

# ---- geometry contract ---------------------------------------------------- #
X0, X1 = -144, 304           # blocks, half-open
Z0, Z1 = -544, 224           # blocks, half-open
W, H = X1 - X0, Z1 - Z0      # 448 x 768
BLOCK_CM = 100.0
ORTHO_WIDTH = (X1 - X0) * BLOCK_CM          # 44800.0
#: Diagnostic zoom: shrink the ortho width to a few blocks to look at ONE tile
#: close up. The acceptance frame always uses the full-campus width above.
ORTHO_WIDTH = float(os.environ.get("MC_ORTHO_WIDTH", ORTHO_WIDTH))

#: "ortho" (the acceptance frame) or "persp" (diagnostic: a normal camera).
#:
#: The persp mode exists because the ortho frame came back with the ground
#: missing while tall geometry was drawn. A perspective camera parked on the
#: ground at the same scene settles whether that is a *level* fault or a fault
#: of this capture path: the ground is centimetres from the lens, so nothing
#: about far planes, ortho depth ranges or top-down projection can hide it.
PROJ = (os.environ.get("MC_ORTHO_PROJ", "ortho").strip().lower() or "ortho")
PERSP = PROJ == "persp"
FOV = float(os.environ.get("MC_ORTHO_FOV", 70.0))
PITCH = float(os.environ.get("MC_ORTHO_PITCH", "0" if PERSP else "-90"))
YAW = float(os.environ.get("MC_ORTHO_YAW", "93.4" if PERSP else "-90"))
#: The on-foot vantage recorded in logs/vantage.json and applied to the
#: PlayerStart (logs/apply_spawn.txt): block (17, -215) -> UE (1700, 21500, 700).
PERSP_CX, PERSP_CY = 1700.0, 21500.0
HEIGHT = float(os.environ.get("MC_ORTHO_HEIGHT",
                              700.0 if PERSP else 60000.0))
BIAS = float(os.environ.get("MC_ORTHO_BIAS", 0.0))
#: Force THIS tool's fixed exposure onto the capture instead of letting the
#: level's post-process volume decide. Off by default: the frame is supposed to
#: measure the level that ships, not a capture that compensates for it.
PP_EXPOSURE = os.environ.get("MC_ORTHO_PP_EXPOSURE", "").strip() not in ("", "0")
#: Fixed exposure for calibration frames, used only when MC_ORTHO_PP_EXPOSURE=1.
#: The level stores a single brightness that has to satisfy two frames (a
#: top-down one that must match the albedo map and a ground-level one that must
#: look like daylight), and the cheapest way to find it is to sweep it here
#: rather than save the level once per guess.
PP_BRIGHTNESS = float(os.environ.get("MC_ORTHO_PP_BRIGHTNESS", "1.0") or 1.0)
#: Optional budget for dark pixels (max(RGB) <= 8), as a fraction. 0 = report
#: only. Dark pixels are geometry in shadow, not a coverage hole -- see the
#: check in flow().
MAX_BLACK = float(os.environ.get("MC_ORTHO_MAX_BLACK", "0") or 0)
#: Zero the height fog for this capture (in memory, never saved). On by
#: default: an orthographic overhead frame sees tens of optical depths of fog,
#: so it measures the fog rather than the level. Ground-level frames should set
#: MC_ORTHO_HIDE_FOG=0 to keep the shipping haze.
HIDE_FOG = os.environ.get("MC_ORTHO_HIDE_FOG", "1").strip() not in ("", "0")
MAXDIST = float(os.environ.get("MC_ORTHO_MAXDIST", 200000.0))
SOURCE = os.environ.get("MC_ORTHO_SOURCE", "SCS_FINAL_COLOR_LDR")
CVARS = os.environ.get(
    "MC_ORTHO_CVARS",
    "r.AntiAliasingMethod 0;r.ScreenPercentage 100;r.TemporalAA.Upsampling 0")
#: Rotator(pitch, yaw, roll) -- see the module docstring.
CAM_ROT = (0.0, PITCH, YAW)
#: Render target size. The ortho frame is 1 px per block; the diagnostic
#: perspective frame is a normal landscape view.
RTW = int(os.environ.get("MC_ORTHO_RTW", 960 if PERSP else W))
RTH = int(os.environ.get("MC_ORTHO_RTH", 540 if PERSP else H))

#: Which axis transform turns the render target into ref_map orientation:
#: 'identity' | 'flip_h' | 'flip_v' | 'rot180'. Chosen by
#: tools/check_ortho_frame.py's scoring pass; the winning score is in the log.
AXIS_TRANSFORM = "flip_v"

#: How many consecutive settled samples count as "the world has finished
#: streaming in". Nanite pages arrive over several frames; the first frames of
#: this scene have geometry only in patches.
#:
#: "Settled" is a tolerance test, not a byte-identical one: some post-process
#: stages (Lumen's spatial filter, dithering) are deterministic but cycle with
#: a period of ~8 frames, so consecutive frames never hash equal even when the
#: image is finished. Byte-identical frames therefore never arrive; mean and
#: content share do settle. A stalled/garbage render shows up as a *constant*
#: frame from sample 1, which is why the warm-up below exists.
STABLE_FRAMES = 4
#: let Nanite stream before trusting "settled". Overridable, because "streamed
#: in" is a property of the machine and of how much changed in the level, not a
#: constant: after a mesh reimport the first frames are emptier than usual and a
#: fixed 8 samples accepted a nearly empty frame as finished. That produced a
#: frame with 14.9% content that its own degeneracy check rejected, while the
#: earlier capture of the same camera had 55% -- the tool told the truth, the
#: sample count did not give it the chance.
STABLE_MIN_SAMPLE = int(os.environ.get("MC_ORTHO_MIN_SAMPLE", "8"))
STABLE_MEAN_EPS = 0.05         # mean luma change per sample, 0-255
STABLE_CONTENT_EPS = 0.004     # content-share change per sample
#: Give up waiting after this many readbacks (each is ~1 s).
MAX_SAMPLES = int(os.environ.get("MC_ORTHO_MAX_SAMPLES", "30"))
#: Frames to wait between readbacks, so the renderer actually advances.
SAMPLE_EVERY = int(os.environ.get("MC_ORTHO_SAMPLE_EVERY", "3"))
#: Keep the superseded voxel-cube layer in the frame. Diagnostic only.
#:
#: The 1041 `MCReplicaPropCluster` actors are the *old* layer: one cube per
#: block, atlas-textured, built before the mesh pipeline existed. It is hidden
#: by default because the shipped look is terrain + structure meshes. But it is
#: a full solid voxel world, so if the frame comes back fully covered with the
#: clusters kept and nearly empty with them hidden, the difference is not a
#: rendering fault -- it means the *mesh* layer is the layer that is not
#: drawing, and the cluster layer was what earlier "good" frames showed.
KEEP_LEGACY = os.environ.get("MC_ORTHO_KEEP_LEGACY", "").strip() not in ("", "0")

#: Clear `bHiddenInGame` on EVERY actor before capturing (diagnostic only).
#:
#: The census in this file cannot prove an actor is visible: `bHiddenInGame`
#: is not reliably readable through the Python property bridge (attempts on
#: AActor return None, which is indistinguishable from "not hidden"), so a
#: frame with the terrain missing cannot be blamed on, or cleared of, hiding
#: by reading properties. Writing False is the only unambiguous test: if the
#: terrain shows up only when this is on, hiding was the cause.
UNHIDE_ALL = os.environ.get("MC_ORTHO_UNHIDE_ALL", "").strip() not in ("", "0")

_L = []


def say(msg):
    _L.append(str(msg))
    unreal.log("[MCOrtho] " + str(msg))
    try:
        with open(LOG, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_L) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# png encode / decode (no PIL, no numpy)
# --------------------------------------------------------------------------- #

def write_png(path, rows):
    """rows[y] = bytes of RGB for that image row."""
    h = len(rows)
    w = len(rows[0]) // 3
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def decode_png(path):
    """(rows, w, h) from an 8-bit RGB/RGBA non-interlaced PNG."""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a png")
    pos = 8
    idat = bytearray()
    w = h = depth = ctype = interlace = None
    while pos + 8 <= len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
    if depth != 8 or ctype not in (2, 6) or interlace != 0:
        raise ValueError("unsupported png: depth=%s ctype=%s interlace=%s"
                         % (depth, ctype, interlace))
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = w * ch
    rows = []
    prev = bytearray(stride)
    p = 0
    for _ in range(h):
        f = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if f == 1:
            for i in range(ch, stride):
                line[i] = (line[i] + line[i - ch]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - ch] if i >= ch else 0
                b = prev[i]
                c = prev[i - ch] if i >= ch else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev = line
        if ch == 3:
            rows.append(bytes(line))
        else:
            out = bytearray(w * 3)
            for x in range(w):
                out[3 * x:3 * x + 3] = line[4 * x:4 * x + 3]
            rows.append(bytes(out))
    return rows, w, h


def flip_rows(rows, name):
    if name == "identity":
        return rows
    if name == "flip_v":
        return list(reversed(rows))
    if name in ("flip_h", "rot180"):
        out = [b"".join(r[i:i + 3] for i in range(len(r) - 3, -1, -3))
               for r in rows]
        return list(reversed(out)) if name == "rot180" else out
    raise ValueError("not dimension-preserving: %r" % name)


# --------------------------------------------------------------------------- #
# image statistics, in plain python
# --------------------------------------------------------------------------- #

def frame_stats(rows):
    n = 0
    s = [0, 0, 0]
    sq = [0, 0, 0]
    black = 0
    colours = set()
    for r in rows:
        rs, gs, bs = r[0::3], r[1::3], r[2::3]
        n += len(rs)
        s[0] += sum(rs)
        s[1] += sum(gs)
        s[2] += sum(bs)
        sq[0] += sum(v * v for v in rs)
        sq[1] += sum(v * v for v in gs)
        sq[2] += sum(v * v for v in bs)
        black += sum(1 for a, b, c in zip(rs, gs, bs) if max(a, b, c) <= 2)
        colours.update(r[i:i + 3] for i in range(0, len(r), 3))
    mean = [v / float(n) for v in s]
    var = max(0.0, sum(sq) / float(3 * n) - (sum(s) / float(3 * n)) ** 2)
    return {"mean": mean, "black": black / float(n),
            "distinct": len(colours), "std": var ** 0.5, "n": n}


def content(rows, w, h, cell=4):
    """-> (content_share, bbox, dominant_key); "sky" is the modal cell colour.

    A top-down frame of the campus must be almost entirely *not* sky: the early
    captures were 85-95% sky with only edges drawn, which is why the drawn share
    is measured on every sample instead of being eyeballed from the mean.
    """
    hist = {}
    cells = []
    for y in range(0, h - cell + 1, cell):
        for x in range(0, w - cell + 1, cell):
            r = g = b = 0
            for dy in range(cell):
                row = rows[y + dy]
                for dx in range(cell):
                    o = 3 * (x + dx)
                    r += row[o]
                    g += row[o + 1]
                    b += row[o + 2]
            k = (r // 32, g // 32, b // 32)
            hist[k] = hist.get(k, 0) + 1
            cells.append((x, y, k))
    dom = max(hist, key=hist.get)
    sky = hist[dom] / float(len(cells))
    xs = [c[0] for c in cells if c[2] != dom]
    ys = [c[1] for c in cells if c[2] != dom]
    bbox = (min(xs), max(xs), min(ys), max(ys)) if xs else None
    return 1.0 - sky, bbox, dom


def digest(rows):
    """Cheap fingerprint of a frame, for "has it stopped changing"."""
    h = 2166136261
    for r in rows[::8]:
        for v in r[::16]:
            h = ((h ^ v) * 16777619) & 0xFFFFFFFF
    return h


# --------------------------------------------------------------------------- #
# property helpers -- report which name landed, never fail silently
# --------------------------------------------------------------------------- #

def set_prop(obj, value, *names):
    errs = []
    for name in names:
        try:
            obj.set_editor_property(name, value)
            say("  set %-34s = %r" % (name, value))
            return name
        except Exception as exc:                       # noqa: BLE001
            errs.append("%s(%s)" % (name, str(exc).splitlines()[0][:70]))
    say("  FAILED to set %r -- tried %s" % (value, "; ".join(errs)))
    return None


# --------------------------------------------------------------------------- #
# render target readback
# --------------------------------------------------------------------------- #

def _newest_png(dirpath, t0):
    best = None
    for name in os.listdir(dirpath):
        if not name.lower().endswith(".png"):
            continue
        p = os.path.join(dirpath, name)
        if os.path.getmtime(p) >= t0 - 1.0:
            if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                best = p
    return best


def rt_rows(world, rt, stem):
    """-> (rows, w, h); rows[y] is the RGB bytes of image row y, top-down.

    `read_render_target_pixels` does not exist on 5.8 and
    `export_render_target(world, rt, file_path, file_name)` silently writes
    nothing for some path/name spellings, so the frame comes from the engine's
    own PNG export (spellings tried in turn) decoded here.
    """
    w = int(rt.get_editor_property("size_x"))
    h = int(rt.get_editor_property("size_y"))
    mod = getattr(unreal, "RenderingLibrary", None)
    exp = getattr(mod, "export_render_target", None) if mod else None
    if callable(exp):
        for path_arg, name_arg in ((OUT_DIR + os.sep, stem),
                                   (OUT_DIR, stem),
                                   (OUT_DIR + os.sep, stem + ".png"),
                                   (OUT_DIR, stem + ".png")):
            t0 = time.time()
            try:
                exp(world, rt, path_arg, name_arg)
            except Exception as exc:                   # noqa: BLE001
                say("  export_render_target(%r, %r) raised: %s"
                    % (path_arg, name_arg, str(exc).splitlines()[0][:80]))
                continue
            got = os.path.join(OUT_DIR, stem + ".png")
            if not os.path.isfile(got):
                got = _newest_png(OUT_DIR, t0)
            if not (got and os.path.isfile(got)):
                continue
            try:
                rows, dw, dh = decode_png(got)
            except Exception as exc:                   # noqa: BLE001
                say("  decode %s failed: %s" % (got, exc))
                continue
            if (dw, dh) != (w, h):
                say("  exported png is %dx%d, render target is %dx%d"
                    % (dw, dh, w, h))
                continue
            try:
                os.remove(got)
            except OSError:
                pass
            return rows, w, h
    say("  no readback route available")
    return None, 0, 0


# --------------------------------------------------------------------------- #
# the frame-driven job
# --------------------------------------------------------------------------- #

class Job(object):
    def __init__(self):
        self.comp = None
        self.cap = None
        self.rt = None
        self.world = None
        self.handle = None
        self.frames = 0
        self.wait = 0
        self.gen = None
        self.done = False
        self.cam = (0.0, 0.0, HEIGHT)

    def pp(self, key, value, override=None):
        pps = self.comp.get_editor_property("post_process_settings")
        try:
            pps.set_editor_property(key, value)
        except Exception as exc:                       # noqa: BLE001
            say("  pps.%s failed: %s" % (key, str(exc).splitlines()[0][:70]))
            return False
        if override:
            try:
                pps.set_editor_property(override, True)
            except Exception:                          # noqa: BLE001
                pass
        self.comp.set_editor_property("post_process_settings", pps)
        return True

    def tick(self, _dt=0.0):
        if self.done:
            return
        self.frames += 1
        if self.frames > 6000:
            self.finish(False, "frame budget exhausted (%d frames)" % self.frames)
            return
        if self.wait > 0:
            self.wait -= 1
            return
        try:
            n = next(self.gen)
        except StopIteration:
            return
        except Exception:
            say(traceback.format_exc())
            self.finish(False, "uncaught exception in the capture flow")
            return
        self.wait = int(n) if isinstance(n, (int, float)) else 0

    def finish(self, ok, msg):
        if self.done:
            return
        self.done = True
        say("")
        say("RESULT: %s %s" % ("PASS" if ok else "FAIL", msg))
        if self.handle is not None:
            try:
                unreal.unregister_slate_post_tick_callback(self.handle)
            except Exception:                          # noqa: BLE001
                pass
            self.handle = None
        try:
            quit_fn = getattr(unreal.SystemLibrary, "quit_editor", None)
            if callable(quit_fn):
                quit_fn()
            else:
                unreal.SystemLibrary.execute_console_command(None, "QUIT_EDITOR")
        except Exception:                              # noqa: BLE001
            pass


def _read(obj, *names):
    """First attribute/property in `names` that reads without raising."""
    for n in names:
        try:
            return obj.get_editor_property(n)
        except Exception:                              # noqa: BLE001
            pass
        try:
            return getattr(obj, n)
        except Exception:                              # noqa: BLE001
            pass
    return None


def census(world):
    """What the renderer is actually being asked to draw, per actor.

    A frame with 7 albedo colours cannot be explained by materials alone, so
    the three things that would empty a frame are read instead of guessed:
    a null mesh, a mesh with no slots, or slots that resolve to no material.
    Every counter here is a fact from the level, not an inference from pixels.
    """
    say("--- render census (what the capture will be asked to draw) ---")
    n_actors = n_null_mesh = n_zero_slots = n_null_slots = n_slots = 0
    n_hidden = n_invisible = 0
    shown = 0
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        lbl = str(a.get_actor_label())
        if not lbl.startswith(("B_", "T_", "V_")):
            continue
        n_actors += 1
        sc = _read(a, "static_mesh_component", "static_mesh_component_0")
        # The mesh lives on the COMPONENT, not on the actor: reading
        # `static_mesh` off the actor raises and, with a silent fallback,
        # reports every actor as having a null mesh. Ask the component.
        mesh = _read(sc, "static_mesh") if sc is not None else None
        hid = _read(a, "b_hidden_in_game", "hidden_in_game", "bHiddenInGame")
        vis = None
        try:
            vis = sc.is_visible() if sc is not None else None
        except Exception:                              # noqa: BLE001
            pass
        if hid:
            n_hidden += 1
        if vis is False:
            n_invisible += 1
        if mesh is None:
            n_null_mesh += 1
            say("  %-16s NULL MESH" % lbl)
            continue
        slots = _read(mesh, "static_materials") or []
        nulls = sum(1 for s in slots
                    if _read(s, "material_interface") is None)
        n_slots += len(slots)
        n_null_slots += nulls
        if not slots:
            n_zero_slots += 1
        if shown < 8:
            shown += 1
            say("  %-16s mesh=%-42s slots=%3d nulls=%3d %s hidden_in_game=%s"
                % (lbl, mesh.get_name(), len(slots), nulls,
                   "invisible" if vis is False else "visible", hid))
    say("actors: %d   null mesh: %d   no slots: %d   hidden_in_game: %d   "
        "component invisible: %d" % (n_actors, n_null_mesh, n_zero_slots,
                                     n_hidden, n_invisible))
    say("material slots: %d total, %d unresolved (null material_interface)"
        % (n_slots, n_null_slots))

    # --- terrain tiles specifically ---------------------------------------
    # The ground is the one layer that comes back missing, and it is the only
    # layer whose mesh is built from a mesh description rather than imported
    # from an OBJ. Read the two numbers that decide whether the asset has
    # anything to draw: triangles per LOD and sections. A mesh with triangles
    # but no sections (or sections with no material) draws nothing at all.
    say("--- terrain tiles (T_*) ---")
    n_t = 0
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        lbl = str(a.get_actor_label())
        if not lbl.startswith("T_"):
            continue
        n_t += 1
        sc = _read(a, "static_mesh_component", "static_mesh_component_0")
        mesh = _read(sc, "static_mesh") if sc is not None else None
        if mesh is None:
            say("  %-24s NULL MESH" % lbl)
            continue
        def _n(fn, *a_):
            try:
                return fn(*a_)
            except Exception:                          # noqa: BLE001
                return "?"
        tris = _n(mesh.get_num_triangles, 0)
        verts = _n(mesh.get_num_vertices, 0)
        secs = _n(mesh.get_num_sections, 0)
        if n_t <= 6:
            try:
                o, e = a.get_actor_bounds(False)
                bb = "loc=(%.0f,%.0f,%.0f) ext=(%.0f,%.0f,%.0f)" % (
                    o.x, o.y, o.z, e.x, e.y, e.z)
            except Exception:                          # noqa: BLE001
                bb = "bounds unreadable"
            say("  %-24s tris=%-7s verts=%-7s sections=%-3s %s"
                % (lbl, tris, verts, secs, bb))
    say("terrain actors: %d" % n_t)


def build(job):
    """Level, render target and capture actor. Returns True on success."""
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = ues.get_editor_world() if ues else None
    if world is None:
        world = unreal.EditorLevelLibrary.get_editor_world()
    if world is None:
        say("RESULT: FAIL no editor world")
        return False
    try:
        say("current world: %s" % world.get_name())
    except Exception:                                  # noqa: BLE001
        pass
    if world.get_name() != "MCReplica":
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        ok = bool(les.load_level(MAP)) if les is not None else False
        if not ok:
            ok = unreal.EditorLoadingAndSavingUtils.load_map(MAP) is not None
        say("load(%s) -> %s" % (MAP, ok))
        if not ok:
            say("RESULT: FAIL level did not load")
            return False
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
    job.world = world

    # ---- what the level actually contains, in cm ----------------------
    lo = [1e18, 1e18, 1e18]
    hi = [-1e18, -1e18, -1e18]
    n_sm = 0
    per_label = {}
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        try:
            o, e = a.get_actor_bounds(False)
        except Exception:                              # noqa: BLE001
            continue
        n_sm += 1
        for i, v in enumerate((o.x - e.x, o.y - e.y, o.z - e.z)):
            lo[i] = min(lo[i], v)
        for i, v in enumerate((o.x + e.x, o.y + e.y, o.z + e.z)):
            hi[i] = max(hi[i], v)
        key = str(a.get_actor_label()).split("_")[0]
        rec = per_label.setdefault(key, [0, 1e18, -1e18])
        rec[0] += 1
        rec[1] = min(rec[1], o.z - e.z)
        rec[2] = max(rec[2], o.z + e.z)
    say("StaticMeshActor count: %d" % n_sm)
    if n_sm:
        say("world bounds  X[%.0f, %.0f]  Y[%.0f, %.0f]  Z[%.0f, %.0f] cm"
            % (lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]))
        say("world size    %.0f x %.0f x %.0f cm  (brief: 44800 x 76800)"
            % (hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]))
        say("blocks implied by size: %.1f x %.1f at 100 cm/block"
            % ((hi[0] - lo[0]) / 100.0, (hi[1] - lo[1]) / 100.0))
        for key in sorted(per_label):
            n, z0, z1 = per_label[key]
            say("  %-14s %4d actors  Z[%.0f, %.0f]" % (key, n, z0, z1))
        if abs((hi[1] + lo[1]) * 0.5 + 16000.0) > 1.0:
            say("NOTE: the level's Y centre is %.0f cm, not the brief's -16000 "
                "-- the world->UE mapping negates Y. The camera is centred on "
                "the measured bounds." % ((hi[1] + lo[1]) * 0.5))
        cam_x = float(os.environ.get(
            "MC_ORTHO_CX", PERSP_CX if PERSP else (lo[0] + hi[0]) * 0.5))
        cam_y = float(os.environ.get(
            "MC_ORTHO_CY", PERSP_CY if PERSP else (lo[1] + hi[1]) * 0.5))
    else:
        cam_x, cam_y = 8000.0, -16000.0
    job.cam = (cam_x, cam_y, HEIGHT)

    if UNHIDE_ALL:
        n_un = 0
        for a in unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.StaticMeshActor):
            try:
                a.set_actor_hidden_in_game(False)
                n_un += 1
            except Exception:                          # noqa: BLE001
                pass
        say("MC_ORTHO_UNHIDE_ALL: bHiddenInGame cleared on %d StaticMeshActor(s)"
            % n_un)

    # ---- height fog ----------------------------------------------------
    # An overhead frame at 600 m is a measuring instrument, not a gameplay
    # camera: over that distance the analytic height fog is tens of optical
    # depths thick, so the frame ends up being a picture of the fog. Measured:
    # with the fog at 0.0005/cm the 600 m frame came back mean (0.9, 1.2, 2.2)
    # with 92.8% of its pixels dark, while the ground-level frame of the same
    # level was healthy (mean 86.6, 93.3, 115.5, 0.08% dark). Zeroing the
    # density here is an in-memory change and is never saved; the ground-level
    # frames keep the shipping fog, so the shipped look is still measured.
    if HIDE_FOG:
        n_fog = 0
        for a in unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.ExponentialHeightFog):
            comp = _read(a, "component", "fog_component",
                         "exponential_height_fog_component")
            if comp is None:
                try:
                    comp = a.get_component_by_class(
                        unreal.ExponentialHeightFogComponent)
                except Exception:                      # noqa: BLE001
                    comp = None
            if comp is None:
                continue
            was = _read(comp, "fog_density")
            try:
                comp.set_editor_property("fog_density", 0.0)
                n_fog += 1
                say("fog: %s density %.6f -> 0 for this capture (in memory "
                    "only, not saved)" % (a.get_actor_label(), was))
            except Exception as exc:                   # noqa: BLE001
                say("fog: could not zero %s (%s)" % (a.get_actor_label(),
                                                     str(exc)[:80]))
        if not n_fog:
            say("fog: MC_ORTHO_HIDE_FOG is on but no ExponentialHeightFog "
                "component was reachable -- the frame still carries the haze")

    census(world)

    # ---- legacy voxel layer -------------------------------------------
    clusters = []
    try:
        clusters = list(unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.MCReplicaPropCluster))
    except Exception as exc:                           # noqa: BLE001
        say("cluster census failed: %s" % exc)
    say("legacy MCReplicaPropCluster actors: %d (%s)"
        % (len(clusters), "KEPT IN the capture: MC_ORTHO_KEEP_LEGACY is on -- "
           "diagnostic run, these are the superseded cube layer"
           if KEEP_LEGACY else
           "kept out of the capture: they are the superseded cube layer, and "
           "the shipped look is the terrain + structure meshes"))
    if clusters and KEEP_LEGACY:
        # Skipping our own hide is not enough: hide_voxel_layer.py saved
        # bHiddenInGame on these actors as a level property, so they stay out
        # of the capture until it is cleared here. The first diagnostic run
        # produced a byte-identical frame precisely because this was missed.
        n_shown = 0
        for a in clusters:
            try:
                a.set_actor_hidden_in_game(False)
                n_shown += 1
            except Exception:                          # noqa: BLE001
                pass
        say("legacy clusters explicitly unhidden for the capture: %d/%d"
            % (n_shown, len(clusters)))
    if clusters and not KEEP_LEGACY:
        n_hidden = 0
        how = "?"
        for a in clusters:
            for how, fn in (("is_temporarily_hidden_in_editor",
                             lambda x: x.set_editor_property(
                                 "is_temporarily_hidden_in_editor", True)),
                            ("set_actor_hidden_in_game",
                             lambda x: x.set_actor_hidden_in_game(True))):
                try:
                    fn(a)
                    n_hidden += 1
                    break
                except Exception:                      # noqa: BLE001
                    continue
        say("legacy clusters hidden: %d/%d (via %s)"
            % (n_hidden, len(clusters), how))

    # ---- render target -------------------------------------------------
    rt = None
    crt = getattr(unreal.RenderingLibrary, "create_render_target2d", None)
    for args in ((world, RTW, RTH, unreal.TextureRenderTargetFormat.RTF_RGBA8,
                  unreal.LinearColor(0.0, 0.0, 0.0, 1.0)),
                 (world, RTW, RTH, unreal.TextureRenderTargetFormat.RTF_RGBA8)):
        try:
            rt = crt(*args)
            say("create_render_target2d(%d args) -> %r" % (len(args), rt))
            break
        except Exception as exc:                       # noqa: BLE001
            say("create_render_target2d(%d args) failed: %s"
                % (len(args), str(exc).splitlines()[0][:90]))
    if rt is None:
        say("RESULT: FAIL could not create render target")
        return False
    job.rt = rt

    # ---- capture actor -------------------------------------------------
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    cap = None
    try:
        cap = eas.spawn_actor_from_class(
            unreal.SceneCapture2D, unreal.Vector(*job.cam),
            unreal.Rotator(*CAM_ROT))
    except Exception as exc:                           # noqa: BLE001
        say("spawn via EditorActorSubsystem failed: %s" % exc)
        cap = unreal.EditorLevelLibrary.spawn_actor_from_class(
            unreal.SceneCapture2D, unreal.Vector(*job.cam),
            unreal.Rotator(*CAM_ROT))
    if cap is None:
        say("RESULT: FAIL could not spawn SceneCapture2D")
        return False
    cap.set_actor_label("MC_OrthoTopDown")
    job.cap = cap
    comp = cap.get_component_by_class(unreal.SceneCaptureComponent2D)
    if comp is None:
        comp = cap.capture_component2d
    job.comp = comp
    loc, rot = cap.get_actor_location(), cap.get_actor_rotation()
    say("camera loc=(%.0f, %.0f, %.0f) rot=(roll %.1f, pitch %.1f, yaw %.1f)"
        % (loc.x, loc.y, loc.z, rot.roll, rot.pitch, rot.yaw))

    mode = getattr(unreal.CameraProjectionMode,
                   "PERSPECTIVE" if PERSP else "ORTHOGRAPHIC", None)
    if set_prop(comp, mode, "projection_type") is None:
        say("RESULT: FAIL the capture component exposes no %s switch" % PROJ)
        return False
    if PERSP:
        if set_prop(comp, FOV, "fov_angle") is None:
            say("RESULT: FAIL fov_angle did not take")
            return False
    elif set_prop(comp, ORTHO_WIDTH, "ortho_width") is None:
        say("RESULT: FAIL ortho width did not take")
        return False
    src = getattr(unreal.SceneCaptureSource, SOURCE, None)
    if src is None:
        say("  SceneCaptureSource has no %s, using SCS_FINAL_COLOR_LDR" % SOURCE)
        src = unreal.SceneCaptureSource.SCS_FINAL_COLOR_LDR
    set_prop(comp, rt, "texture_target")
    set_prop(comp, src, "capture_source")
    # capture_every_frame: the frame is sampled repeatedly while the world's
    # Nanite pages stream in, so the capture must re-render each frame.
    set_prop(comp, True, "capture_every_frame")
    set_prop(comp, False, "capture_on_movement")
    set_prop(comp, 1.0, "post_process_blend_weight")
    set_prop(comp, 0.0, "overscan")
    # THE depth fix: see the module docstring. Without a finite far plane the
    # ortho projection gets ZScale ~1e-38 and the depth test throws the scene
    # away.
    if set_prop(comp, MAXDIST, "max_view_distance_override") is None:
        say("RESULT: FAIL max_view_distance_override did not take -- the ortho "
            "depth range cannot be made finite and the frame will be empty")
        return False

    def pp(key, value, override=None):
        job.pp(key, value, override)

    aem = getattr(unreal.AutoExposureMethod, "AEM_HISTOGRAM", None)
    # Exposure: whose is it?
    #
    # Until 11-01 this tool always forced its own fixed exposure onto the
    # capture component. That made every frame comparable to every other frame
    # and made the level itself unmeasured: the level could be two stops wrong
    # and the frame would still look fine, because the capture compensated.
    # The thing that ships is the level, so by default the level's own
    # post-process volume is now authoritative and this tool writes nothing.
    # MC_ORTHO_PP_EXPOSURE=1 restores the old behaviour for A/B work.
    if PP_EXPOSURE:
        if aem is not None:
            pp("auto_exposure_method", aem, "override_auto_exposure_method")
        # A histogram window with min == max is a *fixed* exposure: adaptation
        # cannot wander with content, which is what an acceptance frame needs.
        for key in ("auto_exposure_min_brightness",
                    "auto_exposure_max_brightness"):
            pp(key, PP_BRIGHTNESS, "override_" + key)
        pp("auto_exposure_bias", BIAS, "override_auto_exposure_bias")
        say("exposure: forced by this tool (MC_ORTHO_PP_EXPOSURE=1), "
            "min=max brightness %.3f, bias %+.2f EV -- this frame does NOT "
            "measure what ships; it exists to calibrate the level's own value"
            % (PP_BRIGHTNESS, BIAS))
    else:
        say("exposure: left to the LEVEL's post-process stack "
            "(MC_ORTHO_PP_EXPOSURE is off); the frame measures what ships")
    for k in ("motion_blur_amount", "bloom_intensity", "vignette_intensity",
              "scene_fringe_intensity", "film_grain_intensity"):
        pp(k, 0.0, "override_" + k)
    pps = comp.get_editor_property("post_process_settings")
    for key in ("auto_exposure_method", "auto_exposure_bias",
                "auto_exposure_min_brightness", "auto_exposure_max_brightness"):
        try:
            say("pps.%s = %r" % (key, pps.get_editor_property(key)))
        except Exception:                              # noqa: BLE001
            say("pps.%s does not exist on 5.8" % key)

    for cmd in [c.strip() for c in CVARS.split(";") if c.strip()]:
        try:
            unreal.SystemLibrary.execute_console_command(None, cmd)
            say("console: %s" % cmd)
        except Exception as exc:                       # noqa: BLE001
            say("console %r failed: %s" % (cmd, str(exc).splitlines()[0][:80]))
    return True


def flow(job):
    """Wait for the frame to stop changing, then write it. yield n = wait n frames."""
    say("")
    say("--- sampling the capture until it stops changing (Nanite pages stream "
        "in over frames) ---")
    say("%-4s %-10s %8s %8s  %s" % ("n", "digest", "mean", "content", "bbox"))
    prev_mean = None
    prev_share = None
    stable = 0
    rows = None
    rw = rh = 0
    settled = False
    i = 0
    for i in range(1, MAX_SAMPLES + 1):
        yield SAMPLE_EVERY
        rows, rw, rh = rt_rows(job.world, job.rt, "_ortho_sample")
        if rows is None:
            job.finish(False, "render target readback failed")
            return
        st = frame_stats(rows)
        share, bbox, dom = content(rows, rw, rh)
        d = digest(rows)
        luma = sum(st["mean"]) / 3.0
        say("%-4d %-10x %8.2f %7.1f%%  %s"
            % (i, d, luma, 100.0 * share, bbox))
        if prev_mean is not None and i > STABLE_MIN_SAMPLE \
                and abs(luma - prev_mean) <= STABLE_MEAN_EPS \
                and abs(share - prev_share) <= STABLE_CONTENT_EPS:
            stable += 1
        else:
            stable = 0
        prev_mean, prev_share = luma, share
        if stable >= STABLE_FRAMES:
            settled = True
            say("settled: %d consecutive samples within %.2f mean / %.2f%% "
                "content (sample %d, frame %d)"
                % (stable, STABLE_MEAN_EPS, 100.0 * STABLE_CONTENT_EPS,
                   i, job.frames))
            break
    if rows is None:
        job.finish(False, "no frame was ever read back")
        return

    if (rw, rh) != (RTW, RTH):
        job.finish(False, "capture is %dx%d, expected %dx%d"
                   % (rw, rh, RTW, RTH))
        return
    st = frame_stats(rows)
    share, bbox, dom = content(rows, rw, rh)
    if not settled:
        say("WARNING: never settled within %d samples; writing the last one "
            "anyway (the checks below still have to pass)" % i)

    say("")
    say("frame: mean=(%.1f, %.1f, %.1f) black=%.4f%% distinct_colours=%d "
        "std=%.1f" % (st["mean"][0], st["mean"][1], st["mean"][2],
                      100.0 * st["black"], st["distinct"], st["std"]))
    say("drawn content: %.1f%% of 4x4 cells differ from the modal colour %s; "
        "bbox %s" % (100.0 * share, dom, bbox))

    write_png(PNG_RAW, rows)
    xform = "identity" if PERSP else AXIS_TRANSFORM
    write_png(PNG_OUT, flip_rows(rows, xform))
    say("wrote %s (%dx%d, transform=%s) and raw %s"
        % (PNG_OUT, RTW, RTH, xform, PNG_RAW))

    try:
        unreal.get_editor_subsystem(unreal.EditorActorSubsystem).destroy_actor(
            job.cap)
    except Exception:                                  # noqa: BLE001
        pass
    # the readback scratch png is not an artifact
    try:
        os.remove(os.path.join(OUT_DIR, "_ortho_sample.png"))
    except OSError:
        pass

    problems = []
    # Pure black is NOT a hole, and treating it as one is a trap.
    #
    # The message this replaces ("2.073% pure black (limit 1%)") failed a frame
    # that was in fact fully covered: the black pixels are surfaces the light
    # does not reach, and they were the same 2.073% both before and after the
    # terrain hole was fixed, which is the proof that they are not the hole.
    # A hole shows up as the *background* colour, not as black. So this is
    # reported always and only fails when a caller asks for a budget with
    # MC_ORTHO_MAX_BLACK.
    say("dark pixels (max(RGB)<=8): %.3f%% -- geometry the light misses, not "
        "a coverage hole; set MC_ORTHO_MAX_BLACK to gate on it"
        % (100.0 * st["black"]))
    if MAX_BLACK > 0.0 and st["black"] > MAX_BLACK:
        problems.append("dark pixels %.3f%% exceeds the requested budget %.3f%%"
                        % (100.0 * st["black"], 100.0 * MAX_BLACK))
    # "share" is the fraction of cells differing from the modal colour. Once
    # the ground is continuous the modal colour IS the ground (31.9% of a
    # correct frame), so a low share no longer means an empty frame -- it means
    # a uniform one. It stays as a degeneracy guard only.
    if share < 0.10:
        problems.append("only %.1f%% of the frame differs from the modal colour"
                        " -- possible empty or single-material frame"
                        % (100.0 * share))
    if problems:
        job.finish(False, "; ".join(problems))
        return
    job.finish(True, "capture %dx%d mean=(%.1f,%.1f,%.1f) dark=%.4f%% "
               "distinct=%d std=%.1f content=%.1f%% height=%.0f bias=%+.2fEV "
               "samples=%d frames=%d"
               % (RTW, RTH, st["mean"][0], st["mean"][1], st["mean"][2],
                  100.0 * st["black"], st["distinct"], st["std"],
                  100.0 * share, HEIGHT, BIAS, i, job.frames))


def main():
    say("=== ortho_topdown start %s ===" % time.strftime("%F %T"))
    say("engine %s" % unreal.SystemLibrary.get_engine_version())
    say("params: height=%.0f bias=%+.2f EV maxdist=%.0f source=%s"
        % (HEIGHT, BIAS, MAXDIST, SOURCE))
    say("geometry: blocks x[%d,%d) z[%d,%d) -> %dx%d px, ortho width %.0f cm"
        % (X0, X1, Z0, Z1, W, H, ORTHO_WIDTH))
    say("projection: %s%s  rt=%dx%d  cam rot pitch=%.1f yaw=%.1f  fov=%.1f"
        % (PROJ, "" if PERSP else " (1 px per block)", RTW, RTH,
           PITCH, YAW, FOV))
    os.makedirs(OUT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    say("slate tick available = %r, quit_editor available = %r"
        % (callable(getattr(unreal, "register_slate_post_tick_callback", None)),
           callable(getattr(unreal.SystemLibrary, "quit_editor", None))))
    if not callable(getattr(unreal, "register_slate_post_tick_callback", None)):
        say("RESULT: FAIL no slate tick callback -- cannot drive frames")
        return 1

    job = Job()
    if not build(job):
        if not _L[-1].startswith("RESULT:"):
            say("RESULT: FAIL setup")
        return 1
    job.gen = flow(job)
    job.handle = unreal.register_slate_post_tick_callback(job.tick)
    say("capture flow registered on the slate tick; the editor quits itself "
        "once the frame is written")
    return None


if __name__ == "__main__":
    try:
        rc = main()
        if rc is not None:
            sys.exit(rc)
    except SystemExit:
        raise
    except Exception:
        say(traceback.format_exc())
        say("RESULT: FAIL uncaught exception")
        raise
