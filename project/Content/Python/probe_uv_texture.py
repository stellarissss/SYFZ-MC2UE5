# -*- coding: utf-8 -*-
"""
Two measurements that decide the "everything is one colour" question, in the
order of how cheaply they can be falsified:

  A. Is the texture INSIDE UE the one that was baked? A stale .uasset holding the
     old warm atlas would produce exactly this symptom while every log stays green
     -- the risk quality-lead flagged.
  B. What UVs did the OBJ importer actually store? Deriving this from the source
     OBJ was wrong once already (the importer may re-index or weld).
"""
import os, time, traceback
import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/probe_uv_texture.txt"
L = []
def say(s):
    L.append(str(s)); unreal.log("[MCUVT] " + str(s)); open(OUT, "w").write("\n".join(L) + "\n")

def main():
    try:
        # ---------- A. texture freshness ----------
        tex = unreal.load_asset("/Game/MC/Atlas/T_MC_Atlas")
        say("A. T_MC_Atlas = %s" % (tex.get_name() if tex else "*** MISSING ***"))
        if tex:
            for p in ("source_file", "asset_import_data", "srgb"):
                try: say("   %-14s %s" % (p, str(tex.get_editor_property(p))[:110]))
                except Exception as e: say("   %-14s <无此属性>" % p)
            try:
                say("   imported_size  %s" % tex.blueprint_get_size_x() if hasattr(tex,"blueprint_get_size_x") else "")
            except Exception: pass
            for p in ("get_platform_data"):
                pass
        for p in ("out/atlas/atlas_diffuse.png",
                  "project/Content/MC/Atlas/atlas_diffuse.png"):
            fp = "G:/BaiduNetdiskDownload/real/MC2UE5/repo/" + p
            if os.path.exists(fp):
                st = os.stat(fp)
                say("   PNG %-46s mtime=%s size=%d" % (p, time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)), st.st_size))
        ua = "G:/BaiduNetdiskDownload/real/MC2UE5/repo/project/Content/MC/Atlas/T_MC_Atlas.uasset"
        if os.path.exists(ua):
            st = os.stat(ua)
            say("   UASSET %-43s mtime=%s size=%d" % ("T_MC_Atlas.uasset", time.strftime("%m-%d %H:%M:%S", time.localtime(st.st_mtime)), st.st_size))

        # ---------- B. stored UVs ----------
        say("")
        say("B. 导入后网格的 UV0 实测")
        for path in ("/Game/MC/Structures/bld_002_structure",
                     "/Game/MC/Terrain/terrain_-144_-544"):
            sm = unreal.load_asset(path)
            if not sm: say("   %s MISSING" % path); continue
            say("   %s" % path)
            try:
                s = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
                n = s.get_number_uvs(sm) if hasattr(s, "get_number_uvs") else None
                say("     StaticMeshEditorSubsystem.get_number_uvs = %s" % n)
            except Exception as e:
                say("     get_number_uvs ERR %s" % str(e)[:70])
            # 顶点位置 + UV 可从 LOD0 的渲染数据里数出来
            try:
                say("     num_lods=%s" % sm.get_num_lods())
                say("     num_verts(lod0)=%s" % sm.get_num_vertices(0))
                say("     num_tris(lod0)=%s" % sm.get_num_triangles(0))
                say("     bounds origin=%s extent=%s"
                    % (sm.get_bounds().origin, sm.get_bounds().box_extent))
            except Exception as e:
                say("     统计 ERR %s" % str(e)[:70])
    except Exception:
        say("FAILED:/n" + traceback.format_exc())
    say("--- done ---")

main()
