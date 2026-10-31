"""ensure_lighting.py -- put a sun and a skylight in the level, idempotently.

Why this exists
---------------
The level shipped with a SkyAtmosphere and nothing else. A census of every
actor in MC_Built gave:

    MCblk 1041, B 158, MC 40, T 24, SkyAtmosphere 1, PlayerStart 1

No DirectionalLight. No SkyLight. Every earlier "the render is black / the
render is blown out" observation was therefore the *sky dome* changing, never a
lit surface: with no light actors there is nothing for a surface to respond
to. It also explains why raising the sun to 100000 lux turned the frame white
(the sky saturated) while 1000 and 10000 lux left the ground unchanged -- the
-MCsuns flag only reconfigures an existing DirectionalLight, so with none
present it had nothing to act on. An earlier run of applylook.py even logged
"CreateLight: 0 actors" and that failure was never treated as blocking.

Everything else was verified correct while this was true: the terrain carries
676,656/676,656 triangles, its vertex normals are present and unit length
(MCMeshTools::CountUsableNormals reports 387,096/387,096 across four tiles),
the material graph samples T_MC_grass, and all 182 mesh actors are visible and
unhidden. Geometry, materials and visibility were never the problem.

Units
-----
UE 5 directional light intensity is in lux and the default exposure is auto, so
a daytime value in the low single digits is normal here. The values below are
modest on purpose: they were chosen to be legible, not physically exact, and
-MCsuns still overrides the intensity at runtime when it is passed.

Run:
    UnrealEditor-Cmd.exe <project> -ExecutePythonScript=tools/ensure_lighting.py
"""

import os
import sys
import time

import unreal

# MC_LOG_DIR is a DIRECTORY; join the filename (using it directly made open()
# hit the directory and fail with PermissionError on every build_world run).
OUT = os.path.join(os.environ.get(
    "MC_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..", "logs")),
    "ensure_lighting.txt")
_lines = []

#: Light values whose write failed. Each is a look decision the spec asks for
#: (sun lux, colour, temperature, skylight capture, no shadow pass), so a failure
#: fails the step rather than leaving the value silently unset.
_FAILED = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[MCLight] " + str(msg))
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_lines) + "\n")
    except Exception:
        pass


def find_existing(world, cls, label):
    for actor in unreal.GameplayStatics.get_all_actors_of_class(world, cls):
        if actor.get_actor_label() == label:
            return actor
    return None


#: Light values live on the light COMPONENT, not on the actor; writing one on the
#: actor raises "Failed to find property".
#:
#: Candidate lists exist because a UPROPERTY's Python name is not always the
#: guessable one: `bUseTemperature` is `use_temperature`, and some values are only
#: exposed as a UFUNCTION. Trying several and reporting which one took keeps the
#: log honest about what actually landed.
LIGHT_PROPS = {
    "intensity":         ("intensity",),
    "light_color":       ("light_color",),
    "temperature":       ("temperature",),
    "use_temperature":   ("use_temperature", "temperature_enabled",
                          "bUseTemperature"),
    "cast_shadows":      ("cast_shadows", "bCastShadows"),
    "real_time_capture": ("real_time_capture", "bRealTimeCapture"),
}


def set_on_component(actor, comp_cls, key, value):
    """Write one light value on the actor's light component.

    UFUNCTION first, UPROPERTY second: light colour is only exposed as
    ``SetLightColor(FLinearColor)``, while the property behind it is an
    ``FColor`` and rejects a ``LinearColor``.

    Returns True if it landed; a failure is recorded in ``_FAILED`` so the step
    cannot report PASS with a value silently missing.
    """
    comp = actor.get_component_by_class(comp_cls)
    if comp is None:
        say("FAIL %s: actor has no %s" % (key, comp_cls))
        return False
    fn = getattr(comp, "set_" + key, None)
    if callable(fn):
        for args in ((value,), (value, False)):
            try:
                fn(*args)
                say("%s = %r (via set_%s, %d arg)"
                    % (key, value, key, len(args)))
                return True
            except Exception:
                continue
    for name in LIGHT_PROPS[key]:
        try:
            comp.set_editor_property(name, value)
            say("%s = %r (via %s)" % (key, value, name))
            return True
        except Exception:
            continue
    _FAILED.append(key)
    say("FAIL %s: none of %s accepted, and set_%s did not take"
        % (key, ", ".join(LIGHT_PROPS[key]), key))
    return False


def make_sun(world):
        """A directional light angled the way a mid-morning sun would be.

        Pitch -58 rather than straight down: a sun directly overhead flattens every
        roof and wall face to the same value, which is exactly the "flat brown
        blobs" read that this pipeline was rebuilt to get away from.

        Shadows are OFF. The level still holds 1,041 legacy ISM clusters carrying
        1,171,144 instances, and as soon as a shadow-casting light exists the
        shadow depth pass overflows its 16-bit mesh-draw-command index and kills the
        process on startup:

            Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < ((uint16) 0xffff)
            [File:.../Runtime/Renderer/Private/ShadowSetup.cpp] [Line: 1611]

        Setting cast_shadow=False on those ISM components does not avoid it -- the
        editor-side write does not mark the render state dirty. Turning the sun's
        shadows off removes the pass entirely, which is the only reliable way to get
        an image out of this scene today. Shadows come back once the legacy voxel
        layer is retired, which is the real fix: those clusters are a superseded
        crutch that the terrain and structure meshes already replace.
        """
        light = unreal.DirectionalLight()
        # These are the SAME values AMCFrameCaptureGameMode::ApplyLook forces
        # at runtime (SunLux=10, rotation (-48,-135,0), sky 0.25, fog 0.0015,
        # exposure window 0.5..3.0). Writing them here too keeps the editor
        # viewport and the cooked build agreeing -- before this, the level was
        # authored at 4.0/(-58,0,34) and silently overwritten to 10/(-48,-135,0)
        # a second after every launch, two sources of truth fighting.
        #
        # Only ACTOR properties here; every light value goes on the component
        # (see set_on_component).
        light.set_actor_rotation(unreal.Rotator(-48.0, -135.0, 0.0), False)
        light.set_actor_location(unreal.Vector(0.0, 0.0, 40000.0), False, False)
        light.set_actor_label("MC_Sun")
        unreal.EditorLevelLibrary.add_actor_to_level(light) \
                if hasattr(unreal.EditorLevelLibrary, "add_actor_to_level") \
                else light.set_editor_property("folder_path", unreal.Name("/Game/MC"))

        comp = unreal.DirectionalLightComponent
        set_on_component(light, comp, "intensity", 10.0)
        set_on_component(light, comp, "light_color",
                         unreal.LinearColor(1.0, 0.96, 0.90))
        set_on_component(light, comp, "use_temperature", True)
        set_on_component(light, comp, "temperature", 5600.0)
        set_on_component(light, comp, "cast_shadows", False)

        say("sun created: rot=(-48,-135,0); values written to the component")
        return light


def make_skylight(world):
    """A skylight so shadowed faces get sky bounce instead of going black."""
    light = unreal.SkyLight()
    light.set_actor_rotation(unreal.Rotator(-38.0, 0.0, 0.0), False)
    light.set_actor_location(unreal.Vector(0.0, 0.0, 60000.0), False, False)
    light.set_actor_label("MC_SkyLight")
    unreal.EditorLevelLibrary.add_actor_to_level(light) \
        if hasattr(unreal.EditorLevelLibrary, "add_actor_to_level") \
        else light.set_editor_property("folder_path", unreal.Name("/Game/MC"))
    comp = unreal.SkyLightComponent
    set_on_component(light, comp, "intensity", 1.0)
    set_on_component(light, comp, "light_color",
                     unreal.LinearColor(0.62, 0.72, 0.90))
    set_on_component(light, comp, "real_time_capture", True)
    say("skylight created: rotation/location set; values written to the "
        "component")
    return light


def mute_legacy_clusters(world):
        """Stop the legacy voxel layer from casting shadows.

        The level still carries 1,041 MCReplicaPropCluster actors holding 1,171,144
        instances from the original cube-layer pipeline. They are already hidden in
        game, but an ISM component still gathers for the shadow pass, and with the
        sun's shadows enabled that overflows the 16-bit mesh-draw-command index:

            Assertion failed: NumAcceptedStaticMeshes >= 0 && MDCIdx < ((uint16) 0xffff)
            [File:.../Runtime/Renderer/Private/ShadowSetup.cpp] [Line: 1611]

        That assertion kills the process on startup, so it blocks every visual check.
        The clusters are a superseded crutch -- the terrain and structure meshes
        replace them -- so muting them is the correct end state, not a workaround.
        Their collision is left alone on purpose: the pawn still stands on
        MCReplicaPropCluster_449, and removing that before the terrain has collision
        would drop the player through the world.
        """
        clusters = unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.MCReplicaPropCluster)
        muted = 0
        for actor in clusters:
                for comp in actor.get_components_by_class(
                                unreal.InstancedStaticMeshComponent):
                        try:
                                comp.set_editor_property("cast_shadow", False)
                                muted += 1
                        except Exception:
                                pass
        # NOTE: as of this writing the mutation above does not stop the crash on its
        # own -- an editor-side property write does not mark the render state dirty,
        # so the shadow pass still gathers these clusters. Disabling the sun's
        # shadows is what actually unblocks the renderer. Mute the clusters anyway:
        # once the legacy layer is retired this becomes moot, and until then it is
        # one less thing that can shadow the real geometry.
        say("legacy voxel clusters: %d actors, %d components muted for shadows"
                % (len(clusters), muted))
        return muted


def main():
        # First line, so a crash cannot be mistaken for a clean pass from a
        # previous run's log.
        say("=== ensure_lighting start %s ===" % time.strftime("%F %T"))
        subsystems = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem)
        world = subsystems.get_editor_world() if subsystems else None
        if world is None:
                # Fall back: the level actors list is available even when the world
                # handle is not, which is enough to decide "do the lights already exist".
                world = unreal.EditorLevelLibrary.get_editor_world()
        if world is None:
                say("FAIL: no editor world")
                say("RESULT: FAIL no editor world")
                return 1

        sun = find_existing(world, unreal.DirectionalLight, "MC_Sun")
        skylight = find_existing(world, unreal.SkyLight, "MC_SkyLight")

        if sun is None:
                make_sun(world)
        else:
                # Re-author the values even when the sun exists, so the level
                # cannot keep an older set: AMCFrameCaptureGameMode::ApplyLook
                # forces SunLux=10 / (-48,-135,0) at runtime, and the editor copy
                # has to agree with it.
                dirlight = unreal.DirectionalLightComponent
                sun.set_actor_rotation(
                        unreal.Rotator(-48.0, -135.0, 0.0), False)
                set_on_component(sun, dirlight, "intensity", 10.0)
                set_on_component(sun, dirlight, "light_color",
                                 unreal.LinearColor(1.0, 0.96, 0.90))
                set_on_component(sun, dirlight, "use_temperature", True)
                set_on_component(sun, dirlight, "temperature", 5600.0)
                set_on_component(sun, dirlight, "cast_shadows", False)
                say("sun already present: re-authored to 10.0 / (-48,-135,0)")
        if skylight is None:
                make_skylight(world)
        else:
                skycomp = unreal.SkyLightComponent
                set_on_component(skylight, skycomp, "intensity", 1.0)
                set_on_component(skylight, skycomp, "light_color",
                                 unreal.LinearColor(0.62, 0.72, 0.90))
                set_on_component(skylight, skycomp, "real_time_capture", True)
                say("skylight already present: values re-asserted on the component")

        mute_legacy_clusters(world)

        # Census after, so the result is measured rather than assumed.
        suns = unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.DirectionalLight)
        skies = unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.SkyLight)
        say("after: DirectionalLight=%d SkyLight=%d" % (len(suns), len(skies)))

        if not suns:
                say("FAIL: no DirectionalLight in the level after the fix -- nothing "
                        "will be lit")
                say("RESULT: FAIL no DirectionalLight after the fix")
                return 1

        if _FAILED:
                say("FAIL: %d light value(s) never landed: %s"
                    % (len(_FAILED), ", ".join(sorted(set(_FAILED)))))
                say("RESULT: FAIL light values not applied")
                return 1

        # Read the values back. Counting actors is not a check: the component must
        # report 10.0 lux or the step has not done its job.
        sun_comp = suns[0].get_component_by_class(unreal.DirectionalLightComponent)
        got = None
        if sun_comp is not None:
                try:
                        got = float(sun_comp.get_editor_property("intensity"))
                except Exception:
                        got = None
        if got is None or abs(got - 10.0) > 1e-3:
                say("FAIL: sun intensity reads %r, expected 10.0" % (got,))
                say("RESULT: FAIL sun intensity not applied")
                return 1
        say("verified: sun intensity = %.3f lux on the component" % got)

        unreal.EditorLevelLibrary.save_current_level()
        say("level saved")
        say("RESULT: PASS sun=%d skylight=%d sun_lux=%.2f"
                % (len(suns), len(skies), got))
        return 0


if __name__ == "__main__":
    # Always leave a RESULT line, including on an uncaught exception, so callers
    # that grep for it can tell failure from a missing log.
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        say(traceback.format_exc())
        say("RESULT: FAIL uncaught exception")
        raise