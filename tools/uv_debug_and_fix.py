# -*- coding: utf-8 -*-
"""
uv_debug_and_fix.py -- show the UVs the engine actually has, then restore.

Every input to the material has now been measured and found correct: the atlas
PNG is the baked one, the sampler is bound, the OBJ's UVs sit inside the right
cell per family (du = 504/4096, dv = 504/2048), and all 13 slots now carry the
atlas material. Yet the render is one uniform cream.

Uniform colour from a texture sampler means one thing: **the sampled coordinate
is the same everywhere**. So the remaining question is not "is the data right"
but "what UV does the renderer actually see". This answers it by colouring the
surface with the UV itself -- red = u, green = v.

If the picture shows 8 distinct bands the UVs are atlas-space and the fault is
elsewhere; if it shows a smooth red/green ramp per face then the UVs are
per-face 0..1 and the mesh never carried the atlas addressing.

The atlas material is restored at the end either way.
"""
import traceback

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/uv_debug.txt"
DEBUG = "/Game/MC/Atlas/M_MC_UVDebug"
ATLAS = "/Game/MC/Atlas/M_MC_Atlas"
MAP = "/Game/Maps/MCReplica"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCUVDBG] " + str(s))
    open(OUT, "w").write("\n".join(L) + "\n")


def comp_of(a):
    for attr in ("static_mesh_component", "static_mesh_component_0"):
        c = getattr(a, attr, None)
        if c is not None:
            return c
    return None


def targets(w):
    for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
        lbl = a.get_actor_label()
        if lbl.startswith("B_") or lbl.startswith("T_"):
            c = comp_of(a)
            if c is not None:
                yield c


def build_debug():
    mel = unreal.MaterialEditingLibrary
    if unreal.EditorAssetLibrary.does_asset_exist(DEBUG):
        m = unreal.load_asset(DEBUG)
        for e in mel.get_material_expressions(m):
            mel.delete_material_expression(m, e)
        for p in (unreal.MaterialProperty.MP_BASE_COLOR,):
            mel.disconnect_material_property(m, p)
    else:
        m = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_MC_UVDebug", "/Game/MC/Atlas", unreal.Material,
            unreal.MaterialFactoryNew())
    uv = mel.create_material_expression(m, unreal.MaterialExpressionTextureCoordinate, -400, 0)
    uv.set_editor_property("coordinate_index", 0)
    mel.connect_material_property(uv, "", unreal.MaterialProperty.MP_BASE_COLOR)
    mel.recompile_material(m)
    unreal.EditorAssetLibrary.save_loaded_asset(m)
    return m


def assign(mat, count_only=False):
    w = unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    n = 0
    for c in targets(w):
        for i in range(c.get_num_materials()):
            c.set_material(i, mat)
            n += 1
    return n


def main():
    try:
        mode = "DEBUG"
        # argv 风格：本脚本被 python 直接执行，用环境变量区分阶段
        import os
        mode = os.environ.get("MCUV_MODE", "DEBUG")
        say("mode = %s" % mode)

        unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        if mode == "DEBUG":
            m = build_debug()
            say("debug material ready: %s" % m.get_name())
            n = assign(m)
            say("UV-debug 已应用到 %d 个槽位 —— 现在抓帧" % n)
        else:
            atlas = unreal.load_asset(ATLAS)
            n = assign(atlas)
            say("已恢复图集到 %d 个槽位" % n)
        say("save -> %s" % unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level())
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")


if __name__ == "__main__":
    main()
