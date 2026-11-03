# -*- coding: utf-8 -*-
"""
probe_spawn_surroundings.py -- what is standing right next to the player spawn?

Why this exists
---------------
The ground-level capture camera is the player spawn (1700, 21500, 700) yaw 93.4,
and every frame taken from it has two near-black slabs framing the left and right
edges: measured 44-73% of those columns under luma 12. A player sees them the
instant the level loads, so they are a defect to explain, not a framing choice.

Two explanations survive the lighting numbers: an object close enough to the
camera that the shot is half inside it, or a surface whose normals get no light
from the sun or from the three fill directions. Guessing between them has
already cost time; the level can simply be asked. This prints every actor within
the given radius of the spawn, nearest first, with bounds and mesh, so the
answer is a name and a distance rather than a theory.

Read-only. Nothing is written, nothing is saved.

Run (editor-side; `-nullrhi` is fine, nothing is drawn):

    UnrealEditor-Cmd.exe <abs>/project/MCReplica.uproject \
        -ExecutePythonScript=<abs>/tools/probe_spawn_surroundings.py \
        -nullrhi -unattended -nopause -nosplash -stdout
"""
import os
import sys
import traceback

import unreal

LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "logs")
OUT = os.path.join(LOG_DIR, "probe_spawn_surroundings.txt")
MAP = "/Game/Maps/MCReplica"
RADIUS = float(os.environ.get("MC_PROBE_RADIUS", "4000"))
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSPAWN] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def main():
    say("=== probe_spawn_surroundings ===")
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

    starts = list(unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.PlayerStart))
    if not starts:
        say("RESULT: FAIL no PlayerStart")
        return 1
    loc = starts[0].get_actor_location()
    say("PlayerStart %s at (%.0f, %.0f, %.0f) yaw %.1f"
        % (starts[0].get_actor_label(), loc.x, loc.y, loc.z,
           starts[0].get_actor_rotation().yaw))

    rows = []
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        try:
            o, e = a.get_actor_bounds(False)
        except Exception:                              # noqa: BLE001
            continue
        d = ((o.x - loc.x) ** 2 + (o.y - loc.y) ** 2) ** 0.5
        if d > RADIUS:
            continue
        mesh = "<none>"
        try:
            comp = a.get_component_by_class(unreal.StaticMeshComponent)
            m = comp.get_editor_property("static_mesh") if comp else None
            mesh = m.get_name() if m else "<none>"
        except Exception:                              # noqa: BLE001
            pass
        rows.append((d, a.get_actor_label(), mesh,
                     (o.x, o.y, o.z), (e.x, e.y, e.z)))

    rows.sort()
    say("")
    say("%d StaticMeshActor(s) within %.0f cm of the spawn, nearest first:"
        % (len(rows), RADIUS))
    say("  %-8s %-26s %-26s %s" % ("dist", "label", "mesh", "origin / extent"))
    for d, label, mesh, o, e in rows[:20]:
        say("  %7.0f %-26s %-26s (%.0f,%.0f,%.0f) ext(%.0f,%.0f,%.0f)"
            % (d, label[:26], mesh[:26], o[0], o[1], o[2], e[0], e[1], e[2]))

    # The frame's two dark slabs are at the extreme left and right of a yaw 93.4
    # view, i.e. roughly east-west of the camera. Call those out by bearing so
    # the next step does not have to re-derive which row matters.
    say("")
    say("bearing from the spawn (0 = +X / yaw 0, 90 = +Y / yaw 90):")
    for d, label, mesh, o, e in rows[:10]:
        bearing = (unreal.MathLibrary.atan2(o[1] - loc.y, o[0] - loc.x)
                   * 180.0 / 3.141592653589793) % 360.0
        say("  %-26s dist %6.0f  bearing %6.1f  height %.0f..%.0f"
            % (label[:26], d, bearing, o[2] - e[2], o[2] + e[2]))

    # The terrain tiles and the vegetation are in the same actor class, so a
    # short actor list here means the near field is genuinely empty -- which
    # would point at vegetation instancing rather than at a static mesh.
    say("")
    veg = 0
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.InstancedStaticMeshActor):
        veg += 1
    say("InstancedStaticMeshActor(s) in the level: %d" % veg)

    say("")
    say("RESULT: PASS %d actor(s) within %.0f cm" % (len(rows), RADIUS))
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
