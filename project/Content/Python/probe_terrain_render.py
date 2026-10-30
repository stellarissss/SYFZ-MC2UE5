# -*- coding: utf-8 -*-
"""probe_terrain_render.py -- why does the terrain draw nothing?

Context
-------
The runtime probe in MCConsoleCommands reports ``terrain(vis=24 col=0)``. Read
what that actually measures (MCConsoleCommands.cpp:96-126): it iterates
AStaticMeshActor, takes the mesh asset, and counts it as "visual" if the asset
name contains ``terrain_``/``overworld`` and does not start with ``C_``. **It
never asks the renderer anything.** So ``vis=24`` means "24 terrain mesh actors
exist", not "24 terrain meshes are drawn". The game window shows empty sky, so
those two statements disagree, and the probe cannot say which side is wrong.

This script asks the renderer-side questions the old probe did not:

  * actor: location / rotation / **scale** (a zero scale silently collapses the
    component bounds and nothing is ever drawn),
  * component: visible flag, hidden-in-game, registered, **component bounds**
    (the renderer culls on these, not on the asset's bounds),
  * component: override materials vs. the asset's slot array,
  * asset: section count, per-section material index, LOD count, triangle count,
    Nanite settings, and the asset bounds.

Anything that differs between "the asset is fine" and "the component is
invisible / degenerate / unmaterialed" shows up here.

Run inside the editor:
    UnrealEditor-Cmd.exe <uproject> -ExecutePythonScript=<this> \
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import math
import os

import unreal

OUT_NAME = "terrain_render.txt"
_log = []


def say(msg):
    _log.append(str(msg))
    unreal.log("[MCTERR] " + str(msg))
    with open(_out_path(), "w", encoding="utf-8") as fh:
        fh.write("\n".join(_log) + "\n")


def _out_path():
    d = os.environ.get("MC_LOG_DIR")
    if not d:
        here = os.path.dirname(os.path.abspath(__file__))
        mc2ue5 = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(here))))
        d = os.path.join(mc2ue5, "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, OUT_NAME)


def vec(v):
    return "(%.1f, %.1f, %.1f)" % (v.x, v.y, v.z)


def main():
    say("=== terrain actors, renderer-side ===")
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()

    terrain_actors = []
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        comp = a.get_component_by_class(unreal.StaticMeshComponent)
        if comp is None:
            continue
        mesh = comp.static_mesh
        if mesh is None:
            continue
        if "terrain_" not in mesh.get_name() and "overworld" not in mesh.get_name():
            continue
        terrain_actors.append((a, comp, mesh))

    say("terrain StaticMeshActors: %d" % len(terrain_actors))
    say("")

    worst = []
    for a, comp, mesh in terrain_actors[:6]:
        say("--- %s" % a.get_actor_label())
        say("    mesh asset      %s" % mesh.get_name())
        say("    actor loc/rot   %s  yaw=%.1f"
            % (vec(a.get_actor_location()), a.get_actor_rotation().yaw))
        sc = a.get_actor_scale3d()
        say("    actor scale     %s" % vec(sc))
        say("    component vis   visible=%s hidden_in_game=%s"
            % (_safe(lambda: comp.is_visible(), "?"),
               _safe(lambda: a.is_hidden(), "?")))
        say("    comp registered %s" % _safe(lambda: comp.is_registered(), "?"))
        try:
            b = comp.bounds
            say("    comp bounds     origin=%s extent=%s"
                % (vec(b.origin), vec(b.box_extent)))
        except Exception as exc:
            say("    comp bounds     FAILED %s" % str(exc)[:80])
        say("    override mats   %s" % _safe(
            lambda: comp.get_num_override_materials(), "?"))
        say("    asset sections  %s   lods=%s   tris=%s"
            % (_safe(lambda: mesh.get_num_sections(0), "?"),
               _safe(lambda: mesh.get_num_lods(), "?"),
               _safe(lambda: unreal.StaticMeshLibrary.
                     get_static_mesh_triangle_count(mesh), "?")))
        say("    asset mats      %s" % _safe(
            lambda: mesh.get_num_materials(0), "?"))
        say("    section->mat    %s" % _sections(mesh))
        say("    nanite          %s" % _nanite(mesh))
        say("    component mats  %s" % _component_mats(comp, mesh))

    say("")
    say("=== summary ===")
    say("terrain actors: %d" % len(terrain_actors))
    for a, comp, mesh in terrain_actors:
        sc = a.get_actor_scale3d()
        problems = []
        if abs(sc.x) < 1e-6 or abs(sc.y) < 1e-6 or abs(sc.z) < 1e-6:
            problems.append("ZERO SCALE")
        try:
            if not comp.is_visible():
                problems.append("COMPONENT NOT VISIBLE")
        except Exception:
            pass
        try:
            if a.is_hidden():
                problems.append("ACTOR HIDDEN")
        except Exception:
            pass
        try:
            b = comp.bounds
            if (abs(b.box_extent.x) < 1.0 and abs(b.box_extent.y) < 1.0
                    and abs(b.box_extent.z) < 1.0):
                problems.append("DEGENERATE COMPONENT BOUNDS")
            if not all(math.isfinite(v) for v in
                       (b.origin.x, b.origin.y, b.origin.z,
                        b.box_extent.x, b.box_extent.y, b.box_extent.z)):
                problems.append("NON-FINITE COMPONENT BOUNDS")
        except Exception:
            problems.append("NO COMPONENT BOUNDS")
        if problems:
            worst.append((a.get_actor_label(), mesh.get_name(),
                          ", ".join(problems)))
    if worst:
        say("--- actors with problems ---")
        for label, mesh, why in worst:
            say("  %-26s %-28s %s" % (label[:26], mesh[:28], why))
        say("")
        say("RESULT: FAIL -- %d of %d terrain actors are degenerate or hidden"
            % (len(worst), len(terrain_actors)))
        return 1
    say("RESULT: PASS -- no degenerate/hidden terrain actor found "
        "(this does NOT prove the mesh draws; it only clears the actor level)")
    return 0


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _sections(mesh):
    out = []
    try:
        n = mesh.get_num_sections(0)
        for i in range(n):
            out.append("%d" % mesh.get_material_index(0, i))
    except Exception as exc:
        return "FAILED %s" % str(exc)[:60]
    return ",".join(out)


def _nanite(mesh):
    try:
        ns = mesh.get_editor_property("nanite_settings")
        return "enabled=%s" % getattr(ns, "enabled", "?")
    except Exception as exc:
        return "unavailable (%s)" % str(exc)[:50]


def _component_mats(comp, mesh):
    out = []
    try:
        n = comp.get_num_materials()
        for i in range(n):
            m = comp.get_material(i)
            out.append(m.get_name() if m else "None")
    except Exception as exc:
        return "FAILED %s" % str(exc)[:60]
    return ",".join(out[:14]) if out else "(none)"


if __name__ == "__main__":
    raise SystemExit(main())
