# -*- coding: utf-8 -*-
"""
retire_voxel_layer.py -- delete the retired voxel layer from MCReplica for good.

What the layer is, and why it is not just "hidden"
--------------------------------------------------
The level still carries 1041 `MCReplicaPropCluster` actors holding 1,171,144
instances of `Cube1x1x1` -- one cube per block of the pre-OBJ pipeline, with one
flat colour material per block family (`MI_grass`, `MI_soil`, ...). The visible
world has since moved to 189 `StaticMeshActor`s (`bld_*_structure`,
`bld_*_detail`, `terrain_*`) carrying the per-block 16x16 textures with
TF_NEAREST. So the cube layer is dead weight: it is not what a frame shows.

`tools/hide_voxel_layer.py` tried to retire it by hiding it. That is not enough,
for three measured reasons:

  * **The hide did not survive the save.** That script's own log ends
    `after reload: 1279 actors, 0 MCReplicaPropCluster hidden` -- `-unattended`
    edits do not reliably mark actors dirty for the level save, so the flag was
    written into a session and never into the file. Anything that relies on the
    hidden state (a packaged build, a fresh editor, the collision it was told to
    drop) is relying on a value that is not there.
  * **Hidden geometry still pays the cost that matters.** `DefaultEngine.ini`
    pins `r.GenerateMeshDistanceFields=False` and `r.Lumen.TraceMeshSDFs=0`
    with the comment "1.17M ISM instances made distance-field generation hang on
    the loading screen -- re-enable only after the legacy voxel layer is
    retired". Hidden instances are still instances: they are in the level, they
    are built into the primitives on load, and they are what the comment is
    waiting for. The measured consequence of leaving that switch off is that
    Lumen indirect diffuse is exactly 0 (GI method 1 vs 0 changes the frame mean
    by 0.0), which is why the sun's fill has to be faked with `MC_BounceLight`
    and a warm-white SkyLight. Deleting the layer is the precondition for
    turning distance fields back on.
  * **A hidden layer is a trap for the next reader.** The level census in
    `logs/ortho_topdown.txt` reads `actors: 182`, because the capture tool takes
    the clusters out of its own frame; the level actually holds 1279 actors.
    Two different numbers for "how much is in this level" is how the earlier
    round concluded a layer was missing when it was merely hidden.

So this script deletes them. It is safe to do so only because the layer is
**inert** -- proven by measurement, not by looking: `out/persp_bounce.png` (the
ground-level before frame) and `out/persp_after_retire.png` (the same camera,
same tool, after the delete) agree to within the tolerance in step 4 of this
round's instructions, i.e. under ~2/255 mean absolute difference and under 3% of
pixels off by more than 8. If they had disagreed the backup map would have gone
back.

Undo
----
The level is generated, not hand-authored: `tools/build_world.py` rebuilds it
from `voxel_data/` (`--only materials|terrain|vegetation|meshes|lighting|spawn|
hide|census`). The 164 MB map asset was also copied to
`G:\\BaiduNetdiskDownload\\real\\MC2UE5\\backup_umap\\<timestamp>\\` before this
script first ran, so the pre-delete file exists verbatim. Deleting the layer
does not remove any source data -- `voxel_data/` is untouched.

Order of operations (a failure before the delete leaves the level as it was)
---------------------------------------------------------------------------
  1. census: clusters, instances, actor classes, and how many clusters actually
     report hidden (this is what answers "were the hides real?");
  2. pre-flight gate: every `terrain_*` StaticMeshActor must have
     USE_COMPLEX_AS_SIMPLE collision, because deleting the cube layer removes
     its collision too and the player would otherwise fall through the world;
  3. delete with `EditorActorSubsystem.destroy_actor`;
  4. hard-assert the survivors (handle count unchanged, one PlayerStart, the
     sun, the bounce light, the sky light, the post-process volume);
  5. `save_current_level()`, then RELOAD FROM DISK and re-assert -- the point of
     this script is that the removal is in the file, so it is verified from the
     file, not from the session that made the change.

Run (editor-side; nothing is drawn, so `-nullrhi` is fine):

    UnrealEditor-Cmd.exe G:/.../project/MCReplica.uproject \
        -ExecutePythonScript=<repo>/tools/retire_voxel_layer.py \
        -nullrhi -unattended -nopause -nosplash -stdout

Knobs (all optional):
    MC_LOG_DIR       directory for retire_voxel_layer.txt (joined, not used raw)
    MC_RETIRE_DRY    1 = census and gate only, delete nothing (default 0)
"""

import os
import sys
import time
import traceback

import unreal

# MC_LOG_DIR is a DIRECTORY; join the filename (see build_terrain_meshes.py).
LOG_DIR = os.environ.get("MC_LOG_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "logs")
OUT = os.path.join(LOG_DIR, "retire_voxel_layer.txt")
MAP = "/Game/Maps/MCReplica"

CLUSTER_CLASS = "MCReplicaPropCluster"
#: Measured in logs/ortho_topdown.txt (2026-11-02). Not a target -- a check that
#: the visible world is the one this script thinks it is leaving alone.
EXPECT_STATIC_MESH_ACTORS = 189
SUN_LABEL = "MC_Sun"
BOUNCE_LABEL = "MC_BounceLight"
PPV_LABEL = "MC_PostProcess"

_L = []
_FAILED = []


def say(msg):
    _L.append(str(msg))
    unreal.log("[MCRetire] " + str(msg))
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(OUT, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_L) + "\n")
    except OSError:
        pass


def fail(msg):
    _FAILED.append(str(msg))
    say("  FAIL %s" % msg)


def label(a):
    try:
        return a.get_actor_label()
    except Exception:                                  # noqa: BLE001
        return a.get_name()


def census(world):
    """Actors by class name, as a dict -- cheap, and it is what the log needs."""
    by_class = {}
    for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        cls = a.get_class().get_name()
        by_class[cls] = by_class.get(cls, 0) + 1
    return by_class


def actor_names(world, cls_name):
    """All actors of a class, selected by NAME rather than by a binding that may
    be missing from this build."""
    out = []
    for a in unreal.GameplayStatics.get_all_actors_of_class(world, unreal.Actor):
        if a.get_class().get_name() == cls_name:
            out.append(a)
    return out


_INSTANCE_SOURCE = [None]


def instance_count(actor):
    """Instances held by one cluster. Returns (count, how-it-was-read).

    Two candidate spellings exist in 5.8 and only one of them may work:
    `AMCReplicaPropCluster::GetInstanceCount()` is a UFUNCTION on the actor
    (C++ header, public/MCReplicaPropCluster.h), while the HISM component it
    owns (`Instances`) also exposes `get_instance_count()`. Which one answers is
    reported once, because the earlier census scripts disagreed about whether
    the actor-level call existed on this build.
    """
    try:
        return int(actor.get_instance_count()), "actor.get_instance_count()"
    except Exception:                                  # noqa: BLE001
        pass
    comp = None
    try:
        comp = actor.get_component_by_class(unreal.InstancedStaticMeshComponent)
    except Exception:                                  # noqa: BLE001
        comp = None
    if comp is None:
        try:
            comp = actor.get_editor_property("instances")
        except Exception:                              # noqa: BLE001
            comp = None
    if comp is not None:
        try:
            return int(comp.get_instance_count()), "InstancedStaticMeshComponent.get_instance_count()"
        except Exception:                              # noqa: BLE001
            pass
    return -1, "<unreadable>"


def count_instances(clusters):
    total = 0
    unreadable = 0
    for a in clusters:
        n, how = instance_count(a)
        if _INSTANCE_SOURCE[0] is None:
            _INSTANCE_SOURCE[0] = how
        if n < 0:
            unreadable += 1
        else:
            total += n
    return total, unreadable


def count_hidden(clusters):
    """How many clusters a frame would actually skip, by either spelling.

    This is the number `logs/hide_voxel_layer.txt` promised and did not deliver
    (it reported 0 after a reload), so it is measured here before anything is
    deleted.
    """
    hidden = 0
    via = "none"
    for a in clusters:
        got = None
        try:
            got = bool(a.is_actor_hidden_in_game())
            via = "actor.is_actor_hidden_in_game()"
        except Exception:                              # noqa: BLE001
            got = None
        if got is None:
            comp = None
            try:
                comp = a.get_component_by_class(
                    unreal.InstancedStaticMeshComponent)
            except Exception:                          # noqa: BLE001
                comp = None
            if comp is not None:
                try:
                    got = not comp.is_visible()
                    via = "component.is_visible()==False"
                except Exception:                      # noqa: BLE001
                    got = None
        if got:
            hidden += 1
    return hidden, via


def collision_gate(world):
    """Every terrain tile must carry complex-as-simple collision.

    The cube layer is the ground as far as the pawn is concerned; once it is
    deleted the only floor left is the terrain. `hide_voxel_layer.py` already
    gated its collision removal on this same property, but it disabled the
    collision in a session whose save did not stick, so the property is re-read
    from the level that is about to lose its backup floor.
    """
    smes = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    ok = 0
    bad = []
    for a in actor_names(world, "StaticMeshActor"):
        lb = label(a)
        c = a.get_component_by_class(unreal.StaticMeshComponent)
        m = c.get_editor_property("static_mesh") if c is not None else None
        mname = m.get_name() if m else ""
        if not (lb.startswith("terrain_") or "terrain_" in mname):
            continue
        if m is None:
            bad.append("%s: no mesh" % lb)
            continue
        try:
            cc = str(smes.get_collision_complexity(m))
        except Exception:                              # noqa: BLE001
            try:
                cc = str(m.get_editor_property("collision_complexity"))
            except Exception as exc:                   # noqa: BLE001
                bad.append("%s: %s" % (lb, str(exc).splitlines()[0][:50]))
                continue
        if "USE_COMPLEX_AS_SIMPLE" in cc:
            ok += 1
        else:
            bad.append("%s: complexity=%s" % (lb, cc))
    say("collision gate: %d terrain tiles complex-as-simple, %d without"
        % (ok, len(bad)))
    for b in bad[:10]:
        say("  ! %s" % b)
    return ok, bad


def assert_survivors(world, expect_static_mesh, tag):
    """The things whose absence would mean this script deleted the wrong thing."""
    by_class = census(world)
    sma = by_class.get("StaticMeshActor", 0)
    if sma == expect_static_mesh:
        say("  OK   StaticMeshActor        %4d (unchanged)" % sma)
    else:
        fail("%s: StaticMeshActor count %d, expected %d -- the visible world "
             "changed" % (tag, sma, expect_static_mesh))

    starts = by_class.get("PlayerStart", 0)
    if starts == 1:
        say("  OK   PlayerStart            %4d" % starts)
    else:
        fail("%s: %d PlayerStart actors, expected 1" % (tag, starts))

    for cls_name, want in (("SkyLight", 1), ("PostProcessVolume", 1)):
        n = by_class.get(cls_name, 0)
        if n == want:
            say("  OK   %-20s %4d" % (cls_name, n))
        else:
            fail("%s: %d %s actors, expected %d" % (tag, n, cls_name, want))

    dls = actor_names(world, "DirectionalLight")
    by_label = {}
    for a in dls:
        by_label[label(a)] = by_label.get(label(a), 0) + 1
    for want_label in (SUN_LABEL, BOUNCE_LABEL):
        n = by_label.get(want_label, 0)
        if n == 1:
            say("  OK   DirectionalLight       %4d  labelled %s"
                % (by_label.get(want_label), want_label))
        else:
            fail("%s: %d DirectionalLight actors labelled %s, expected 1"
                 % (tag, n, want_label))
    say("       DirectionalLight total   %4d  labels: %s"
        % (len(dls), ", ".join("%s x%d" % (k, v)
                               for k, v in sorted(by_label.items()))))

    ppvs = actor_names(world, "PostProcessVolume")
    hit = [a for a in ppvs if label(a) == PPV_LABEL]
    if len(hit) == 1:
        say("  OK   PostProcessVolume      %4d  labelled %s"
            % (1, PPV_LABEL))
    else:
        fail("%s: %d PostProcessVolume actors labelled %s, expected 1"
             % (tag, len(hit), PPV_LABEL))

    left = by_class.get(CLUSTER_CLASS, 0)
    say("  %-4s %-20s %4d  (target 0)"
        % ("OK" if left == 0 else "FAIL", CLUSTER_CLASS, left))
    if left != 0:
        fail("%s: %d %s actors survived" % (tag, left, CLUSTER_CLASS))
    return by_class


def main():
    say("=== retire_voxel_layer start %s ===" % time.strftime("%F %T"))
    say("engine %s" % unreal.SystemLibrary.get_engine_version())
    dry = os.environ.get("MC_RETIRE_DRY", "").strip() in ("1", "true", "yes")
    say("mode: %s" % ("DRY (delete nothing)" if dry else "DELETE"))

    lvl = unreal.EditorLoadingAndSavingUtils.load_map(MAP)
    say("load_map(%s) -> %s" % (MAP, lvl.get_name() if lvl else "None"))
    if lvl is None:
        fail("map did not load")
        say("")
        say("RESULT: FAIL map did not load")
        return 1
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if world is None:
        fail("no editor world")
        say("")
        say("RESULT: FAIL no editor world")
        return 1

    # ---- 1. census ------------------------------------------------------- #
    say("")
    say("--- before ----------------------------------------------------------")
    before = census(world)
    say("actors: %d, classes: %d" % (sum(before.values()), len(before)))
    for cls in sorted(before, key=lambda c: -before[c])[:10]:
        say("  %-28s %5d" % (cls, before[cls]))
    n_static_before = before.get("StaticMeshActor", 0)

    clusters = actor_names(world, CLUSTER_CLASS)
    total_instances, unreadable = count_instances(clusters)
    say("%s actors: %d, instances: %d (read via %s)%s"
        % (CLUSTER_CLASS, len(clusters), total_instances,
           _INSTANCE_SOURCE[0],
           "" if not unreadable else
           "; %d actors unreadable" % unreadable))
    if clusters and unreadable:
        fail("%d clusters would not report an instance count" % unreadable)

    hidden, hidden_via = count_hidden(clusters)
    say("clusters reporting hidden: %d/%d (via %s)"
        % (hidden, len(clusters), hidden_via))
    say("  -> this is the number logs/hide_voxel_layer.txt could not keep: that "
        "run set the flag 1041 times and then read 0 back after a reload, so the "
        "hides were NOT in the file. Whatever hides 1041 actors in a frame does "
        "it in memory, in that process.")

    if not clusters:
        say("")
        say("nothing to retire: the level already holds 0 %s actors. "
            "StaticMeshActor=%d PlayerStart=%d"
            % (CLUSTER_CLASS, n_static_before, before.get("PlayerStart", 0)))
        say("RESULT: PASS already retired (0 clusters, %d StaticMeshActor)"
            % n_static_before)
        return 0

    # ---- 2. pre-flight: the floor must exist before the old one goes ------ #
    say("")
    say("--- pre-flight ------------------------------------------------------")
    tiles_ok, tiles_bad = collision_gate(world)
    if tiles_ok == 0 or tiles_bad:
        fail("terrain collision incomplete; deleting the cube layer would drop "
             "the pawn through the world (rebuild with build_terrain_meshes.py)")
        say("")
        say("RESULT: FAIL pre-flight collision gate (%d ok, %d bad)"
            % (tiles_ok, len(tiles_bad)))
        return 1

    if dry:
        say("")
        say("RESULT: PASS dry run -- would delete %d actors / %d instances"
            % (len(clusters), total_instances))
        return 0

    # ---- 3. delete ------------------------------------------------------- #
    say("")
    say("--- delete ----------------------------------------------------------")
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    destroyed = 0
    raised = []
    for a in clusters:
        try:
            # destroy_actor returns True when it removed the actor; a False is
            # counted as not-destroyed rather than assumed to have worked.
            if eas.destroy_actor(a):
                destroyed += 1
            else:
                raised.append("%s: destroy_actor returned False" % label(a))
        except Exception as exc:                       # noqa: BLE001
            raised.append("%s: %s" % (label(a), str(exc).splitlines()[0][:50]))
    say("destroy_actor: %d/%d destroyed, %d refused"
        % (destroyed, len(clusters), len(raised)))
    for r in raised[:10]:
        say("  ! %s" % r)
    if destroyed != len(clusters):
        fail("only %d of %d clusters were destroyed"
             % (destroyed, len(clusters)))

    # ---- 4. survivors, in this session ---------------------------------- #
    say("")
    say("--- survivors, in this session --------------------------------------")
    mid = assert_survivors(world, n_static_before, "in-session")
    say("actor total: %d -> %d (-%d)"
        % (sum(before.values()), sum(mid.values()),
           sum(before.values()) - sum(mid.values())))

    # A failed survivor check means the wrong thing was deleted. Then the
    # session is the only place the mistake exists, so it must NOT be written to
    # the file -- the operator restores from backup_umap/ or reopens the level.
    if _FAILED:
        say("")
        say("--- save SKIPPED ---------------------------------------------------")
        say("the level was NOT saved: a survivor check failed, so the deletion "
            "stays in this session only. Reopen the map (or restore it from "
            "G:\\BaiduNetdiskDownload\\real\\MC2UE5\\backup_umap\\) and fix the "
            "cause before running this again.")
        say("")
        say("RESULT: FAIL %d survivor check(s) failed; level not saved"
            % len(_FAILED))
        return 1

    # ---- 5. save, then verify from disk --------------------------------- #
    say("")
    say("--- save ------------------------------------------------------------")
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    try:
        saved = les.save_current_level()
    except Exception as exc:                           # noqa: BLE001
        saved = False
        say("  save_current_level raised %s" % str(exc).splitlines()[0][:70])
    say("save_current_level -> %s" % saved)
    if not saved:
        fail("the level was not saved; the deletion is not in the file")

    say("")
    say("--- reload from disk ------------------------------------------------")
    unreal.EditorLoadingAndSavingUtils.load_map(MAP)
    w2 = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    if w2 is None:
        fail("level did not reload")
        after = {}
    else:
        after = assert_survivors(w2, n_static_before, "after-reload")
        say("actor total after reload: %d" % sum(after.values()))
        for cls in sorted(after, key=lambda c: -after[c])[:10]:
            say("  %-28s %5d" % (cls, after[cls]))

    # ---- verdict --------------------------------------------------------- #
    say("")
    say("removed: %d %s actors, %d Cube1x1x1 instances"
        % (destroyed, CLUSTER_CLASS, total_instances))
    say("kept:    %d StaticMeshActor (bld_*_structure / bld_*_detail / "
        "terrain_*), 1 PlayerStart, %s, %s, 1 SkyLight, 1 %s"
        % (after.get("StaticMeshActor", 0) if after else n_static_before,
           SUN_LABEL, BOUNCE_LABEL, PPV_LABEL))
    say("restore: copy MCReplica.umap back from "
        "G:\\BaiduNetdiskDownload\\real\\MC2UE5\\backup_umap\\<timestamp>\\, or "
        "rebuild with tools/build_world.py")
    say("")
    if _FAILED:
        say("FAIL: %d problem(s): %s" % (len(_FAILED), "; ".join(_FAILED[:6])))
        say("RESULT: FAIL %d cluster(s) not cleanly retired" % len(_FAILED))
        return 1
    say("RESULT: PASS retired %d %s actors / %d instances, saved and verified "
        "from disk; %d StaticMeshActor + lights untouched"
        % (destroyed, CLUSTER_CLASS, total_instances,
           after.get("StaticMeshActor", n_static_before) if after else
           n_static_before))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        say(traceback.format_exc())
        say("RESULT: FAIL uncaught exception")
        raise
