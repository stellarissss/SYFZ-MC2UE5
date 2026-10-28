# -*- coding: utf-8 -*-
"""
probe_sections.py -- per-LOD section -> MaterialIndex -> resolved material.

"the slot array is right" and "the draw call sees the right material" are
different claims, and the material chain now hinges on the second one. The slot
array (static_materials) is verified correct, and the component resolves every
slot to M_MC_<family>. But LOD0's render data is a list of *sections*, each with
its own MaterialIndex into the slot array. If a section index is out of range,
or every section points at slot 0, the visible surface is wrong even though both
arrays look right in isolation.

API placement, learned the hard way: on UE 5.8 these live on the **StaticMesh**
(`get_num_sections`, `get_material_index`, `get_num_lods`), NOT on
StaticMeshComponent -- `StaticMeshComponent.get_num_sections` raises
AttributeError. Names are also exercised rather than assumed, because several
guesses on this engine have been wrong (get_static_mesh, imported_size_x).

Read-only: loads the saved map, prints, saves nothing.

Run:
  UnrealEditor-Cmd.exe MCReplica.uproject \
      -ExecutePythonScript=tools/probe_sections.py -nullrhi -unattended -nopause
"""

import json
import os
import traceback

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/probe_sections.txt"
STRUCT = "G:/BaiduNetdiskDownload/real/MC2UE5/repo/out/structures"
TERRAIN = "G:/BaiduNetdiskDownload/real/MC2UE5/repo/out/terrain"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSECT] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def obj_tris(path):
    t = 0
    if not os.path.isfile(path):
        return None
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            if line.startswith("f "):
                k = len(line.split()) - 1
                t += (k - 2) if k >= 3 else 0
    return t


def call(fn, *args):
    try:
        return fn(*args), None
    except Exception as exc:
        return None, str(exc)[:100]


def describe_mesh(m, obj_path, label, slotted_names):
    say("--- %s ---" % label)
    want = obj_tris(obj_path)
    say("  OBJ triangles: %s" % want)

    n_lods, err = call(m.get_num_lods)
    say("  get_num_lods() -> %s%s" % (n_lods, "" if not err else "  [%s]" % err))
    if n_lods is None:
        n_lods = 1

    for lod in range(int(n_lods)):
        n_sec, err = call(m.get_num_sections, lod)
        got_tris, _ = call(m.get_num_triangles, lod)
        say("  LOD%d: sections=%s  triangles=%s"
            % (lod, n_sec if err is None else "[%s]" % err, got_tris))
        if n_sec is None:
            continue
        idxs = []
        for i in range(int(n_sec)):
            mi, err = call(m.get_material_index, lod, i)
            if err is not None:
                say("      sec%-2d get_material_index raised %s" % (i, err))
                continue
            nm = (slotted_names[mi] if isinstance(mi, int)
                  and 0 <= mi < len(slotted_names) else "*** OOR ***")
            flag = ("" if isinstance(mi, int) and 0 <= mi < len(slotted_names)
                    else "  *** out of range ***")
            idxs.append(mi)
            say("      sec%-2d MaterialIndex=%s -> slot '%s'%s"
                % (i, mi, nm, flag))
        if idxs:
            uniq = sorted(set(idxs))
            say("      distinct MaterialIndex values: %s%s"
                % (uniq,
                   "   <-- ALL SECTIONS SHARE ONE SLOT" if len(uniq) == 1
                   and len(idxs) > 1 else ""))
    return want


def main():
    try:
        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()
        by_label = {}
        for a in unreal.GameplayStatics.get_all_actors_of_class(
                world, unreal.StaticMeshActor):
            by_label[a.get_actor_label()] = a
        say("StaticMeshActors in map: %d" % len(by_label))

        # Discover the real API surface once, so a rename shows up as evidence
        # rather than as a silent skip.
        m0 = unreal.load_asset("/Game/MC/Structures/bld_001_structure")
        if m0 is None:
            say("FATAL: bld_001_structure missing")
            return
        say("StaticMesh members (section/lod/material): %s"
            % sorted(n for n in dir(m0) if any(
                k in n.lower() for k in ("section", "lod", "material"))))

        probes = [
            ("B_bld_001_structure", "/Game/MC/Structures/bld_001_structure",
             os.path.join(STRUCT, "bld_001_structure.obj")),
            ("B_bld_028_structure", "/Game/MC/Structures/bld_028_structure",
             os.path.join(STRUCT, "bld_028_structure.obj")),
            ("B_bld_001_detail", "/Game/MC/Structures/bld_001_detail",
             os.path.join(STRUCT, "bld_001_detail.obj")),
            ("T_terrain_-144_-544", "/Game/MC/Terrain/terrain_-144_-544",
             os.path.join(TERRAIN, "terrain_-144_-544.obj")),
            ("T_terrain_-144_-416", "/Game/MC/Terrain/terrain_-144_-416",
             os.path.join(TERRAIN, "terrain_-144_-416.obj")),
            ("T_terrain_-016_-032", "/Game/MC/Terrain/terrain_-016_-032",
             os.path.join(TERRAIN, "terrain_-016_-032.obj")),
        ]

        say("")
        say("=== per-LOD section -> MaterialIndex -> slot ===")
        for label, asset_path, obj_path in probes:
            m = unreal.load_asset(asset_path)
            if m is None:
                say("--- %s --- MISSING ASSET" % label)
                continue
            arr = m.get_editor_property("static_materials") or []
            names = []
            for b in arr:
                try:
                    names.append(str(b.get_editor_property("material_slot_name")))
                except Exception:
                    names.append("<?>")
            say("")
            say("--- %s   slots(%d)=%s" % (label, len(names), names))
            describe_mesh(m, obj_path, label, names)
            # which slots are actually referenced by a section?
            n_lods, _ = call(m.get_num_lods)
            n_sec, _ = call(m.get_num_sections, 0)
            used = set()
            if n_sec is not None:
                for i in range(int(n_sec)):
                    mi, _ = call(m.get_material_index, 0, i)
                    if isinstance(mi, int):
                        used.add(mi)
            unused = [names[i] for i in range(len(names)) if i not in used]
            say("    slots referenced by a section: %d / %d"
                % (len(used), len(names)))
            if unused:
                say("    slots NOT referenced (shadowed): %s" % unused)

        # Terrain-wide: which families does each tile resolve, and how many
        # sections? A tile with 4 usemtl groups and only 1 section means three
        # families are unreachable.
        say("")
        say("=== all 24 terrain tiles: sections vs OBJ groups ===")
        tman = json.load(open(os.path.join(TERRAIN, "manifest.json")))
        bad = 0
        for t in tman["tiles"]:
            name = os.path.splitext(os.path.basename(t["obj"]))[0]
            m = unreal.load_asset("/Game/MC/Terrain/" + name)
            if m is None:
                say("  %-22s MISSING" % name)
                bad += 1
                continue
            obj_path = os.path.join(TERRAIN, os.path.basename(t["obj"]))
            groups = []
            with open(obj_path, "r", errors="replace") as fh:
                for line in fh:
                    if line.startswith("usemtl "):
                        g = line.split(None, 1)[1].strip()
                        if g not in groups:
                            groups.append(g)
            n_sec, err = call(m.get_num_sections, 0)
            arr = m.get_editor_property("static_materials") or []
            slots = [str(b.get_editor_property("material_slot_name")) for b in arr]
            tris, _ = call(m.get_num_triangles, 0)
            flag = ""
            if n_sec is not None and int(n_sec) != len(groups):
                flag = "  *** %d sections but %d usemtl groups ***" % (
                    int(n_sec), len(groups))
                bad += 1
            if "grass" not in slots:
                flag += "  *** no grass slot ***"
                bad += 1
            say("  %-22s sections=%s groups=%d slots=%s tris=%s%s"
                % (name, n_sec, len(groups), slots, tris, flag))
        say("terrain tiles with a mismatch: %d / %d" % (bad, len(tman["tiles"])))

        say("--- done ---")
    except Exception:
        say("FAILED:\n" + traceback.format_exc())
        say("--- done ---")


main()
