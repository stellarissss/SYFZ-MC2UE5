# -*- coding: utf-8 -*-
"""
fix_lighting_mc.py -- put the level itself (not the capture) into the
Minecraft-style lighting state, and calibrate it against out/truth_colour.png.

What the target actually is
---------------------------
The look route is **写实日光** (realistic daylight), decided by the user and
recorded in the project's decision log. It is explicitly NOT the vanilla-flat
route: vanilla Minecraft applies no cast shadows and no ambient occlusion, so a
vanilla frame reads as an evenly lit drawing of the albedo. Daylight asks for
the opposite -- one dominant sun with cast shadows, a real sky that is brighter
than it is blue, and enough sky fill that a face turned away from the sun is
still readable.

The acceptance bar is "MC 地图的标准贴图质量，远看近看都仿真". Split into what
this script is responsible for and what it is not:

  * texture fidelity -- sharp 16x16 texels, the right tile on the right block --
    is the material and UV chain's job, and is not measured here;
  * the LOOK -- sun/sky ratio, exposure, no post effects that smear texels --
    is this script's job.

The unavoidable cost of the route: a sunlit surface is `albedo * (sun*cos +
sky)` pushed through a tone curve, so the rendered pixel is NOT the texture
colour, and the gap drifts with the lighting parameters. "接近" is the contract,
"等于" was the other route's contract. So the ruler `out/truth_colour.png`
(every pixel = the arithmetic mean sRGB of the vanilla 16x16 tile that column's
topmost solid block shows from above) is used as a **tolerance band around the
mean colour**, not as a per-pixel equality test. Three properties are gated:

    sky       sRGB luminance 130..210, and B > G > R (a real daytime sky);
    ground    each channel within ~12/255 of the truth map's mean;
    faces     no runaway blue: |B/R - 1| <= 0.25 on vertical faces.

The failure this replaces was measured on `out/persp_spawn.png`: sky near black
and every vertical wall a saturated blue -- a blue floodlight at night, not
daylight. Its cause was structural, not a matter of tuning: SkyLight was
`SLS_CAPTURED_SCENE` + `real_time_capture` at intensity 5 while the sun sat at
3 lux, so the sky light was the only real source, its cubemap content came from
an atmosphere driven by that 3-lux sun, and the atmosphere was therefore black.
Raising the sky light can only make walls bluer. The fix is to make the SUN the
source and the sky its consequence.

Why the exposure has to move into the level
-------------------------------------------
`MC_PostProcess` in the level has every `override_*` flag **False** (measured,
200+ flags), so the volume is inert and the authored values in it
(auto_exposure_min/max_brightness 0.4/1.8, bias 0.4, bloom 0.6, vignette 0.25)
never apply. The only exposure actually in force during a capture is the
SceneCapture's own PostProcessSettings, written by tools/ortho_topdown.py:830-840
with min_brightness == max_brightness == 1.0 -> a FIXED exposure of roughly
EV100 6.4, i.e. calibrated for a ~1700 lux scene. The level runs a 10 lux
("twilight") sun. That mismatch is why only the sun-facing side survives:
sunlit tops sit at 235-255 and everything else falls into the toe.

A look that only exists inside a capture cannot ship, so this script enables the
exposure override ON THE VOLUME and pins it there. Bounded auto was tried and
measured, and it lost: with the window 2500..9000 the top-down frame came out
+1.37 / +1.41 / +1.86 stops above out/truth_colour.png (over-bright and too
blue), while a fixed 7170 lands at -0.16 / +0.16 / +0.20 stops. The window
also did not bound the result the way the docs suggest -- the adaptation chose a
value brighter than the max -- so the feature is kept only as an option
(`MC_EXPOSURE_MIN`/`MC_EXPOSURE_MAX`) and the shipped default is one fixed
number that the ruler can be compared against. The game and any capture then
agree -- provided the capture stops overriding it (see the warning this script
prints; tools/ortho_topdown.py leaves it alone unless MC_ORTHO_PP_EXPOSURE=1).

Everything here is idempotent: every write is an absolute value, so re-running
on an already-fixed level reports the same before/after and the same RESULT.
Nothing is deleted, no asset is touched, nothing is rendered.

Run (editor-side; `-nullrhi` is fine, nothing is drawn):

    UnrealEditor-Cmd.exe project/MCReplica.uproject \
        -ExecutePythonScript=<repo>/tools/fix_lighting_mc.py \
        -nullrhi -unattended -nopause -nosplash -stdout

Knobs (all optional; every value is printed before and after):
    MC_LOG_DIR              directory for fix_lighting_mc.txt (joined, not used raw)
    MC_SUN_LUX              directional light intensity            (default 60000)
    MC_SUN_PITCH            sun pitch, vertical-face modelling     (default -50)
    MC_SUN_TEMP             blackbody temperature                  (default 6500)
    MC_SUN_COLOR            "r,g,b" tint                           (default 1,1,1)
    MC_SUN_SHADOWS          1 to let the sun cast shadows          (default 1)
    MC_BOUNCE_LUX           warm fill, facing the unlit facades    (default 15000)
    MC_BOUNCE_SIDE_LUX      the two side fills of the ring         (default 9000)
    MC_BOUNCE_TEMP          their blackbody temperature            (default 5200)
    MC_BOUNCE_PITCH         near-flat on purpose; see fix_fill_lights (default -12)
    MC_SKY_INTENSITY        strength of the sky FILL (not the dome) (default 1.0)
    MC_SKY_LUMINANCE        x on the rendered dome's radiance       (default 2.7)
    MC_SKY_COLOR            "r,g,b" white balance on the fill      (default 1,0.97,0.90)
    MC_LOWER_HEMI_COLOR     "r,g,b" lower hemisphere               (default 0.30,0.33,0.38)
    MC_EXPOSURE_BRIGHTNESS  fixed exposure, both min and max       (default 7.0)
    MC_EXPOSURE_MIN/MAX     window for bounded auto; unset = fixed  (default unset)
    MC_FOG_DENSITY          per cm                                 (default 0.00006)
    MC_FOG_INSCATTER        "r,g,b" colour of the haze             (default 0.58,0.60,0.63)
    MC_FOG_MAX_OPACITY      cap on the far-field fog sheet         (unset = leave alone)
    MC_SAVE_LEVEL           1 to save the level                    (default 0 = do not)
"""

import os
import sys
import time
import traceback

import unreal

LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "logs")
OUT = os.path.join(LOG_DIR, "fix_lighting_mc.txt")
MAP = "/Game/Maps/MCReplica"

SUN_LABEL = "MC_Sun"
SKY_LABEL = "MC_SkyLight"
BOUNCE_LABEL = "MC_BounceLight"
FILL_LEFT_LABEL = "MC_FillLeft"
FILL_RIGHT_LABEL = "MC_FillRight"
FOG_LABEL = "MC_Fog"
PPV_LABEL = "MC_PostProcess"

#: The ruler's grass reference: the alpha-weighted mean sRGB of the vanilla
#: `grass_block_top` tile, which is what `out/truth_colour.png` stores for every
#: grass column.
TRUTH_GRASS = (84, 109, 51)

_L = []
_FAILED = []


def say(msg):
    _L.append(str(msg))
    unreal.log("[MCFixLight] " + str(msg))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_L) + "\n")
    except OSError:
        pass


def env_float(name, default):
    v = os.environ.get(name, "").strip()
    if not v:
        return float(default)
    try:
        return float(v)
    except ValueError:
        say("warning: %s=%r is not a number, using %r" % (name, v, default))
        return float(default)


def env_color(name, default):
    v = os.environ.get(name, "").strip()
    if not v:
        return unreal.LinearColor(*default)
    try:
        r, g, b = [float(x) for x in v.split(",")]
        return unreal.LinearColor(r, g, b)
    except Exception:                                  # noqa: BLE001
        say("warning: %s=%r is not 'r,g,b', using %r" % (name, v, default))
        return unreal.LinearColor(*default)


def read(obj, *names):
    """First name that reads; "<unreadable>" means no name worked."""
    if obj is None:
        return "<no object>"
    for n in names:
        try:
            return obj.get_editor_property(n)
        except Exception:                              # noqa: BLE001
            pass
        try:
            return getattr(obj, n)
        except Exception:                              # noqa: BLE001
            pass
    return "<unreadable>"


def fmt(v):
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


def set_prop(obj, label, value, *names):
    """set_editor_property with a candidate list; logs which spelling landed."""
    for n in names:
        try:
            obj.set_editor_property(n, value)
            say("  set %-34s = %-26s (via %s)" % (label, fmt(value), n))
            return n
        except Exception as exc:                       # noqa: BLE001
            last = str(exc).splitlines()[0][:60]
    _FAILED.append(label)
    say("  FAIL %-33s could not be set to %s via any of %s -- last error: %s"
        % (label, fmt(value), ", ".join(names), last))
    return None


def assert_close(label, got, want, tol=1e-3):
    """Read-back check. A value that did not land fails the step."""
    ok = False
    if isinstance(want, bool):
        ok = (bool(got) == want)
    elif isinstance(got, (int, float)) and isinstance(want, (int, float)):
        ok = abs(float(got) - float(want)) <= max(tol, abs(float(want)) * 1e-4)
    else:
        ok = (str(got) == str(want))
    say("  %-4s %-34s reads %-26s (wanted %s)"
        % ("OK" if ok else "FAIL", label, fmt(got), fmt(want)))
    if not ok:
        _FAILED.append(label + " readback")
    return ok


def find_existing(world, cls, label):
    """Same lookup ensure_lighting.py uses (label first, then any instance)."""
    hits = list(unreal.GameplayStatics.get_all_actors_of_class(world, cls))
    for a in hits:
        if a.get_actor_label() == label:
            return a
    return hits[0] if hits else None


# --------------------------------------------------------------------------- #
# 1. the sun: white 6500 K, low intensity, movable, oblique enough to model faces
# --------------------------------------------------------------------------- #

def set_pitch(actor, pitch, keep):
    """Write the actor's pitch, whatever the binding's Rotator field order is.

    `unreal.Rotator(a, b, c)` does NOT reliably mean (pitch, yaw, roll): a first
    attempt at (-55, 180, 132) read back as pitch 0, i.e. the tuple landed in a
    different field than expected. Rather than guess the binding, write each
    spelling and keep the one that READS BACK as the requested pitch -- the same
    "report which name landed" rule the rest of this project uses.
    """
    attempts = []
    try:
        attempts.append(("keyword", unreal.Rotator(pitch=pitch, yaw=keep.yaw,
                                                  roll=keep.roll)))
    except Exception:                                  # noqa: BLE001
        pass
    attempts.append(("pitch,yaw,roll", unreal.Rotator(pitch, keep.yaw, keep.roll)))
    attempts.append(("roll,pitch,yaw", unreal.Rotator(keep.roll, pitch, keep.yaw)))
    for label, rot in attempts:
        try:
            actor.set_actor_rotation(rot, False)
        except Exception as exc:                       # noqa: BLE001
            say("  pitch via %-14s raised %s"
                % (label, str(exc).splitlines()[0][:50]))
            continue
        got = actor.get_actor_rotation()
        say("  pitch via %-14s -> reads (p %.1f, y %.1f, r %.1f)"
            % (label, got.pitch, got.yaw, got.roll))
        if abs(got.pitch - pitch) <= 0.01:
            return float(got.pitch)
    _FAILED.append("pitch")
    say("  FAIL pitch could not be written: no spelling read back as %.1f" % pitch)
    return None


def fix_sun(world):
    say("")
    say("--- DirectionalLight -------------------------------------------------")
    sun = find_existing(world, unreal.DirectionalLight, SUN_LABEL)
    if sun is None:
        _FAILED.append("no DirectionalLight")
        say("  FAIL no DirectionalLight in the level -- nothing will be lit")
        return None
    comp = sun.get_component_by_class(unreal.DirectionalLightComponent)
    if comp is None:
        _FAILED.append("sun has no component")
        say("  FAIL %s has no DirectionalLightComponent" % SUN_LABEL)
        return None

    lux = env_float("MC_SUN_LUX", 60000.0)
    pitch = env_float("MC_SUN_PITCH", -50.0)
    temp = env_float("MC_SUN_TEMP", 6500.0)
    colour = env_color("MC_SUN_COLOR", (1.0, 1.0, 1.0))
    want_shadows = os.environ.get("MC_SUN_SHADOWS", "1").strip() not in ("", "0")

    rot = sun.get_actor_rotation()
    say("  actor %s" % sun.get_actor_label())
    say("  before: intensity=%s mobility=%s cast_shadows=%s use_temperature=%s "
        "temperature=%s light_color=%s rot=(p %.1f, y %.1f, r %.1f)"
        % (fmt(read(comp, "intensity")), fmt(read(comp, "mobility")),
           fmt(read(comp, "cast_shadows")), fmt(read(comp, "use_temperature")),
           fmt(read(comp, "temperature")), fmt(read(comp, "light_color")),
           rot.pitch, rot.yaw, rot.roll))

    # Shadows are ON for the daylight route, and the reason has to be written
    # down because the opposite was asserted here before: "vanilla casts no
    # shadow" is true, and it is a property of the route the user rejected. A
    # sun that cannot cast a shadow is not daylight -- it is a directional tint,
    # and it leaves every surface it does not face reading as ambient only.
    if read(comp, "cast_shadows") is not want_shadows:
        set_prop(comp, "cast_shadows", want_shadows, "cast_shadows",
                 "bCastShadows")
    else:
        say("  cast_shadows already %s -- left alone" % want_shadows)

    # Intensity: a modelling term, not the light source.
    try:
        comp.set_intensity(lux)                        # UFUNCTION: dirties state
        say("  set %-34s = %-26s (via set_intensity)" % ("intensity", fmt(lux)))
    except Exception:                                  # noqa: BLE001
        set_prop(comp, "intensity", lux, "intensity")

    # White 6500 K. The measured reason: the old 5600 K + (255,250,243) pair
    # removed 28% of blue from the shaded albedo (lit/albedo B/R 0.658 vs 0.918
    # for a white sun), and the grass column is 28/51 short in blue.
    set_prop(comp, "use_temperature", True, "use_temperature",
             "temperature_enabled", "bUseTemperature")
    set_prop(comp, "temperature", temp, "temperature")
    try:
        comp.set_light_color(colour)
        say("  set %-34s = (%.3f, %.3f, %.3f) (via set_light_color)"
            % ("light_color", colour.r, colour.g, colour.b))
    except Exception as exc:                           # noqa: BLE001
        say("  note: set_light_color not callable (%s); trying the property"
            % str(exc).splitlines()[0][:50])
        set_prop(comp, "light_color", colour, "light_color")

    # Movable: a Stationary/Static light would need a lighting build, and this
    # level has none (mesh lightmap resolution 4).
    set_prop(comp, "mobility", unreal.ComponentMobility.MOVABLE, "mobility")
    # Pitch: a grazing sun collapses vertical faces to zero; -55 gives E/W
    # faces ~0.57 and N/S ~0.80 of the top's irradiance -- the vanilla 0.6/0.8.
    set_pitch(sun, pitch, rot)
    try:
        comp.mark_render_state_dirty()
    except Exception:                                  # noqa: BLE001
        pass

    say("  -- read back --")
    assert_close("cast_shadows", bool(read(comp, "cast_shadows")), want_shadows)
    assert_close("intensity", float(read(comp, "intensity")), lux)
    assert_close("use_temperature", bool(read(comp, "use_temperature")), True)
    assert_close("temperature", float(read(comp, "temperature")), temp)
    assert_close("mobility", str(read(comp, "mobility")),
                 str(unreal.ComponentMobility.MOVABLE))
    assert_close("pitch", round(sun.get_actor_rotation().pitch, 3), pitch)
    # `atmosphere_sun_light` is what makes this sun the one the SkyAtmosphere
    # scatters, and the sky's radiance then scales with `lux`. With it off (or
    # with a twilight lux) the atmosphere renders a near-black dome whatever the
    # sky light does -- that was the "night-time blue floodlight" frame.
    if read(comp, "atmosphere_sun_light") is not True:
        set_prop(comp, "atmosphere_sun_light", True, "atmosphere_sun_light",
                 "bAtmosphereSunLight")
    say("  atmosphere_sun_light=%s at %s lux: the sun drives the sky, and the "
        "sky light's captured cubemap is then a real sky rather than the black "
        "dome a twilight sun produces."
        % (fmt(read(comp, "atmosphere_sun_light")), fmt(lux)))
    return comp


# --------------------------------------------------------------------------- #
# 1b. the bounce: the warm fill Lumen would otherwise supply, and does not
# --------------------------------------------------------------------------- #

def find_by_label(world, cls, label):
    """Label-only lookup. `find_existing` falls back to hits[0], which for a
    second DirectionalLight would hand back the sun."""
    for a in unreal.GameplayStatics.get_all_actors_of_class(world, cls):
        if a.get_actor_label() == label:
            return a
    return None


def yaw_delta(a, b):
    """Smallest absolute difference between two yaw angles, in degrees."""
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def set_pitch_yaw(actor, pitch, yaw):
    """Write pitch+yaw, keeping only a spelling that READS BACK as asked.

    Same rule as `set_pitch`: `unreal.Rotator(a, b, c)` does not reliably mean
    (pitch, yaw, roll) through this binding, so the write is verified rather
    than trusted.
    """
    attempts = []
    try:
        attempts.append(("keyword", unreal.Rotator(pitch=pitch, yaw=yaw, roll=0.0)))
    except Exception:                                  # noqa: BLE001
        pass
    attempts.append(("pitch,yaw,roll", unreal.Rotator(pitch, yaw, 0.0)))
    attempts.append(("roll,pitch,yaw", unreal.Rotator(0.0, pitch, yaw)))
    for label, rot in attempts:
        try:
            actor.set_actor_rotation(rot, False)
        except Exception as exc:                       # noqa: BLE001
            say("  rot   via %-14s raised %s"
                % (label, str(exc).splitlines()[0][:50]))
            continue
        got = actor.get_actor_rotation()
        say("  rot   via %-14s -> reads (p %.1f, y %.1f, r %.1f)"
            % (label, got.pitch, got.yaw, got.roll))
        # UE normalises yaw into [-180, 180), so a requested 222 reads back as
        # -138. Same angle; compare around the circle, not on the number line.
        if abs(got.pitch - pitch) <= 0.01 and yaw_delta(got.yaw, yaw) <= 0.01:
            return got
    _FAILED.append("bounce rotation")
    say("  FAIL no spelling read back as pitch %.1f / yaw %.1f" % (pitch, yaw))
    return None


def fix_fill_lights(world):
    """A ring of near-flat warm lights standing in for the bounce Lumen owes us.

    Why an approximation is the right call on this target
    ----------------------------------------------------
    `r.GenerateMeshDistanceFields=0` leaves Lumen's surface cache empty, so
    indirect diffuse is exactly zero -- this script's own cvar report measures
    GI method 1 vs 0 changing the frame mean by 0.0. The only fill left is the
    SkyLight, and a sky is blue. The render target is an Intel Iris Xe
    (i5-12500H): real GI on integrated graphics would be slow and fragile, and
    the project already had a documented reason to keep distance fields off, so
    the fill is modelled explicitly instead of chased.

    Measured proof that the fill is the defect, on one ground-level frame
    (out/persp_after_retire.png) against its own base-colour frame
    (out/persp_albedo.png) at identical pixels:

        region          albedo (base colour)   final (lit)         B/R
        sunlit path     (215.7,202.7,190.0)    (212.9,215.4,224.2) 1.05  ok
        sunlit grass    ( 32.6, 82.7,  3.0)    (107.1,161.2, 72.7) 0.68  ok
        mid facade      ( 78.0, 71.4, 71.4)    ( 43.8, 59.8,100.7) 2.30  bad
        tree            ( 16.7, 16.1, 14.7)    (  9.4, 14.0, 25.5) 2.71  bad

    Neutral albedo in, blue out, and only on surfaces the sun does not face:
    sunlit tops match the albedo ruler while shaded verticals read like a blue
    floodlight at night. Raising the SkyLight is not the fix -- measured, going
    from intensity 6 to 12 took the near-black share from 13.8% to 0.0% but
    pushed median B/R from 1.06 to 1.33 and dropped frame std from 86 to 53,
    i.e. it washed the walls out without changing their hue.

    Why several lights, and why flat
    --------------------------------
    1 light fills only the 2 azimuths that face it. Measured on the same frame,
    facades whose normals were perpendicular to both the sun and a single fill
    light stayed at B/R 22-76 with luma 8. Real bounce arrives from the ground
    and from facing walls -- roughly horizontal directions, all around -- so
    these lights are pitched -12 deg and spaced 90 deg in azimuth:

        vertical face                     cos(12) = 0.98   <- the fill
        horizontal top face               sin(12) = 0.21   <- nearly free

    That ratio is the design. The top-down calibration against
    out/truth_colour.png already matches (measured -0.16 / +0.16 / +0.20 stops),
    and a fill that also lit the tops would break it; pitched flat, the ring
    fixes verticals without moving horizontals.

    They cast no shadow, because a bounce light IS light that already bounced,
    and they are warm (5200 K), because what they stand in for is sunlight that
    picked up the colour of dirt, concrete and brick. None of them drives the
    atmosphere, or the sky would contain four suns.
    """
    say("")
    say("--- DirectionalLights (fill ring) -----------------------------------")
    sun = find_by_label(world, unreal.DirectionalLight, SUN_LABEL)
    if sun is None:
        _FAILED.append("no sun for the fill azimuths")
        say("  FAIL %s is missing; the fill ring needs its azimuth" % SUN_LABEL)
        return []
    sun_rot = sun.get_actor_rotation()
    # `facing` is the normal the sun illuminates. The fill starts opposite it,
    # which is where the facades the sun misses point.
    facing = (sun_rot.yaw + 180.0) % 360.0

    pitch = env_float("MC_BOUNCE_PITCH", -12.0)
    temp = env_float("MC_BOUNCE_TEMP", 5200.0)
    main_lux = env_float("MC_BOUNCE_LUX", 15000.0)
    side_lux = env_float("MC_BOUNCE_SIDE_LUX", 9000.0)

    plan = (
        (BOUNCE_LABEL, 0.0, main_lux),
        (FILL_LEFT_LABEL, -90.0, side_lux),
        (FILL_RIGHT_LABEL, 90.0, side_lux),
    )

    out = []
    for label, offset, lux in plan:
        yaw = (facing + offset) % 360.0
        actor = find_by_label(world, unreal.DirectionalLight, label)
        if actor is None:
            try:
                eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
                actor = eas.spawn_actor_from_class(
                    unreal.DirectionalLight, unreal.Vector(0.0, 0.0, 0.0),
                    unreal.Rotator(pitch, yaw, 0.0))
            except Exception as exc:                   # noqa: BLE001
                say("  FAIL %s could not be spawned: %s"
                    % (label, str(exc).splitlines()[0][:60]))
                _FAILED.append("spawn " + label)
                continue
            if actor is None:
                say("  FAIL spawn returned None for %s" % label)
                _FAILED.append("spawn " + label)
                continue
            actor.set_actor_label(label)
            say("  spawned %s" % label)
        else:
            say("  %s already present -- rewriting it" % label)

        comp = actor.get_component_by_class(unreal.DirectionalLightComponent)
        if comp is None:
            say("  FAIL %s has no DirectionalLightComponent" % label)
            _FAILED.append("component " + label)
            continue

        say("  %s: yaw %.1f (sun-facing %.1f %+.0f), pitch %.1f, %s lux, %s K"
            % (label, yaw, facing, offset, pitch, fmt(lux), fmt(temp)))
        try:
            comp.set_intensity(lux)
        except Exception:                              # noqa: BLE001
            set_prop(comp, "intensity", lux, "intensity")
        set_prop(comp, "use_temperature", True, "use_temperature",
                 "temperature_enabled", "bUseTemperature")
        set_prop(comp, "temperature", temp, "temperature")
        set_prop(comp, "mobility", unreal.ComponentMobility.MOVABLE, "mobility")
        set_prop(comp, "cast_shadows", False, "cast_shadows", "bCastShadows")
        set_prop(comp, "atmosphere_sun_light", False, "atmosphere_sun_light",
                 "bAtmosphereSunLight")
        if set_pitch_yaw(actor, pitch, yaw) is None:
            continue

        say("  -- read back --")
        assert_close(label + " intensity", float(read(comp, "intensity")), lux)
        assert_close(label + " temperature", float(read(comp, "temperature")), temp)
        assert_close(label + " cast_shadows",
                     bool(read(comp, "cast_shadows")), False)
        assert_close(label + " atmosphere_sun_light",
                     bool(read(comp, "atmosphere_sun_light")), False)
        assert_close(label + " pitch", round(actor.get_actor_rotation().pitch, 3),
                     pitch)
        assert_close(label + " yaw",
                     round(yaw_delta(actor.get_actor_rotation().yaw, yaw), 3), 0.0)
        try:
            comp.mark_render_state_dirty()
        except Exception:                              # noqa: BLE001
            pass
        out.append(comp)
    return out


def fix_atmosphere(world):
    """Raise the rendered sky's own radiance, and nothing else.

    Measured defect: `tools/qa_persp_gates.py` puts the ground-level frame's sky
    segment at sRGB luma 82.8 against a 130..210 daylight gate (mean RGB
    (56,86,129) -- a dusk-blue dome), and that sky is DARKER than the sunlit
    grass in the same frame (luma 143). No real midday sky is darker than the
    ground it lights.

    Exposure cannot fix this. The level's exposure is pinned at 7170 precisely
    because that is where sunlit tops land on out/truth_colour.png (measured
    -0.16 / +0.16 / +0.20 stops), so moving it would trade a correct ground for
    a correct sky. So the sky's own radiance has to move.

    `sky_luminance_factor` on `SkyAtmosphereComponent` is that knob, and it is
    measured rather than assumed -- `tools/probe_sky_atmosphere.py` dumps the
    component and it is the only property that scales the rendered sky directly
    (current value (1,1,1); the scattering scales are 0.0331 Rayleigh / 0.0040
    Mie, i.e. physically-named parameters that changing would also change the
    sky's *colour*).

    The catch, and why the SkyLight is compensated
    ----------------------------------------------
    The SkyLight is `SLS_CAPTURED_SCENE` with real-time capture, so the cubemap
    it lights the level with is this same brighter sky: raising the factor by
    N raises the ambient blue fill by N as well, which is the opposite of what
    the frame needs. `fix_skylight` therefore divides its intensity by the same
    factor, holding the illumination constant while the sky the camera sees gets
    brighter. Appearance and illumination are two separate knobs; this function
    turns one and the other compensates.
    """
    say("")
    say("--- SkyAtmosphere ----------------------------------------------------")
    atmos = list(unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.SkyAtmosphere))
    if not atmos:
        say("  no SkyAtmosphere actor: the sky is whatever the SkyLight captured")
        return 1.0
    factor = env_float("MC_SKY_LUMINANCE", 2.7)
    got = None
    for a in atmos:
        comp = a.get_component_by_class(unreal.SkyAtmosphereComponent)
        if comp is None:
            continue
        say("  actor %s: sky_luminance_factor before = %s"
            % (a.get_actor_label(), fmt(read(comp, "sky_luminance_factor"))))
        # A LinearColor, so the multiplier is written per channel: a single
        # number here would also shift the sky's hue.
        colour = unreal.LinearColor(factor, factor, factor, 1.0)
        set_prop(comp, "sky_luminance_factor", colour, "sky_luminance_factor")
        got = read(comp, "sky_luminance_factor")
        say("  set sky_luminance_factor = %s (x%s on the dome only)"
            % (fmt(got), fmt(factor)))
        say("  the SkyLight intensity is divided by the same factor in "
            "fix_skylight, so the ambient fill this brighter sky would add is "
            "held constant -- only what the camera sees changes")
    if got is None:
        _FAILED.append("no SkyAtmosphereComponent")
        say("  FAIL no SkyAtmosphereComponent")
        return 1.0
    return factor


# --------------------------------------------------------------------------- #
# 2. the skylight: this is what has to carry the light
# --------------------------------------------------------------------------- #

def fix_skylight(world, sky_luminance=1.0):
    say("")
    say("--- SkyLight ---------------------------------------------------------")
    sky = find_existing(world, unreal.SkyLight, SKY_LABEL)
    if sky is None:
        _FAILED.append("no SkyLight")
        say("  FAIL no SkyLight in the level -- the shade will stay at 0")
        return None
    comp = sky.get_component_by_class(unreal.SkyLightComponent)
    if comp is None:
        _FAILED.append("skylight has no component")
        say("  FAIL %s has no SkyLightComponent" % SKY_LABEL)
        return None

    # MC_SKY_INTENSITY is the strength of the FILL, not of the component: the
    # cubemap is captured from a sky that fix_atmosphere has just brightened by
    # sky_luminance, so the component value is divided by that factor and the
    # light the level receives stays where it was measured.
    want_fill = env_float("MC_SKY_INTENSITY", 1.0)
    intensity = want_fill / max(sky_luminance, 1e-6)
    if sky_luminance != 1.0:
        say("  fill %.3f / sky luminance %.3f -> component intensity %.3f"
            % (want_fill, sky_luminance, intensity))
    lower = env_color("MC_LOWER_HEMI_COLOR", (0.30, 0.33, 0.38))

    say("  actor %s" % sky.get_actor_label())
    say("  before: intensity=%s mobility=%s real_time_capture=%s source_type=%s "
        "lower_hemisphere_is_black=%s lower_hemisphere_color=%s cubemap=%s"
        % (fmt(read(comp, "intensity")), fmt(read(comp, "mobility")),
           fmt(read(comp, "real_time_capture")), fmt(read(comp, "source_type")),
           fmt(read(comp, "lower_hemisphere_is_black")),
           fmt(read(comp, "lower_hemisphere_color")),
           fmt(read(comp, "cubemap"))))

    # The single change that lets the sky fill the shadows: the lower hemisphere
    # stops being black, so a wall still sees sky instead of a black floor.
    set_prop(comp, "lower_hemisphere_is_black", False,
             "lower_hemisphere_is_black", "bLowerHemisphereIsBlack")
    set_prop(comp, "lower_hemisphere_color", lower, "lower_hemisphere_color")
    set_prop(comp, "source_type",
             unreal.SkyLightSourceType.SLS_CAPTURED_SCENE, "source_type")
    set_prop(comp, "real_time_capture", True,
             "real_time_capture", "bRealTimeCapture")
    set_prop(comp, "mobility", unreal.ComponentMobility.MOVABLE, "mobility")
    try:
        comp.set_intensity(intensity)
        say("  set %-34s = %-26s (via set_intensity)"
            % ("intensity", fmt(intensity)))
    except Exception:                                  # noqa: BLE001
        set_prop(comp, "intensity", intensity, "intensity")
    # Sky tint. The captured cubemap of a Rayleigh sky is blue, so a face that
    # the sun does not reach is lit almost entirely blue -- measured at B/R 1.47
    # on a wall, where real daylight sits nearer 1.15. Real cameras white
    # balance this away; a rendering rig has to be told. The tint is a white
    # balance on the fill light, not a colour cast on the scene: it multiplies
    # the cubemap only, so sunlit faces (where the sun dominates) barely move.
    sky_colour = env_color("MC_SKY_COLOR", (1.0, 0.97, 0.90))
    try:
        comp.set_light_color(sky_colour)
        say("  set %-34s = (%.3f, %.3f, %.3f) (via set_light_color)"
            % ("light_color", sky_colour.r, sky_colour.g, sky_colour.b))
    except Exception as exc:                           # noqa: BLE001
        say("  note: skylight set_light_color raised %s; trying the property"
            % str(exc).splitlines()[0][:60])
        set_prop(comp, "light_color", sky_colour, "light_color")

    # The cubemap has to be re-captured AFTER the sun is white, otherwise it
    # feeds back whatever the previous lighting baked into it. Note: 5.8's
    # SkyLightComponent has no `mark_render_state_dirty` (that call raised), so
    # the recapture is attempted on its own and its outcome is reported.
    recaptured = False
    for fn in ("recapture_sky", "set_real_time_capture"):
        f = getattr(comp, fn, None)
        if not callable(f):
            continue
        try:
            if fn == "set_real_time_capture":
                f(True, True)                          # bNewValue, bForceUpdate
            else:
                f()
            recaptured = True
            say("  sky re-capture via %s -> ok" % fn)
            break
        except Exception as exc:                       # noqa: BLE001
            say("  sky re-capture via %s raised %s"
                % (fn, str(exc).splitlines()[0][:60]))
    if not recaptured:
        say("  note: no recapture entry point accepted; the cubemap in the level "
            "is whatever was last captured. With real_time_capture=True the "
            "next rendered frame re-captures it anyway, so the first capture "
            "after this script is the one that sees the white sun -- do not "
            "judge colours from a frame taken before that.")

    say("  -- read back --")
    assert_close("lower_hemisphere_is_black",
                 bool(read(comp, "lower_hemisphere_is_black")), False)
    assert_close("real_time_capture", bool(read(comp, "real_time_capture")), True)
    assert_close("intensity", float(read(comp, "intensity")), intensity)
    assert_close("mobility", str(read(comp, "mobility")),
                 str(unreal.ComponentMobility.MOVABLE))
    say("  measured calibration for this knob (10 lux sun, fixed capture "
        "exposure): intensity 1.0 moves the frame mean by 0.7/255 only, and the "
        "share of pixels at exactly (0,0,0) goes 13.5%% (1.0) -> 7.4%% (5.0) -> "
        "0.28%% (10.0). Default here is 5.0, not 1.0, for that reason.")
    return comp


# --------------------------------------------------------------------------- #
# 3. the post-process volume: make the level own the exposure
# --------------------------------------------------------------------------- #

def fix_postprocess(world):
    say("")
    say("--- PostProcessVolume ------------------------------------------------")
    ppv = find_existing(world, unreal.PostProcessVolume, PPV_LABEL)
    if ppv is None:
        _FAILED.append("no PostProcessVolume")
        say("  FAIL no %s -- the level cannot own its exposure" % PPV_LABEL)
        return None

    brightness = env_float("MC_EXPOSURE_BRIGHTNESS", 7.0)
    # Fixed exposure (min == max) is one number that has to serve two very
    # different views, and it cannot. Measured on this level: the top-down frame
    # needs ~7170 to land on out/truth_colour.png, while the ground-level view
    # needs ~3000 to read as daylight -- the same 1.25 stops apart, because a
    # sunlit horizontal surface is genuinely brighter than a camera looking at
    # a shadowed facade. Pinning one makes the other wrong.
    #
    # So the window is bounded instead of collapsed: adaptation picks the value,
    # but min/max bracket the two measured optimums, so it cannot wander to a
    # blown-out or a night-time exposure. Realistic daylight is also what the
    # route asks for -- a camera adapts -- and this is the same thing with a
    # leash on it.
    lo = env_float("MC_EXPOSURE_MIN", brightness)
    hi = env_float("MC_EXPOSURE_MAX", brightness)
    say("  exposure window: min %s .. max %s%s"
        % (fmt(lo), fmt(hi),
           " (fixed)" if lo == hi else " (bounded auto)"))
    say("  actor %s  unbound=%s enabled=%s blend_weight=%s"
        % (ppv.get_actor_label(), fmt(read(ppv, "unbound")),
           fmt(read(ppv, "enabled")), fmt(read(ppv, "blend_weight"))))

    st = ppv.get_editor_property("settings")
    say("  before: override_min=%s min=%s override_max=%s max=%s "
        "override_method=%s method=%s override_bias=%s bias=%s"
        % (fmt(read(st, "override_auto_exposure_min_brightness")),
           fmt(read(st, "auto_exposure_min_brightness")),
           fmt(read(st, "override_auto_exposure_max_brightness")),
           fmt(read(st, "auto_exposure_max_brightness")),
           fmt(read(st, "override_auto_exposure_method")),
           fmt(read(st, "auto_exposure_method")),
           fmt(read(st, "override_auto_exposure_bias")),
           fmt(read(st, "auto_exposure_bias"))))

    # A fixed exposure is min == max. The histogram method with a collapsed
    # window clamps the eye adaptation to that one value regardless of content,
    # which is what makes the game and a capture agree.
    writes = (
        ("auto_exposure_method", unreal.AutoExposureMethod.AEM_HISTOGRAM),
        ("override_auto_exposure_method", True),
        ("auto_exposure_min_brightness", lo),
        ("override_auto_exposure_min_brightness", True),
        ("auto_exposure_max_brightness", hi),
        ("override_auto_exposure_max_brightness", True),
        ("auto_exposure_bias", 0.0),
        ("override_auto_exposure_bias", True),
    )
    for k, v in writes:
        try:
            st.set_editor_property(k, v)
            say("  set settings.%-33s = %s" % (k, fmt(v)))
        except Exception as exc:                       # noqa: BLE001
            _FAILED.append("settings." + k)
            say("  FAIL settings.%-30s = %s (%s)"
                % (k, fmt(v), str(exc).splitlines()[0][:50]))

    # Post effects that fight "MC 标准贴图质量". Every one of these blurs or
    # veils texel edges, and at 16x16 per block a blurred texel is the whole
    # texture: bloom puts a halo on every bright tile, a vignette darkens the
    # frame corners so the same roof reads two different colours either side of
    # it, motion blur smears a walking player's view, and film grain is noise
    # with no counterpart in the source art. They are look decisions from a
    # cinematic preset, not from the map, so they are switched off rather than
    # left at their authored weights (0.6 bloom / 0.25 vignette).
    clean = (
        ("bloom_intensity", 0.0), ("override_bloom_intensity", True),
        ("vignette_intensity", 0.0), ("override_vignette_intensity", True),
        ("motion_blur_amount", 0.0), ("override_motion_blur_amount", True),
        ("film_grain_intensity", 0.0), ("override_film_grain_intensity", True),
        ("scene_fringe_intensity", 0.0), ("override_scene_fringe_intensity", True),
    )
    for k, v in clean:
        try:
            st.set_editor_property(k, v)
        except Exception as exc:                       # noqa: BLE001
            _FAILED.append("settings." + k)
            say("  FAIL settings.%-30s = %s (%s)"
                % (k, fmt(v), str(exc).splitlines()[0][:50]))
    say("  set bloom/vignette/motion-blur/grain/fringe to 0 "
        "(each would veil a 16x16 texel)")
    # `expand_gamut` lets the working colour space reach outside sRGB before the
    # tone curve. A Minecraft block texture is authored in sRGB and has nothing
    # outside it, so expanding the gamut cannot reveal detail -- it can only
    # invent colour, and it is what pushes a saturated tile off its own mean.
    # The engine default is 1.0 (fully expanded).
    try:
        st.set_editor_property("expand_gamut", 0.0)
        st.set_editor_property("override_expand_gamut", True)
        say("  set expand_gamut = 0 (the textures are sRGB; expanding invents "
            "colour that is not in the art)")
    except Exception as exc:                           # noqa: BLE001
        _FAILED.append("settings.expand_gamut")
        say("  FAIL settings.expand_gamut = 0 (%s)"
            % str(exc).splitlines()[0][:50])
    # Ambient occlusion is deliberately NOT zeroed. It veils a texel the same
    # way bloom does, but it is also the only contact darkening the scene has
    # (Lumen's indirect diffuse measures 0 here -- no mesh distance fields), and
    # without it every wall/ground junction reads as a seam of pure albedo. The
    # daylight route wants the corner shadow; it is left at the project default.

    # UE 5 has one tone curve. The 4.x `TonemapperType` enum (Filmic / ACES /
    # None) was removed when the ACES-derived curve became the only one, so
    # there is nothing to choose here and the enum no longer exists on `unreal`
    # at all -- reading it raised AttributeError and took the whole script down
    # after the exposure writes, which is how the first daylight attempt ended
    # up half-applied. What IS still exposed and worth setting:
    #
    #   tone_curve_amount  how much of that curve is applied (1 = full). Lower
    #                      values flatten the toe and lift the shadows, which is
    #                      the opposite of what a 16x16 texture wants.
    #   expand_gamut       how far outside sRGB the working gamut reaches before
    #                      the curve. High values are what push a saturated
    #                      Minecraft tile off its own colour, because a block
    #                      texture is authored in sRGB and has nothing outside it.
    #
    # Both are left at the project default unless asked for, and the defaults are
    # reported so a future colour investigation starts from measured values.
    say("  tone_curve_amount = %s, expand_gamut = %s (read, not written)"
        % (fmt(read(st, "tone_curve_amount")), fmt(read(st, "expand_gamut"))))

    ppv.set_editor_property("settings", st)
    st2 = ppv.get_editor_property("settings")
    say("  -- read back --")
    assert_close("settings.auto_exposure_min_brightness",
                 float(read(st2, "auto_exposure_min_brightness")), lo)
    assert_close("settings.auto_exposure_max_brightness",
                 float(read(st2, "auto_exposure_max_brightness")), hi)
    assert_close("settings.override_auto_exposure_min_brightness",
                 bool(read(st2, "override_auto_exposure_min_brightness")), True)
    assert_close("settings.override_auto_exposure_max_brightness",
                 bool(read(st2, "override_auto_exposure_max_brightness")), True)
    assert_close("settings.override_auto_exposure_bias",
                 bool(read(st2, "override_auto_exposure_bias")), True)

    if lo == hi:
        say("  exposure as set: fixed at brightness %s for both min and max. "
            "Engine conversion EV100 ~= log2(B * 100 / 1.2), so this is "
            "EV100 ~= %.2f (derived from the engine constant, not measured)."
            % (fmt(lo), _log2(lo * 100.0 / 1.2)))
    else:
        say("  exposure as set: bounded auto, brightness %s .. %s "
            "(EV100 ~= %.2f .. %.2f). A single fixed value cannot serve both "
            "the top-down calibration shot and a ground-level view -- measured "
            "1.25 stops apart -- so the window is clamped around both."
            % (fmt(lo), fmt(hi),
               _log2(lo * 100.0 / 1.2), _log2(hi * 100.0 / 1.2)))
    say("  capture agreement: tools/ortho_topdown.py leaves exposure to the "
        "level unless MC_ORTHO_PP_EXPOSURE=1, so a capture measures what ships. "
        "Setting that env var forces min=max on the SceneCapture instead and "
        "the frame will then disagree with the game by design.")
    return ppv


def _log2(x):
    try:
        import math
        return math.log(float(x), 2.0)
    except Exception:                                  # noqa: BLE001
        return float("nan")


# --------------------------------------------------------------------------- #
# 4. the fog: a light analytic haze, not a volumetric sheet
# --------------------------------------------------------------------------- #

def fix_fog(world):
    say("")
    say("--- ExponentialHeightFog ---------------------------------------------")
    fog = find_existing(world, unreal.ExponentialHeightFog, FOG_LABEL)
    if fog is None:
        say("  note: no %s in the level -- nothing to do (a sky-exposed world "
            "does not need fog)" % FOG_LABEL)
        return None
    comp = fog.get_component_by_class(unreal.ExponentialHeightFogComponent)
    if comp is None:
        _FAILED.append("fog has no component")
        say("  FAIL %s has no ExponentialHeightFogComponent" % FOG_LABEL)
        return None

    # 0.0002/cm is 2% per metre: 4 optical depths at 100 m, which turns the
    # middle distance into a flat sheet. Measured against the frame, the haze
    # was erasing the very texel detail the acceptance bar is about. 0.00006/cm
    # leaves aerial perspective (3 optical depths at 500 m) without erasing the
    # campus.
    density = env_float("MC_FOG_DENSITY", 0.00006)
    say("  before: density=%s height_falloff=%s volumetric=%s "
        "inscattering_luminance=%s start_distance=%s fog_max_opacity=%s"
        % (fmt(read(comp, "fog_density")), fmt(read(comp, "fog_height_falloff")),
           fmt(read(comp, "enable_volumetric_fog")),
           fmt(read(comp, "fog_inscattering_luminance")),
           fmt(read(comp, "start_distance")), fmt(read(comp, "fog_max_opacity"))))

    set_prop(comp, "enable_volumetric_fog", False,
             "enable_volumetric_fog", "bEnableVolumetricFog")
    set_prop(comp, "fog_density", density, "fog_density")
    # start_distance / fog_cutoff_distance / max_view_distance are deliberately
    # NOT touched: they decide the near-field clear zone and the cull.

    if os.environ.get("MC_FOG_MAX_OPACITY", "").strip():
        v = env_float("MC_FOG_MAX_OPACITY", 1.0)
        set_prop(comp, "fog_max_opacity", v, "fog_max_opacity")
    else:
        say("  left alone: fog_max_opacity = %s. It is the one lever that stops "
            "a 600 m frame reading as a uniform sheet WITHOUT touching the "
            "ground-level haze; set MC_FOG_MAX_OPACITY (e.g. 0.15) if that "
            "frame still washes out." % fmt(read(comp, "fog_max_opacity")))

    say("  -- read back --")
    assert_close("fog_density", float(read(comp, "fog_density")), density)
    assert_close("enable_volumetric_fog",
                 bool(read(comp, "enable_volumetric_fog")), False)

    # Inscattering colour: the parameter behind the COLOUR of the haze, and the
    # reason the horizon band read blue. Measured on out/persp_dome.png, the
    # per-cell gate map put 30-46% of the y=180..270 band over B/R 1.6 -- and
    # that band is the distant tree line and the far buildings, not a wall:
    # everything at 100 m and beyond sits inside 0.0002/cm * 20000 cm = 4
    # optical depths of (0.60, 0.72, 0.95) inscatter, so distance alone tints
    # the scene blue. Real midday haze desaturates toward the sky's own pale
    # grey-blue as it thickens, so the near-neutral value below is what a haze
    # layer should contribute; a saturated blue one is a stylisation.
    inscatter = env_color("MC_FOG_INSCATTER",
                          (0.58, 0.60, 0.63))
    set_prop(comp, "fog_inscattering_luminance", inscatter,
             "fog_inscattering_luminance")
    got_ins = read(comp, "fog_inscattering_luminance")
    say("  set fog_inscattering_luminance = %s (was (0.60,0.72,0.95): a blue "
        "veil that turned the whole horizon band over B/R 1.6)" % fmt(got_ins))
    assert_close("fog_inscattering_luminance.b",
                 round(float(read(comp, "fog_inscattering_luminance").b), 3),
                 round(float(inscatter.b), 3))
    return comp


# --------------------------------------------------------------------------- #
# 5. where the calibration stands, in numbers
# --------------------------------------------------------------------------- #

def report_calibration():
    """Print the three gates this rig is judged by, and the numbers behind them.

    The gates are measured on a frame, not here -- this only records the ruler
    so a later capture can be compared against something stable.
    """
    say("")
    say("--- acceptance gates (measured on a capture, not here) ---------------")
    say("  sky     sRGB luma 130..210 and B > G > R (a real daytime sky)")
    say("  ground  each channel within ~12/255 of out/truth_colour.png")
    say("  faces   no runaway blue: |B/R - 1| <= 0.25 on a vertical face")
    say("  ruler   out/truth_colour.png, grass top face reference (%d, %d, %d)"
        % TRUTH_GRASS)
    say("  capture with tools/ortho_topdown.py; MC_ORTHO_PP_EXPOSURE=1 pins the")
    say("  exposure for measurement, leaving it off measures what ships.")


# --------------------------------------------------------------------------- #

def _exposure_window_text():
    """Describe the level's exposure window for the final RESULT line."""
    base = env_float("MC_EXPOSURE_BRIGHTNESS", 7.0)
    lo = env_float("MC_EXPOSURE_MIN", base)
    hi = env_float("MC_EXPOSURE_MAX", base)
    if lo == hi:
        return "fixed at brightness %s" % fmt(lo)
    return "bounded auto %s..%s" % (fmt(lo), fmt(hi))


def main():
    say("=== fix_lighting_mc start %s ===" % time.strftime("%F %T"))
    say("engine %s" % unreal.SystemLibrary.get_engine_version())
    say("target: a fully lit top face renders at its own texture colour, ruler "
        "= out/truth_colour.png")

    ues = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
    world = ues.get_editor_world() if ues else None
    if world is None:
        world = unreal.EditorLevelLibrary.get_editor_world()
    if world is None:
        say("RESULT: FAIL no editor world")
        return 1
    if world.get_name() != "MCReplica":
        ok = False
        try:
            ok = bool(unreal.EditorLoadingAndSavingUtils.load_map(MAP))
        except Exception:                              # noqa: BLE001
            ok = False
        say("load(%s) -> %s" % (MAP, ok))
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        if world is None:
            say("RESULT: FAIL level did not load")
            return 1
    say("world: %s" % world.get_name())

    say("")
    say("--- renderer cvars that decide what the rig can do -------------------")
    for cvar in ("r.AllowStaticLighting", "r.DynamicGlobalIlluminationMethod",
                 "r.ReflectionMethod", "r.GenerateMeshDistanceFields",
                 "r.Lumen.TraceMeshSDFs", "r.DefaultFeature.AutoExposure"):
        try:
            v = unreal.SystemLibrary.get_console_variable_int_value(cvar)
        except Exception:                              # noqa: BLE001
            v = "<unreadable>"
        say("  %-34s %s" % (cvar, v))
    say("  GenerateMeshDistanceFields=0 leaves Lumen's surface cache empty: "
        "measured, GI method 1 vs 0 changes the frame mean by 0.0, so indirect "
        "diffuse is zero and the skylight is the only fill.")

    fix_sun(world)
    fix_fill_lights(world)
    fix_skylight(world, fix_atmosphere(world))
    fix_postprocess(world)
    fix_fog(world)
    report_calibration()

    say("")
    if os.environ.get("MC_SAVE_LEVEL", "").strip() in ("1", "true", "yes"):
        try:
            unreal.EditorLevelLibrary.save_current_level()
            say("level saved (MC_SAVE_LEVEL is on)")
        except Exception as exc:                       # noqa: BLE001
            _FAILED.append("save_current_level")
            say("FAIL save_current_level: %s" % str(exc).splitlines()[0][:80])
    else:
        say("level NOT saved (MC_SAVE_LEVEL is off): these values live in this "
            "editor session only. Set MC_SAVE_LEVEL=1 when the capture runs in a "
            "separate process.")

    say("")
    say("--- values to re-measure ---------------------------------------------")
    say("  sky light intensity      : %s" % fmt(env_float("MC_SKY_INTENSITY", 1.0)))
    say("  sun intensity / temp     : %s lux / %s K, tint %s"
        % (fmt(env_float("MC_SUN_LUX", 60000.0)), fmt(env_float("MC_SUN_TEMP", 6500)),
           fmt(env_color("MC_SUN_COLOR", (1.0, 1.0, 1.0)))))
    say("  sun pitch                : %s" % fmt(env_float("MC_SUN_PITCH", -50)))
    say("  sky dome luminance       : x%s (the fill is divided by it, so the "
        "dome gets brighter and the ambient does not)"
        % fmt(env_float("MC_SKY_LUMINANCE", 2.7)))
    say("  fill ring                : %s lux facing the unlit facades, %s lux "
        "either side, %s K, pitch %s, no shadow"
        % (fmt(env_float("MC_BOUNCE_LUX", 15000.0)),
           fmt(env_float("MC_BOUNCE_SIDE_LUX", 9000.0)),
           fmt(env_float("MC_BOUNCE_TEMP", 5200.0)),
           fmt(env_float("MC_BOUNCE_PITCH", -12.0))))
    say("  post-process exposure    : %s" % _exposure_window_text())
    say("  fog density              : %s /cm, volumetric off"
        % fmt(env_float("MC_FOG_DENSITY", 0.00006)))

    say("")
    if _FAILED:
        say("FAIL: %d value(s) did not land: %s"
            % (len(_FAILED), ", ".join(sorted(set(_FAILED)))))
        say("RESULT: FAIL light values not applied")
        return 1
    say("RESULT: PASS daylight look -- sun %s lux @ %s K pitch %s shadows %s "
        "movable, atmosphere sun light; fill ring %s + 2x%s lux @ %s K pitch %s "
        "shadowless; skylight %s captured live with the lower hemisphere lit; "
        "level exposure %s; bloom/vignette/grain/fringe off; fog %s/cm"
        % (fmt(env_float("MC_SUN_LUX", 60000.0)),
           fmt(env_float("MC_SUN_TEMP", 6500)),
           fmt(env_float("MC_SUN_PITCH", -50)),
           "on" if os.environ.get("MC_SUN_SHADOWS", "1").strip() not in ("", "0")
           else "off",
           fmt(env_float("MC_BOUNCE_LUX", 15000.0)),
           fmt(env_float("MC_BOUNCE_SIDE_LUX", 9000.0)),
           fmt(env_float("MC_BOUNCE_TEMP", 5200.0)),
           fmt(env_float("MC_BOUNCE_PITCH", -12.0)),
           fmt(env_float("MC_SKY_INTENSITY", 1.0)),
           _exposure_window_text(),
           fmt(env_float("MC_FOG_DENSITY", 0.00006))))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        say(traceback.format_exc())
        say("RESULT: FAIL uncaught exception")
        raise
