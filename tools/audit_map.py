# -*- coding: utf-8 -*-
"""
audit_map.py -- count what is actually in the level, before packaging.

Task ENG-PKG1 asks for four product counts "reported as actual numbers", not
as "confirmed". A count taken from a manifest is a count of what a tool
*wrote*; a count taken from the .umap is a count of what the level *has*. Those
two disagree routinely -- an asset can exist on disk and never be placed, or be
placed and never be cooked -- and the failure mode is silent in both cases.

So this reads the map itself and prints the actor inventory grouped by class
and by label prefix. It is a read-only audit: it places nothing, deletes
nothing, and saves nothing.

Run inside the editor:
    UnrealEditor-Cmd.exe project/MCReplica.uproject \
        -ExecutePythonScript=tools/audit_map.py -unattended -nopause -nosplash
"""

import json
import re
from collections import Counter

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/audit_map.txt"
JSON_OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/audit_map.json"
MAP = "/Game/Maps/MCReplica"

_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[AUDIT] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    world = unreal.EditorLoadingAndSavingUtils.load_map(MAP)
    if world is None:
        say("FATAL: could not load %s" % MAP)
        return
    actors = unreal.get_editor_subsystem(
        unreal.EditorActorSubsystem).get_all_level_actors()
    say("map: %s" % MAP)
    say("total level actors: %d" % len(actors))

    by_class = Counter(a.get_class().get_name() for a in actors)
    say("")
    say("--- by class ---")
    for name, n in by_class.most_common():
        say("  %-34s %d" % (name, n))

    labels = [a.get_actor_label() for a in actors]
    pref = Counter()
    for lb in labels:
        m = re.match(r"^([A-Za-z]+)_", lb)
        pref[m.group(1) if m else "(none)"] += 1
    say("")
    say("--- by label prefix ---")
    for name, n in pref.most_common():
        say("  %-24s %d" % (name, n))

    terrain = [a for a in actors if a.get_actor_label().startswith("T_terrain_")]
    bld = [a for a in actors if a.get_actor_label().startswith("B_")]
    say("")
    say("T_terrain_* actors: %d" % len(terrain))
    say("B_* actors:         %d" % len(bld))

    # Per-actor mesh sanity: a placed actor with no mesh is invisible geometry.
    #
    # `StaticMeshComponent` has **no** `get_static_mesh()` in UE 5.8's Python --
    # verified by tools/probe_actor_mesh.py; the mesh is the `static_mesh`
    # editor property. Calling the method name raised for all 182 actors, which
    # reads as "the whole level has no geometry" -- precisely the false alarm
    # that makes a real problem get dismissed later.
    empty = []
    tris = 0
    verts = 0
    for a in terrain + bld:
        label = a.get_actor_label()
        mesh = None
        try:
            # A component *class*, not an instance method: passing the instance
            # raises, which is why the first run of this audit reported all 182
            # actors as "no static mesh" -- a broken check that reads exactly
            # like a catastrophic content problem.
            comp = a.get_component_by_class(unreal.StaticMeshComponent)
            mesh = comp.get_editor_property("static_mesh") if comp else None
        except Exception as exc:
            empty.append("%s (%s)" % (label, str(exc)[:50]))
            continue
        if mesh is None:
            empty.append(label)
            continue
        try:
            tris += mesh.get_num_triangles(0)
        except Exception:
            pass
    say("actors with no static mesh: %d" % len(empty))
    for lb in empty[:10]:
        say("  EMPTY %s" % lb)
    say("total triangles across terrain+structure actors: %d" % tris)

    lights = [a for a in actors
              if "Light" in a.get_class().get_name()
              or "Sky" in a.get_class().get_name()
              or "Sun" in a.get_actor_label()
              or a.get_actor_label() == "MC_Sun"]
    say("")
    say("--- lights / sky ---")
    for a in lights:
        say("  %-28s %s" % (a.get_actor_label(), a.get_class().get_name()))
        for prop in ("intensity", "mobility"):
            try:
                comps = a.get_components_by_class(unreal.LightComponent)
                if comps:
                    say("      %s = %s" % (prop, comps[0].get_editor_property(prop)))
            except Exception:
                pass
        try:
            comps = a.get_components_by_class(unreal.DirectionalLightComponent)
            if comps:
                say("      cast_shadows = %s"
                    % comps[0].get_editor_property("cast_shadows"))
        except Exception:
            pass

    report = {
        "map": MAP,
        "total_actors": len(actors),
        "by_class": dict(by_class),
        "by_label_prefix": dict(pref),
        "terrain_actors": len(terrain),
        "building_actors": len(bld),
        "actors_without_mesh": empty,
        "triangles_terrain_plus_structures": tris,
        "lights": [a.get_actor_label() for a in lights],
    }
    with open(JSON_OUT, "w") as fh:
        json.dump(report, fh, indent=2)
    say("")
    say("wrote %s" % JSON_OUT)
    say("--- audit done (read-only; nothing placed, deleted or saved) ---")


if __name__ == "__main__":
    main()