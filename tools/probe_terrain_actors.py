# -*- coding: utf-8 -*-
"""
probe_terrain_actors.py -- why does the ground not appear in a render?

The measured state (2026-11-01) is: a perspective camera standing on the sports
field sees sky below the horizon, and the orthographic top-down frame shows the
terrain's columns as exactly the same colour as the sky (per-128-block-tile
correlation 1:1 against the truth map, 88.7% of ground columns missing). The
structures do draw, darkly. So the terrain layer is the one going missing, and
"the mesh has no geometry" and "the renderer is told to skip it" look identical
from a screenshot.

This reads, per terrain actor: the mesh the component points at, its triangle
and section counts on LOD 0, its bounds, and every visibility flag that would
remove it from a capture but not from an editor viewport. Read-only.

Run:

    UnrealEditor-Cmd.exe MCReplica.uproject ^
        -ExecutePythonScript=<repo>/tools/probe_terrain_actors.py ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import os
import traceback

import unreal

OUT = os.path.join(os.environ.get(
    "MC_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..", "logs")),
    "probe_terrain_actors.txt")
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCTERR] " + str(s))
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def read(obj, *names):
    """First name that reads. `None` means "not readable", never "false"."""
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


def flags(sc):
    keys = ("visible", "hidden_in_scene_capture", "visible_in_scene_capture_only",
            "b_hidden_in_scene_capture", "b_visible_in_scene_capture_only",
            "render_in_main_pass", "render_in_depth_pass", "cast_shadow",
            "is_temporarily_hidden_in_editor", "is_actor_hidden_in_game",
            "component_tags", "lod_parent", "detail_mode")
    out = {}
    for k in keys:
        if k == "visible":
            try:
                out[k] = sc.is_visible()
                continue
            except Exception:                          # noqa: BLE001
                pass
        if k in ("is_temporarily_hidden_in_editor", "is_actor_hidden_in_game"):
            continue
        out[k] = read(sc, k)
    for k in ("is_temporarily_hidden_in_editor", "is_actor_hidden_in_game"):
        out[k] = read(sc.get_owner(), k)
    return out


def main():
    say("=== probe_terrain_actors ===")
    unreal.EditorLoadingAndSavingUtils.load_map(MAP)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    actors = unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.StaticMeshActor)
    say("StaticMeshActors: %d" % len(actors))

    n_terr = n_bld = 0
    tris_terr = tris_bld = 0
    suspicious = []
    for a in actors:
        lbl = str(a.get_actor_label())
        if not lbl.startswith(("T_", "B_", "V_")):
            continue
        sc = read(a, "static_mesh_component", "static_mesh_component_0")
        mesh = read(sc, "static_mesh") if sc is not None else None
        if not isinstance(mesh, unreal.StaticMesh):
            say("%-24s NO MESH (%r)" % (lbl, mesh))
            suspicious.append(lbl + ": no mesh")
            continue
        try:
            tris = mesh.get_num_triangles(0)
        except Exception:                              # noqa: BLE001
            tris = read(mesh, "get_num_triangles")
        try:
            secs = mesh.get_num_sections(0)
        except Exception:                              # noqa: BLE001
            secs = len(read(mesh, "static_materials") or [])
        lods = read(mesh, "get_num_lods")
        o, e = a.get_actor_bounds(False)
        if lbl.startswith("T_"):
            n_terr += 1
            tris_terr += tris if isinstance(tris, int) else 0
        else:
            n_bld += 1
            tris_bld += tris if isinstance(tris, int) else 0
        if lbl.startswith("T_") or lbl in ("B_bld_028_structure",):
            say("%-24s tris=%-7s secs=%-3s lods=%-3s bounds x[%.0f,%.0f] y[%.0f,%.0f] z[%.0f,%.0f]"
                % (lbl, tris, secs, lods, o.x - e.x, o.x + e.x, o.y - e.y,
                   o.y + e.y, o.z - e.z, o.z + e.z))
            say("    %s" % flags(sc))

    say("")
    say("terrain actors %d, triangles %d   |   other actors %d, triangles %d"
        % (n_terr, tris_terr, n_bld, tris_bld))
    if suspicious:
        say("suspicious: %s" % "; ".join(suspicious[:20]))
    say("RESULT: %s" % ("PASS" if n_terr and tris_terr else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        say("FAILED:\n" + traceback.format_exc())
