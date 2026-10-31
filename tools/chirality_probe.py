# -*- coding: utf-8 -*-
"""
chirality_probe.py -- put a camera at a *known* pose in a real game window, so
that an external window grab (tools/focus_capture.py) can be used to decide one
single question:

    is the built UE campus a MIRROR of the save's block grid, or a rotation of it?

Why a window grab and not the render target
-------------------------------------------
`out/ortho_topdown_raw.png` was produced by `SceneCapture2D` + `export_render_target`,
and it scores as `flip_v` against the save's own top-down truth map
(`out/truth_colour.png`). That leaves two explanations that produce identical
images:

  (a) the world geometry really is mirrored, or
  (b) `export_render_target` (or the PNG writer behind it) writes the render
      target bottom-up.

A window grab is insensitive to (b): it reads the pixels the compositor
presented, through `PrintWindow(PW_RENDERFULLCONTENT)`, with no render target,
no export helper and no image writer in the path. If the window grab also comes
out mirrored, it is the world; if it comes out as a pure rotation, the earlier
flip was the export path.

Why PIE and not `-game`
-----------------------
`-game` cannot be scripted: the Python plugin is an editor module. In `-game`
the only camera control is `-MCstop=5`, which teleports the *pawn* to Z = 64995
and then lets gravity take it -- the pose at grab time is bounded, not known. A
`CameraActor` that PIE duplicates from the editor world has an exact transform,
and PIE in its own window has no editor UI inside the client area, so the whole
window is the render.

Camera pose, and why three captures
-----------------------------------
`Rotator(pitch=-90, yaw=Y, roll=0)` is a nadir view. For a UE camera, screen
right is local +Y and screen down is local -Z, so

    yaw = -90 :  right = (+1,0,0) = +X     up = (0,+1,0) = +Y
    yaw = +90 :  right = (-1,0,0) = -X     up = (0,-1,0) = -Y

With the level's own mapping (`UE X = 100*block_x`, `UE Y = -100*block_z`,
measured from the .pos packs and the actor bounds), the save's truth map is
`col = +x`, `row = +z`, i.e. `col ~ +X`, `row ~ -Y`. Composing:

    yaw = -90 :  (u,v) = (+col, +row)   -> 'identity' if unmirrored, 'flip_v' if mirrored
    yaw = +90 :  (u,v) = (-col, -row)   -> 'rot180'   if unmirrored, 'flip_h' if mirrored

So one yaw already answers the question and the pair is a consistency check:
the winner may only change by 180 degrees between them. yaw = -90 is the
decisive pose because there "identity" means unmirrored with no mental
arithmetic about camera handedness.

Three poses, because the briefed 2 km altitude is the *thinnest* possible
evidence: at 2 km a 1920 px frame covers 4 km, so one block is 0.48 px and the
campus is a 215 x 369 px thumbnail. The third pose drops to 700 m -- the lowest
altitude at which the whole 448 x 768 campus still fits a 16:9 frame -- which is
2.8x more pixels per block. Same question, better contrast. All three are
reported; the answer has to agree across them.

Two things that made the 2 km frame a flat blue-grey rectangle on the first
attempt, both fixed at run time (nothing is saved):

  * exponential height fog. At 2 km every surface is 100% fogged. The level's
    own fog components are set to density 0 in the PIE world (`-MCfog=0` also
    covers the game mode's ApplyLook if it runs).
  * PIE defaults to playing *inside the editor viewport*, so a window grab
    returns the editor's toolbars. `ULevelEditorPlaySettings` is not exposed to
    Python (`unreal.LevelEditorPlaySettings` is missing in 5.8), so its class
    default object is loaded by path and `play_in_new_window` is set on it --
    the CDO is what the play code reads.

Driving, and why flags
----------------------
As in tools/ortho_topdown.py, the script must be started with
`-ExecCmds="py <script>"` (with `-ExecutePythonScript` the editor quits before
anything renders) and driven from `unreal.register_slate_post_tick_callback`.
The grab happens in a *different* process, so the two are synchronised with flag
files in out/:

    out/_chir_ready_<tag>.txt   written by this script when that pose settled
    out/_chir_cmd.txt           created by the driver: next tag, or "stop"

The pose actually used is written to out/chirality_pose_<tag>.json, so the
offline checker never has to guess where the camera was.

Run:

    UnrealEditor.exe project/MCReplica.uproject \
        -ExecCmds="py <repo>/tools/chirality_probe.py" -MCfog=0 \
        -nopause -nosplash -log -stdout

Knobs (environment): MC_CHIR_WIN "WxH" (default 1920x1080), MC_CHIR_FOV (90),
MC_CHIR_SETTLE (75 s for the first pose), MC_CHIR_SETTLE_N (15 s for the rest).
"""

import ctypes
import json
import os
import sys
import time
import traceback
from ctypes import wintypes

import unreal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(os.path.dirname(ROOT), "logs")
LOG = os.path.join(LOG_DIR, "chirality_probe.txt")
OUT = os.path.join(ROOT, "out")

MAP = "/Game/Maps/MCReplica"

CAM_X = float(os.environ.get("MC_CHIR_X", 8000.0))
CAM_Y = float(os.environ.get("MC_CHIR_Y", -16000.0))
FOV = float(os.environ.get("MC_CHIR_FOV", 90.0))
PITCH = -90.0
_win = os.environ.get("MC_CHIR_WIN", "1920x1080").lower().split("x")
WIN_W, WIN_H = int(_win[0]), int(_win[1])

SETTLE_FIRST = float(os.environ.get("MC_CHIR_SETTLE", 75.0))
SETTLE_NEXT = float(os.environ.get("MC_CHIR_SETTLE_N", 15.0))
HARD_LIMIT = 1200.0

#: tag -> (camera Z in cm, yaw). Tag "a" is the briefed pose; "c" is the
#: legible one; "b" is the control yaw at the briefed altitude.
POSES = [
    ("a", 200000.0, -90.0),
    ("b", 200000.0, 90.0),
    ("c", 70000.0, -90.0),
]

FLAG_CMD = os.path.join(OUT, "_chir_cmd.txt")
FLAG_STOP = os.path.join(OUT, "_chir_stop.txt")

_L = []


def say(msg):
    line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
    _L.append(line)
    unreal.log("[MCChir] " + str(msg))
    try:
        with open(LOG, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_L) + "\n")
    except Exception:
        pass


def ready_flag(tag):
    return os.path.join(OUT, "_chir_ready_%s.txt" % tag)


def pose_path(tag):
    return os.path.join(OUT, "chirality_pose_%s.json" % tag)


def windows():
    """[(hwnd, title, client_w, client_h)] for visible top-level windows.

    The PIE window's title is not documented anywhere in 5.8, so it is *read*
    rather than assumed -- otherwise the driver's `--match` picks the editor
    window and the grab is a picture of the editor.
    """
    user32 = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                r = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(r))
                out.append((int(hwnd), buf.value,
                            r.right - r.left, r.bottom - r.top))
        return True

    try:
        user32.EnumWindows(cb, 0)
    except Exception as exc:                     # noqa: BLE001
        say("window enumeration failed: %s" % exc)
    return out


class Job(object):
    def __init__(self):
        self.ues = None
        self.les = None
        self.cam = None
        self.cam_g = None
        self.pc = None
        self.game_world = None
        self.handle = None
        self.state = "start"
        self.t0 = time.time()
        self.t_start = time.time()
        self.done = False
        self.idx = 0
        self.notes = []

    def note(self, msg):
        self.notes.append(msg)
        say(msg)

    def tick(self, _dt=0.0):
        if self.done:
            return
        try:
            now = time.time()
            if now - self.t_start > HARD_LIMIT:
                self.finish(False, "hard time limit (%.0fs) in state %s"
                            % (now - self.t_start, self.state))
                return
            getattr(self, "s_" + self.state)(now)
        except Exception:
            say(traceback.format_exc())
            self.finish(False, "uncaught exception in state %s" % self.state)

    def go(self, state):
        self.state = state
        self.t0 = time.time()
        say("-> state %s" % state)

    def finish(self, ok, msg):
        if self.done:
            return
        self.done = True
        try:
            if self.les is not None:
                self.les.editor_request_end_play()
                say("PIE end requested")
        except Exception as exc:                 # noqa: BLE001
            say("end_play failed: %s" % str(exc).splitlines()[0][:80])
        try:
            if self.cam is not None:
                unreal.get_editor_subsystem(
                    unreal.EditorActorSubsystem).destroy_actor(self.cam)
        except Exception:                        # noqa: BLE001
            pass
        say("")
        say("RESULT: %s %s" % ("PASS" if ok else "FAIL", msg))
        if self.handle is not None:
            try:
                unreal.unregister_slate_post_tick_callback(self.handle)
            except Exception:                    # noqa: BLE001
                pass
            self.handle = None
        try:
            quit_fn = getattr(unreal.SystemLibrary, "quit_editor", None)
            if callable(quit_fn):
                quit_fn()
            else:
                unreal.SystemLibrary.execute_console_command(None, "QUIT_EDITOR")
        except Exception:                        # noqa: BLE001
            pass

    # -- setup ----------------------------------------------------------- #
    def s_start(self, now):
        self.go("load")

    def play_settings(self):
        """The ULevelEditorPlaySettings CDO, loaded by path (not in `unreal`)."""
        for path in ("/Script/UnrealEd.Default__LevelEditorPlaySettings",
                     "/Script/UnrealEd.LevelEditorPlaySettings"):
            try:
                obj = unreal.load_object(None, path)
                if obj is not None:
                    say("play settings: %s -> %r" % (path, obj))
                    return obj
            except Exception as exc:             # noqa: BLE001
                say("load_object(%s) failed: %s" % (path, str(exc).splitlines()[0][:70]))
        return None

    def s_load(self, now):
        self.ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = self.ues.get_editor_world() if self.ues else None
        if world is None:
            self.finish(False, "no editor world")
            return
        if world.get_name() != "MCReplica":
            self.les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
            ok = bool(self.les.load_level(MAP)) if self.les is not None else False
            if not ok:
                ok = unreal.EditorLoadingAndSavingUtils.load_map(MAP) is not None
            self.note("load(%s) -> %s" % (MAP, ok))
            if not ok:
                self.finish(False, "level did not load")
                return
            world = self.ues.get_editor_world()
        self.note("editor world: %s" % world.get_name())

        tag, z, yaw = POSES[0]
        loc = unreal.Vector(CAM_X, CAM_Y, z)
        rot = unreal.Rotator(PITCH, yaw, 0.0)
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        cam = eas.spawn_actor_from_class(unreal.CameraActor, loc, rot)
        if cam is None:
            self.finish(False, "could not spawn unreal.CameraActor")
            return
        self.cam = cam
        try:
            cam.set_actor_label("MC_ChiralityCam")
        except Exception:                        # noqa: BLE001
            pass
        comp = getattr(cam, "camera_component", None)
        if comp is not None:
            for k, v in (("field_of_view", FOV), ("constrain_aspect_ratio", False)):
                try:
                    comp.set_editor_property(k, v)
                    self.note("camera_component.%s = %r" % (k, v))
                except Exception as exc:         # noqa: BLE001
                    self.note("camera_component.%s FAILED: %s"
                              % (k, str(exc).splitlines()[0][:70]))
        self.log_pose("editor-world camera")

        ps = self.play_settings()
        if ps is not None:
            for k, v in (("play_in_new_window", True),
                         ("new_window_width", WIN_W),
                         ("new_window_height", WIN_H),
                         ("center_new_window", True),
                         ("game_gets_mouse_control", False),
                         ("play_number_of_clients", 1)):
                try:
                    ps.set_editor_property(k, v)
                    self.note("play_settings.%s = %r" % (k, v))
                except Exception as exc:         # noqa: BLE001
                    self.note("play_settings.%s FAILED: %s"
                              % (k, str(exc).splitlines()[0][:70]))
            for k in ("play_in_new_window", "new_window_width", "new_window_height"):
                try:
                    self.note("play_settings.%s reads %r"
                              % (k, ps.get_editor_property(k)))
                except Exception:                # noqa: BLE001
                    pass
        else:
            self.note("WARNING: no play settings object; PIE will run in the "
                      "editor viewport and the window grab will contain the editor UI")
        self.go("pie")

    def log_pose(self, label):
        act = self.cam_g or self.cam
        l, r = act.get_actor_location(), act.get_actor_rotation()
        try:
            fwd = act.get_actor_forward_vector()
            up = act.get_actor_up_vector()
            vec = "fwd=(%.3f, %.3f, %.3f) up=(%.3f, %.3f, %.3f)" % (
                fwd.x, fwd.y, fwd.z, up.x, up.y, up.z)
        except Exception:                        # noqa: BLE001
            vec = "fwd/up unavailable"
        self.note("%s: loc=(%.1f, %.1f, %.1f) rot=(p %.2f, y %.2f, r %.2f) %s"
                  % (label, l.x, l.y, l.z, r.pitch, r.yaw, r.roll, vec))

    # -- PIE ------------------------------------------------------------- #
    def s_pie(self, now):
        self.les = self.les or unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        try:
            self.les.editor_request_begin_play()
        except Exception as exc:                 # noqa: BLE001
            self.note("editor_request_begin_play failed: %s"
                      % str(exc).splitlines()[0][:90])
        self.note("PIE begin requested")
        self.go("wait_world")

    def s_wait_world(self, now):
        gw = None
        try:
            gw = self.ues.get_game_world()
        except Exception:                        # noqa: BLE001
            pass
        if gw is None:
            if now - self.t0 > 180.0:
                self.finish(False, "PIE world never appeared")
            return
        self.game_world = gw
        self.note("PIE world: %s" % gw.get_name())
        for hwnd, title, w, h in windows():
            if w > 300 and h > 200:
                self.note("  window %-9d %5dx%-5d %r" % (hwnd, w, h, title))

        cams = list(unreal.GameplayStatics.get_all_actors_of_class(
            gw, unreal.CameraActor))
        self.note("CameraActor count in PIE world: %d" % len(cams))
        pick = None
        for c in cams:
            try:
                lab = str(c.get_actor_label())
            except Exception:                    # noqa: BLE001
                lab = "?"
            l, r = c.get_actor_location(), c.get_actor_rotation()
            self.note("  cam %-22r loc=(%.0f, %.0f, %.0f) rot=(p %.1f, y %.1f)"
                      % (lab, l.x, l.y, l.z, r.pitch, r.yaw))
            if "Chirality" in lab or pick is None:
                pick = c
        if pick is None:
            self.finish(False, "the PIE world has no CameraActor -- the editor "
                               "actor was not duplicated, the pose is not known")
            return
        self.cam_g = pick
        self.pc = unreal.GameplayStatics.get_player_controller(gw, 0)
        if self.pc is None:
            self.finish(False, "no player controller in the PIE world")
            return
        self.note("view target before: %r" % self.pc.get_view_target())
        self.pc.set_view_target_with_blend(self.cam_g, 0.0)
        self.note("view target after: %r" % self.pc.get_view_target())
        for call in ("set_ignore_look_input", "set_ignore_move_input"):
            try:
                getattr(self.pc, call)(True)
            except Exception:                    # noqa: BLE001
                pass
        self.kill_fog()
        self.log_pose("PIE camera (pose a)")
        self.go("settle_a")

    def kill_fog(self):
        """Density 0 on every exponential height fog actor -- runtime only.

        At 2 km the campus is 100% fogged otherwise, and a uniform fog-coloured
        frame is exactly what the first attempt produced.
        """
        n = 0
        for cls_name in ("ExponentialHeightFog",):
            cls = getattr(unreal, cls_name, None)
            if cls is None:
                continue
            for a in unreal.GameplayStatics.get_all_actors_of_class(self.game_world, cls):
                comp = a.get_component_by_class(unreal.ExponentialHeightFogComponent)
                if comp is None:
                    continue
                try:
                    comp.set_editor_property("fog_density", 0.0)
                    n += 1
                except Exception as exc:         # noqa: BLE001
                    self.note("fog density set failed: %s" % str(exc).splitlines()[0][:70])
        self.note("height fog actors set to density 0: %d" % n)
        for cmd in ("r.VolumetricFog 0", "r.VolumetricFogGridPixelSize 8"):
            try:
                unreal.SystemLibrary.execute_console_command(self.game_world, cmd)
            except Exception:                    # noqa: BLE001
                pass

    # -- the settle / capture handshake ---------------------------------- #
    def settle(self, seconds, now, on_done):
        if now - self.t0 < seconds:
            return
        on_done()

    def s_settle_a(self, now):
        def done():
            self.write_pose(POSES[0])
            self.go("wait_cmd")
        self.settle(SETTLE_FIRST, now, done)

    def s_wait_cmd(self, now):
        if os.path.isfile(FLAG_STOP):
            self.finish(True, "stop requested; captures should be on disk")
            return
        if not os.path.isfile(FLAG_CMD):
            if now - self.t0 > 420.0:
                self.finish(False, "no command within 420s of the ready flag")
            return
        try:
            with open(FLAG_CMD, encoding="utf-8") as fh:
                cmd = fh.read().strip().lower()
        except OSError:
            return
        try:
            os.remove(FLAG_CMD)
        except OSError:
            pass
        if cmd in ("stop", "quit", "end"):
            self.finish(True, "stop command")
            return
        want = [p for p in POSES if p[0] == cmd]
        if not want:
            self.note("unknown command %r -- ignoring" % cmd)
            return
        tag, z, yaw = want[0]
        self.cam_g.set_actor_location(unreal.Vector(CAM_X, CAM_Y, z), False, False)
        self.cam_g.set_actor_rotation(unreal.Rotator(PITCH, yaw, 0.0), False)
        self.note("applied pose %s" % tag)
        self.log_pose("PIE camera (pose %s)" % tag)
        self.next_tag = tag
        self.go("settle_next")

    def s_settle_next(self, now):
        def done():
            want = [p for p in POSES if p[0] == self.next_tag][0]
            self.write_pose(want)
            self.go("wait_cmd")
        self.settle(SETTLE_NEXT, now, done)

    def write_pose(self, pose):
        tag, z, yaw = pose
        act = self.cam_g
        l, r = act.get_actor_location(), act.get_actor_rotation()
        comp = getattr(act, "camera_component", None)
        fov = None
        try:
            fov = float(comp.get_editor_property("field_of_view"))
        except Exception:                        # noqa: BLE001
            pass
        fwd = act.get_actor_forward_vector()
        up = act.get_actor_up_vector()
        payload = {
            "tag": tag,
            "camera": {"x": l.x, "y": l.y, "z": l.z,
                       "pitch": r.pitch, "yaw": r.yaw, "roll": r.roll,
                       "forward": [fwd.x, fwd.y, fwd.z],
                       "up": [up.x, up.y, up.z],
                       "fov_expected": FOV, "fov_read": fov},
            "window": {"width": WIN_W, "height": WIN_H},
            "fog_disabled": True,
            "mapping": {
                "world": "UE X = 100*block_x ; UE Y = -100*block_z ; Z up",
                "truth": "out/truth_colour.png col=+block_x row=+block_z",
                "yaw_-90_unmirrored": "identity",
                "yaw_-90_mirrored": "flip_v",
                "yaw_+90_unmirrored": "rot180",
                "yaw_+90_mirrored": "flip_h",
            },
            "notes": self.notes,
            "stamp": time.strftime("%F %T"),
        }
        try:
            with open(pose_path(tag), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=1)
            self.note("wrote %s" % pose_path(tag))
        except Exception as exc:                 # noqa: BLE001
            self.note("pose json failed: %s" % exc)
        self.note("windows now:")
        for hwnd, title, w, h in windows():
            if w > 300 and h > 200:
                self.note("  window %-9d %5dx%-5d %r" % (hwnd, w, h, title))
        open(ready_flag(tag), "w", encoding="utf-8").write(tag)


def main():
    say("=== chirality_probe start %s ===" % time.strftime("%F %T"))
    say("engine %s" % unreal.SystemLibrary.get_engine_version())
    say("camera x=%.0f y=%.0f  pitch %.0f  fov %.0f  window %dx%d"
        % (CAM_X, CAM_Y, PITCH, FOV, WIN_W, WIN_H))
    say("poses: %s" % (POSES,))
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    for tag, _z, _y in POSES:
        for p in (ready_flag(tag),):
            try:
                os.remove(p)
            except OSError:
                pass
    for p in (FLAG_CMD, FLAG_STOP):
        try:
            os.remove(p)
        except OSError:
            pass
    if not callable(getattr(unreal, "register_slate_post_tick_callback", None)):
        say("RESULT: FAIL no slate tick callback -- cannot drive PIE")
        return 1
    job = Job()
    job.handle = unreal.register_slate_post_tick_callback(job.tick)
    say("state machine registered on the slate tick")
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
