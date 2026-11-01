# -*- coding: utf-8 -*-
"""
probe_terrain_sections.py -- do the terrain tiles' sections resolve a material?

Measured state (2026-11-01): the 24 terrain tiles each report 32,768 triangles
and correct bounds, are visible, are not hidden in game, and yet 96.6% of the
columns that the truth map says are grass are pure black in a BASE_COLOR
capture -- i.e. no geometry is drawn there at all. The structure meshes, which
arrive through the OBJ importer, do draw.

The two paths differ in one important way: `build_terrain_meshes.py` builds the
mesh by hand from a mesh description, creating one polygon group per material
and then writing `static_materials` itself. If a section's material index does
not resolve inside that array, the section has no material and the renderer
skips it -- with no error anywhere. This reads the built assets and compares
section count against the material array, per tile and per section.

Read-only. Run:

    UnrealEditor-Cmd.exe MCReplica.uproject ^
        -ExecutePythonScript=<repo>/tools/probe_terrain_sections.py ^
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import os
import traceback

import unreal

OUT = os.path.join(os.environ.get(
    "MC_LOG_DIR",
    os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "..", "logs")),
    "probe_terrain_sections.txt")
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSEC] " + str(s))
    try:
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def read(obj, *names):
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


def describe(a, tag):
    sc = read(a, "static_mesh_component", "static_mesh_component_0")
    mesh = read(sc, "static_mesh") if sc is not None else None
    if not isinstance(mesh, unreal.StaticMesh):
        say("%-24s NO MESH" % tag)
        return
    try:
        secs = mesh.get_num_sections(0)
    except Exception:                                  # noqa: BLE001
        secs = "<n/a>"
    mats = read(mesh, "static_materials") or []
    resolved = 0
    names = []
    for m in mats:
        mi = read(m, "material_interface")
        slot = read(m, "material_slot_name")
        names.append(str(slot))
        if mi is not None:
            resolved += 1
    nanite = read(mesh, "nanite_settings")
    enabled = read(mesh, "has_nanite_data")
    say("%-24s sections=%-3s materials=%-3d resolved=%-3d nanite=%r has_data=%r"
        % (tag, secs, len(mats), resolved, nanite, enabled))
    say("      slots: %s" % ", ".join(names[:10]))
    # The material the renderer will actually use for each section: this is the
    # question the mesh-description path can get wrong silently.
    for s in range(min(int(secs) if isinstance(secs, int) else 0, 4)):
        mi = "<n/a>"
        try:
            mi = mesh.get_material(s)
        except Exception:                              # noqa: BLE001
            try:
                mi = sc.get_material(s)
            except Exception:                          # noqa: BLE001
                pass
        say("      section %d -> %r" % (s, mi))


def main():
    say("=== probe_terrain_sections ===")
    unreal.EditorLoadingAndSavingUtils.load_map(MAP)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    actors = unreal.GameplayStatics.get_all_actors_of_class(
        world, unreal.StaticMeshActor)
    say("StaticMeshActors: %d" % len(actors))
    n_t = 0
    for a in actors:
        lbl = str(a.get_actor_label())
        if lbl.startswith("T_") and n_t < 4:
            n_t += 1
            describe(a, lbl)
    for a in actors:
        lbl = str(a.get_actor_label())
        if lbl in ("B_bld_028_structure", "B_bld_001_structure"):
            describe(a, lbl)
    say("--- done ---")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        say("FAILED:\n" + traceback.format_exc())
