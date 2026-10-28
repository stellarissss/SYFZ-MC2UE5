# -*- coding: utf-8 -*-
"""
probe_actor_mesh.py -- how do you actually read a placed actor's mesh?

`audit_map.py` reported all 182 terrain/structure actors as having "no static
mesh" twice, for two different reasons, and neither was a content problem:

  1. `actor.get_components_by_class(unreal.StaticMeshComponent)` -- passed the
     class and got something whose components have no `get_static_mesh`.
  2. The retry used `a.get_components_by_class(...)` and then
     `comps[0].get_static_mesh()`, and the error text shows the thing being
     subscripted is the *class*, so `get_components_by_class` is not returning a
     component list here.

A check that reports "every actor is broken" when nothing is wrong is worse than
no check, because it cries wolf and gets ignored. So find the accessor that
works before believing any number.

Run: UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/probe_actor_mesh.py -unattended -nopause
"""

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/probe_actor_mesh.txt"
_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[PROBE5] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    world = unreal.EditorLoadingAndSavingUtils.load_map("/Game/Maps/MCReplica")
    actors = unreal.get_editor_subsystem(
        unreal.EditorActorSubsystem).get_all_level_actors()
    target = None
    for a in actors:
        if a.get_actor_label().startswith("T_terrain_"):
            target = a
            break
    if target is None:
        say("FATAL: no T_terrain_ actor")
        return
    say("actor: %s  class=%s" % (target.get_actor_label(),
                                 target.get_class().get_name()))

    # The direct, obvious route.
    for label, fn in (
            ("a.static_mesh_component",
             lambda: target.static_mesh_component),
            ("a.get_component_by_class(StaticMeshComponent)",
             lambda: target.get_component_by_class(
                 unreal.StaticMeshComponent)),
            ("a.get_components_by_class(StaticMeshComponent)",
             lambda: target.get_components_by_class(
                 unreal.StaticMeshComponent)),
    ):
        try:
            v = fn()
            say("OK   %s -> %r" % (label, v))
        except Exception as exc:
            say("FAIL %s -> %s" % (label, str(exc)[:140]))

    # If a component came back, walk it properly.
    try:
        comp = target.get_component_by_class(unreal.StaticMeshComponent)
    except Exception:
        comp = None
    if comp is not None:
        for label, fn in (
                ("comp.get_static_mesh()", lambda: comp.get_static_mesh()),
                ("comp.static_mesh", lambda: comp.get_editor_property(
                    "static_mesh")),
        ):
            try:
                m = fn()
                say("OK   %s -> %r" % (label, m))
                if m is not None and hasattr(m, "get_num_triangles"):
                    say("     triangles(0) = %s" % m.get_num_triangles(0))
            except Exception as exc:
                say("FAIL %s -> %s" % (label, str(exc)[:140]))

    # Aggregate over all terrain actors with whichever route works.
    ok = 0
    tris = 0
    none = 0
    for a in actors:
        lb = a.get_actor_label()
        if not (lb.startswith("T_terrain_") or lb.startswith("B_")):
            continue
        try:
            c = a.get_component_by_class(unreal.StaticMeshComponent)
            m = c.get_static_mesh() if c else None
        except Exception:
            m = None
        if m is None:
            none += 1
        else:
            ok += 1
            try:
                tris += m.get_num_triangles(0)
            except Exception:
                pass
    say("")
    say("aggregate: with_mesh=%d without_mesh=%d triangles=%d"
        % (ok, none, tris))
    say("--- probe done ---")


if __name__ == "__main__":
    main()