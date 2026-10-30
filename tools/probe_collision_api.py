# -*- coding: utf-8 -*-
"""probe_collision_api.py -- find the UE 5.8 Python API for complex-as-simple
collision, and diagnose why build_terrain_meshes.py cannot write its log.

UE 5.8 removed/renamed what the script used: unreal.CollisionComplexity does
not exist and StaticMeshEditorSubsystem has no set_collision_complexity (both
verified by the 10-30 build run failing on all 24 tiles). This probe prints
what IS available instead of guessing, then tests the log write path.
"""

import os

import unreal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(os.path.dirname(REPO), "logs",
                           "probe_collision_api.txt")
OUT = os.environ.get("MC_LOG_DIR", os.path.dirname(OUT_DEFAULT))
OUT = os.path.join(OUT, "probe_collision_api.txt")
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCCOLLPROBE] " + str(s))


try:
    say("MC_LOG_DIR env = %r" % os.environ.get("MC_LOG_DIR"))
    say("OUT path = %r" % OUT)
    say("OUT dir exists = %s" % os.path.isdir(os.path.dirname(OUT)))
    try:
        with open(OUT, "w") as fh:
            fh.write("probe write test\n")
        say("log write: OK (%d bytes)" % os.path.getsize(OUT))
    except Exception as exc:
        say("log write FAILED: %r" % exc)

    say("--- unreal names containing Collision ---")
    names = [n for n in dir(unreal) if "ollision" in n]
    say("%s" % names)

    say("--- StaticMeshEditorSubsystem collision attrs ---")
    sub = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    attrs = [a for a in dir(sub) if "collision" in a.lower()]
    say("%s" % attrs)

    say("--- StaticMesh class collision attrs ---")
    sm_attrs = [a for a in dir(unreal.StaticMesh) if "collision" in a.lower()]
    say("%s" % sm_attrs)

    asset = unreal.load_asset("/Game/MC/Terrain/terrain_-016_-032")
    if asset is None:
        say("asset load FAILED")
    else:
        say("asset: %s  class=%s" % (asset.get_name(),
                                     asset.get_class().get_name()))
        for prop in ("collision_complexity", "body_setup", "allow_cpu_access",
                     "collision_trace_flag"):
            try:
                v = asset.get_editor_property(prop)
                say("  get %r -> %r" % (prop, v))
            except Exception as exc:
                say("  get %r -> EXC %r" % (prop, str(exc)[:120]))
        bs = None
        try:
            bs = asset.get_editor_property("body_setup")
        except Exception:
            pass
        if bs is not None:
            say("  body_setup class = %s" % bs.get_class().get_name())
            bs_attrs = [a for a in dir(bs) if "trace" in a.lower()
                        or "collision" in a.lower()]
            say("  body_setup attrs: %s" % bs_attrs)
            try:
                say("  collision_trace_flag = %r"
                    % bs.get_editor_property("collision_trace_flag"))
            except Exception as exc:
                say("  collision_trace_flag EXC %r" % str(exc)[:120])

    say("RESULT: PASS (probe completed)")
except Exception:
    import traceback
    say("probe crashed:\n" + traceback.format_exc())
    say("RESULT: FAIL probe crashed")

unreal.log("[MCCOLLPROBE] done")
