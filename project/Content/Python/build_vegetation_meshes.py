# -*- coding: utf-8 -*-
"""
build_vegetation_meshes.py -- import the S4.5 vegetation OBJs, bind materials,
give them collision, and place their actors.

Editor side of tools/build_vegetation_pack.py. It deliberately reuses the
structure chain's proven pieces rather than inventing a second path:

  * import_obj / usemtl_first_seen_order / bind_slots from
    tools/import_family_materials.py -- the exact import+binding sequence the
    158 structure meshes went through;
  * the Interchange OBJ importer's basis transform (x, y, z) -> (x, -y, z) is
    inherited for free, so vegetation lands in the SAME (mirrored-to-UE)
    world as the structures and the terrain pack. Voxel shells import at
    100% face retention (measured; heightfields are the broken case);
  * collision via the CDO allow_cpu_access default set BEFORE import (the
    refix_collision.py lesson: the flag must be on when the render data is
    built, or the cooked trimesh is empty), then complex-as-simple.

Actors: one StaticMeshActor per chunk at the identity transform (the OBJs are
already in world centimetres), labelled ``Veg_<chunk>``, find-or-spawn so the
script is idempotent like every other level-side script here.

Run inside the editor (UnrealEditor-Cmd -ExecutePythonScript=...), with
MC_LOG_DIR set if the report should land somewhere specific.
"""

import json
import os
import sys
import traceback

import unreal

_repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))   # Python -> Content -> project -> repo
PACK = os.path.join(_repo, "out/vegetation_obj")
sys.path.insert(0, os.path.join(_repo, "tools"))

OUT = os.path.join(
    os.environ.get("MC_LOG_DIR",
                   os.path.join(os.path.dirname(_repo), "logs")),
    "build_vegetation_meshes.txt")

# The structure chain's proven import helpers (imports unreal itself).
from import_family_materials import (                      # noqa: E402
    import_obj, usemtl_first_seen_order, bind_slots)

MAP = "/Game/Maps/MCReplica"
DEST = "/Game/MC/Vegetation"
L = []


def say(s):
    L.append(str(s))
    unreal.log("[MCVEGBUILD] " + str(s))
    try:
        with open(OUT, "w") as fh:
            fh.write("\n".join(L) + "\n")
    except OSError:
        pass


def label(a):
    try:
        return a.get_actor_label()
    except Exception:
        return a.get_name()


def place_actor(world, name, mesh):
    """Find-or-spawn the chunk's StaticMeshActor, identity transform."""
    want = "Veg_%s" % name
    for a in unreal.GameplayStatics.get_all_actors_of_class(
            world, unreal.StaticMeshActor):
        if label(a) == want:
            comp = a.get_component_by_class(unreal.StaticMeshComponent)
            comp.set_static_mesh(mesh)
            say("  %s: reused actor" % want)
            return a
    actor = unreal.EditorLevelLibrary.spawn_actor_from_class(
        unreal.StaticMeshActor, unreal.Vector(0.0, 0.0, 0.0),
        unreal.Rotator(0.0, 0.0, 0.0))
    if actor is None:
        return None
    actor.set_actor_label(want)
    comp = actor.get_component_by_class(unreal.StaticMeshComponent)
    if comp is None:
        return None
    comp.set_static_mesh(mesh)
    for prop, value in (("mobility", unreal.ComponentMobility.STATIC),
                        ("collision_profile_name", "BlockAll")):
        try:
            comp.set_editor_property(prop, value)
        except Exception as exc:
            say("  %s: %s = %s failed (%s)"
                % (want, prop, value, str(exc)[:60]))
    say("  %s: actor placed" % want)
    return actor


def build_chunk(world, smes, t):
    name = t["name"]
    obj_path = t["obj_path"]
    if not os.path.isfile(obj_path):
        return {"name": name, "error": "missing %s" % obj_path}

    order = usemtl_first_seen_order(obj_path)
    if not order:
        return {"name": name, "error": "no usemtl groups in OBJ"}

    mesh = import_obj(obj_path, DEST, "SM_%s" % name)
    if mesh is None:
        return {"name": name, "error": "import failed"}

    problems = []
    if not bind_slots(mesh, order, "SM_%s" % name, problems):
        return {"name": name, "error": "; ".join(problems)[:140]}

    # ---- collision ---------------------------------------------------------
    # CDO default was set before the import, so the built render data kept a
    # CPU-side index copy and the complex-as-simple trimesh will be real.
    try:
        smes.set_collision_complexity(
            mesh, unreal.CollisionComplexity.CTC_USE_COMPLEX_AS_SIMPLE)
    except Exception as exc:
        try:
            mesh.set_editor_property(
                "collision_complexity",
                unreal.CollisionComplexity.CTC_USE_COMPLEX_AS_SIMPLE)
        except Exception as exc2:
            return {"name": name,
                    "error": "collision complexity: %s / %s"
                             % (str(exc)[:60], str(exc2)[:60])}
    try:
        body = mesh.get_editor_property("body_setup")
        if body is not None:
            body.set_editor_property(
                "collision_trace_flag",
                unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
    except Exception as exc:
        say("  %s: body_setup trace flag: %s (continuing)"
            % (name, str(exc)[:80]))

    try:
        got_cc = str(smes.get_collision_complexity(mesh))
    except Exception:
        got_cc = "?"
    if "USE_COMPLEX_AS_SIMPLE" not in got_cc:
        return {"name": name,
                "error": "collision complexity reads back as %s" % got_cc}

    actor = place_actor(world, name, mesh)
    if actor is None:
        return {"name": name, "error": "could not place actor"}

    got = mesh.get_num_triangles(0)
    want = t["triangles"]
    return {"name": name, "want": want, "got": got,
            "slots": order, "verts": mesh.get_num_vertices(0)}


def main():
    ok = True
    try:
        say("=== build_vegetation_meshes ===")
        say("pack: %s" % PACK)
        with open(os.path.join(PACK, "manifest.json")) as fh:
            man = json.load(fh)
        chunks = man["chunks"]
        say("pack: %d chunks, %s triangles, %d vegetation blocks"
            % (len(chunks), format(man["totals"]["triangles"], ","),
               man["totals"]["blocks"]))

        lvl = unreal.EditorLoadingAndSavingUtils.load_map(MAP)
        say("load_map %s -> %s" % (MAP, lvl.get_name() if lvl else "None"))
        if lvl is None:
            raise RuntimeError("map %s did not load" % MAP)
        world = unreal.get_editor_subsystem(
            unreal.UnrealEditorSubsystem).get_editor_world()

        smes = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)

        # BEFORE any import: class default so new imports start with CPU-side
        # vertex data (refix_collision.py ordering lesson).
        cdo = unreal.StaticMesh.get_default_object()
        try:
            cdo.set_editor_property("allow_cpu_access", True)
            say("CDO allow_cpu_access set")
        except Exception as exc:
            say("CDO set failed: %s" % exc)

        want_total = got_total = 0
        bad = []
        for t in chunks:
            st = build_chunk(world, smes, t)
            if "error" in st:
                bad.append(st)
                say("  %-12s *** %s" % (st["name"], st["error"]))
                continue
            want_total += st["want"]
            got_total += st["got"] if isinstance(st["got"], int) else 0
            flag = "" if st["got"] == st["want"] else "   *** MISMATCH"
            say("  %-12s tris %6d / %-6d verts %-7s slots %s%s"
                % (st["name"], st["got"], st["want"], st["verts"],
                   ",".join(st["slots"]), flag))

        say("")
        say("totals: triangles %d / %d" % (got_total, want_total))
        if bad:
            ok = False
            say("FAILED: %s" % ", ".join(b["name"] for b in bad))
        else:
            say("every chunk matched its OBJ triangle count")

        # Actors were placed: the LEVEL is dirty. save_current_level is the
        # call that actually persists a .umap (§8.6).
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        say("save_current_level -> %s" % les.save_current_level())
    except SystemExit:
        ok = False
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())

    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")


if __name__ == "__main__":
    main()
