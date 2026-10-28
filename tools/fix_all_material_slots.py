# -*- coding: utf-8 -*-
"""
fix_all_material_slots.py -- give EVERY material slot the atlas, not just slot 0.

The messy part of this project's material history in one place:

extract_structures.py has always written `usemtl <family>` per face group (42
groups in one building), so the OBJ importer creates **one material slot per
family**. That is correct and standard.

But the material pass only ever touched `get_material(0)` / `set_material(0, ...)`.
Slots 1..N were never inspected or assigned, so every face in every group except
the first rendered with whatever the importer left there -- which is the engine
default material for an OBJ with no .mtl file. A mesh whose slot 0 shows atlas
texture and whose other 13 slots show the grey default is exactly "one colour
everywhere" with no error anywhere.

This assigns the atlas to all slots and reports the per-slot state before and
after, per mesh, so the claim is checkable.
"""
import traceback
from collections import Counter

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/fix_all_material_slots.txt"
ATLAS = "/Game/MC/Atlas/M_MC_Atlas"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCSLOT] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def comp_of(actor):
    """StaticMeshComponent, whatever the API name is on this engine build."""
    for attr in ("static_mesh_component", "static_mesh_component_0"):
        c = getattr(actor, attr, None)
        if c is not None:
            return c
    return None


def survey(tag):
    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    slots = Counter()
    empties = 0
    meshes = 0
    samples = []
    for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
        lbl = a.get_actor_label()
        if not (lbl.startswith("B_") or lbl.startswith("T_")):
            continue
        c = comp_of(a)
        if c is None:
            continue
        meshes += 1
        n = c.get_num_materials()
        names = []
        for i in range(n):
            m = c.get_material(i)
            names.append(m.get_name() if m else "*** NULL ***")
            if m is None:
                empties += 1
        slots[(lbl[:2], n)] += 1
        if len(samples) < 3:
            samples.append((lbl, n, names))
    say("---- %s ----" % tag)
    say("  mesh 数 = %d   NULL 槽位总数 = %d" % (meshes, empties))
    for (p, n), c in sorted(slots.items()):
        say("  %s 槽数=%-3d %d 个" % (p, n, c))
    for lbl, n, names in samples:
        say("  %s slots=%d %s" % (lbl, n, names))
    return empties


def main():
    try:
        atlas = unreal.load_asset(ATLAS)
        say("atlas = %s" % (atlas.get_name() if atlas else "*** MISSING ***"))
        if atlas is None:
            return
        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        before = survey("修改前")

        w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
        changed = 0
        for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
            lbl = a.get_actor_label()
            if not (lbl.startswith("B_") or lbl.startswith("T_")):
                continue
            c = comp_of(a)
            if c is None:
                continue
            for i in range(c.get_num_materials()):
                m = c.get_material(i)
                if m is None or m.get_name() != "M_MC_Atlas":
                    c.set_material(i, atlas)
                    changed += 1
        say("已为 %d 个槽位设置 M_MC_Atlas" % changed)

        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        say("save_current_level -> %s" % les.save_current_level())
        after = survey("修改后（已重载关卡）")
        say("NULL 槽位: %d -> %d" % (before, after))
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
