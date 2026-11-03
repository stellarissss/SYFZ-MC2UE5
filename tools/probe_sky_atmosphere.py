# -*- coding: utf-8 -*-
"""
probe_sky_atmosphere.py -- report every knob on the level's SkyAtmosphere that
could brighten the rendered sky, with its current value.

Why this exists
---------------
`tools/qa_persp_gates.py` measures the ground-level frame's sky segment at sRGB
luma 82.8 against a 130..210 daylight gate, i.e. the rendered sky is roughly a
stop too dark -- and darker than the sunlit grass in the same frame (143), which
no real midday sky is. Exposure cannot fix it: the level's exposure is pinned at
the value that makes sunlit tops match out/truth_colour.png, and moving it would
break that calibration. So the sky's own radiance has to move, and the SkyAtmo-
sphere component is where it lives.

Which property that is cannot be guessed: 5.8 exposes scattering scales, a
multi-scattering factor and possibly a luminance factor under names that have
moved between engine versions. This dumps what is actually there, with values,
so the fix can name one property instead of trying five.

Read-only. Nothing is written, nothing is saved.

Run (editor-side, `-nullrhi` is fine, nothing is drawn):

    UnrealEditor-Cmd.exe <abs>/project/MCReplica.uproject \
        -ExecutePythonScript=<abs>/tools/probe_sky_atmosphere.py \
        -nullrhi -unattended -nopause -nosplash -stdout
"""
import os
import sys
import traceback

import unreal

LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "logs")
OUT = os.path.join(LOG_DIR, "probe_sky_atmosphere.txt")
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSKY] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


INTERESTING = ("lumin", "bright", "scale", "factor", "intensity", "multipl",
               "rayleigh", "mie", "absorption", "sun_disk", "sky")


def dump_props(obj, tag):
    say("--- %s (%s) ---" % (tag, obj.get_class().get_name()))
    rows = []
    for name in dir(obj):
        if name.startswith("_"):
            continue
        low = name.lower()
        if not any(k in low for k in INTERESTING):
            continue
        try:
            val = obj.get_editor_property(name)
        except Exception:                              # noqa: BLE001
            continue
        if callable(val):
            continue
        try:
            txt = repr(val)
        except Exception:                              # noqa: BLE001
            txt = "<unrepr>"
        rows.append((name, txt[:70]))
    for name, txt in sorted(rows):
        say("  %-44s %s" % (name, txt))
    say("  (%d properties matched)" % len(rows))


def main():
    say("=== probe_sky_atmosphere %s ===" % unreal.SystemLibrary
        .get_engine_version())
    ok = False
    try:
        ok = bool(unreal.EditorLoadingAndSavingUtils.load_map(MAP))
    except Exception:                                  # noqa: BLE001
        ok = False
    say("load(%s) -> %s" % (MAP, ok))
    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = ues.get_editor_world() if ues else None
    if world is None:
        say("RESULT: FAIL no editor world")
        return 1

    atmos = list(unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.SkyAtmosphere))
    say("SkyAtmosphere actors: %d" % len(atmos))
    for a in atmos:
        say("  actor %s" % a.get_actor_label())
        comp = a.get_component_by_class(unreal.SkyAtmosphereComponent)
        if comp is None:
            say("  no SkyAtmosphereComponent")
            continue
        dump_props(comp, "SkyAtmosphereComponent")

    skies = list(unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.SkyLight))
    for a in skies:
        say("")
        say("SkyLight %s" % a.get_actor_label())
        comp = a.get_component_by_class(unreal.SkyLightComponent)
        if comp is not None:
            for name in ("intensity", "light_color", "source_type",
                         "real_time_capture", "lower_hemisphere_is_black",
                         "lower_hemisphere_color", "affect_global_illumination",
                         "mobility"):
                say("  %-40s %s" % (name, repr(_read(comp, name))[:60]))

    say("")
    say("--- relevant cvars ---")
    for cvar in ("r.SkyAtmosphere", "r.SkyAtmosphere.SampleCountScale",
                 "r.SkyAtmosphere.MultiScatteringLUT", "r.SkyLuminanceFactor",
                 "r.Lumen.SkylightLeaking", "r.SupportSkyAtmosphere",
                 "r.SupportSkyAtmosphereAffectsHeightFog",
                 "r.DefaultFeature.AmbientOcclusion"):
        try:
            v = unreal.SystemLibrary.get_console_variable_int_value(cvar)
        except Exception:                              # noqa: BLE001
            v = "<unreadable>"
        say("  %-44s %s" % (cvar, v))

    say("")
    say("RESULT: PASS reported %d SkyAtmosphere actor(s)" % len(atmos))
    return 0


def _read(obj, *names):
    for n in names:
        try:
            return obj.get_editor_property(n)
        except Exception:                              # noqa: BLE001
            continue
    return "<missing>"


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        say(traceback.format_exc())
        say("RESULT: FAIL uncaught exception")
        raise
