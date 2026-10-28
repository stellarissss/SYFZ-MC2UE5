# -*- coding: utf-8 -*-
"""How many material slots does each mesh have, and what is on EACH one?

The importer creates one slot per `usemtl` group. My earlier material fix only
touched slot 0, so slots 1..N were never inspected -- and a mesh whose slot 0 is
the atlas but whose remaining slots are the engine default would render the
engine default on most of its faces.
"""
import traceback
from collections import Counter
import unreal
OUT="G:/BaiduNetdiskDownload/real/MC2UE5/logs/probe_slots.txt"; L=[]
def say(s): L.append(str(s)); open(OUT,"w").write("\n".join(L)+"\n")
try:
    w=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    per=Counter()
    shown=0
    for a in unreal.GameplayStatics.get_all_actors_of_class(w, unreal.StaticMeshActor):
        lbl=a.get_actor_label()
        if not (lbl.startswith("B_") or lbl.startswith("T_")): continue
        sc=a.static_mesh_component()
        if sc is None: continue
        sm=sc.get_editor_property("static_mesh") if hasattr(sc,"get_editor_property") else None
        n = sc.get_num_materials() if hasattr(sc,"get_num_materials") else -1
        mats=[]
        try:
            for i in range(n):
                m=sc.get_material(i)
                mats.append(m.get_name() if m else None)
        except Exception as e:
            mats=["ERR %s" % str(e)[:40]]
        per[(lbl[:2], n)] += 1
        if shown < 4:
            say("%s  slots=%d  %s" % (lbl, n, mats))
            shown += 1
    say("")
    say("按 (前缀, 槽数) 统计:")
    for (p,n),c in sorted(per.items()):
        say("   %s  槽数=%-3d  %d 个 mesh" % (p,n,c))
except Exception: say("FAILED:/n"+traceback.format_exc())
say("--- done ---")
