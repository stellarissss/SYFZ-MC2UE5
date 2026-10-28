# -*- coding: utf-8 -*-
"""
set_mesh_asset_materials.py -- write the atlas into every StaticMesh asset's
material slot array, and prove it by reading the assets back from disk.

Why the ASSET and not the component: component-level `set_material(i, ...)`
reported success, saved with the level, and changed nothing on screen. A
UV-visualisation material pushed through the same path rendered identically to
the atlas material, which is impossible if it were actually in use -- so the
draw path reads the asset's own slots, not the component override.

Why not StaticMeshEditorSubsystem: it has no `set_material` on UE 5.8 (verified
by calling it; every call raised AttributeError). The slots live in the asset's
`static_materials` array, which is a plain UPROPERTY and can simply be written.

The failure this replaces: `bld_001_structure` has 13 slots of which only slot 0
carried the atlas and the other 12 carried `WorldGridMaterial`, the engine
default. Fourteen of fifteen visible families therefore rendered as engine grey
mixed into one flat tone, with no error anywhere. That is the "everything is one
colour" symptom this project has been chasing.

Junk assets under the same folder (GridTest_*, Probe_*, UVTest_*,
C_overworld_*_collision) are DELETED here rather than textured: they are
leftovers from earlier probe scripts and would otherwise ship.
"""
import traceback

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/mesh_asset_materials.txt"
ATLAS = "/Game/MC/Atlas/M_MC_Atlas"
DIRS = ("/Game/MC/Structures", "/Game/MC/Terrain")
JUNK_PREFIX = ("GridTest_", "Probe_", "UVTest_", "C_overworld_")
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCASSET] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def slot_names(mesh):
    try:
        arr = mesh.get_editor_property("static_materials")
    except Exception:
        return []
    out = []
    for b in arr or []:
        try:
            mi = b.get_editor_property("material_interface")
            out.append(mi.get_name() if mi else None)
        except Exception:
            out.append("<?>")
    return out


def set_all_slots(mesh, mat):
    """Replace every slot's material_interface. Returns the new slot count."""
    try:
        arr = mesh.get_editor_property("static_materials")
    except Exception:
        return -1
    n = len(arr) if arr else 0
    if n == 0:
        # A mesh with no slots needs one before it can be textured at all.
        n = 1
    new = []
    for _ in range(n):
        s = unreal.StaticMaterial()
        s.set_editor_property("material_interface", mat)
        new.append(s)
    mesh.set_editor_property("static_materials", new)
    return n


def main():
    ok = True
    try:
        atlas = unreal.load_asset(ATLAS)
        say("atlas = %s" % (atlas.get_name() if atlas else "*** MISSING ***"))
        if atlas is None:
            return

        for d in DIRS:
            paths = unreal.EditorAssetLibrary.list_assets(d, recursive=False,
                                                          include_folder=False)
            meshes = assigned = deleted = slots_total = 0
            for p in paths:
                name = p.split("/")[-1].split(".")[0]
                if name.startswith(JUNK_PREFIX):
                    if unreal.EditorAssetLibrary.delete_asset(p):
                        deleted += 1
                    continue
                a = unreal.load_asset(p)
                if a is None or a.get_class().get_name() != "StaticMesh":
                    continue
                meshes += 1
                n = set_all_slots(a, atlas)
                if n < 0:
                    say("  static_materials 写不了: %s" % p)
                    ok = False
                    continue
                slots_total += n
                assigned += 1
                unreal.EditorAssetLibrary.save_loaded_asset(a)
            say("%-24s 网格=%-4d 槽位=%-5d 删除遗留=%-3d"
                % (d, meshes, slots_total, deleted))
            if meshes == 0 or assigned == 0:
                say("*** %s 无网格被处理 —— 失败" % d)
                ok = False

        # Read back from disk. The whole point: the previous attempt reported
        # success while changing nothing, so the claim has to come from a fresh
        # load of the saved asset.
        for probe in ("/Game/MC/Structures/bld_001_structure",
                      "/Game/MC/Structures/bld_001_detail",
                      "/Game/MC/Structures/bld_028_structure",
                      "/Game/MC/Terrain/terrain_-144_-544"):
            m = unreal.load_asset(probe)
            if not m:
                say("复核 %-34s *** MISSING ***" % probe.split("/")[-1])
                ok = False
                continue
            names = slot_names(m)
            good = bool(names) and all(x == "M_MC_Atlas" for x in names)
            say("复核 %-34s slots=%-3d %s"
                % (probe.split("/")[-1], len(names),
                   "全部=图集" if good else "*** %s ***" % names[:5]))
            if not good:
                ok = False
    except Exception:
        ok = False
        say("FAILED:/n" + traceback.format_exc())
    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
