# -*- coding: utf-8 -*-
"""
build_world.py -- one command that rebuilds the whole game world, in order.

Why this exists
----------------
Since 10-07 the level has been assembled by an ad-hoc sequence of editor
scripts run by hand (see REFACTOR_PLAN §8.36 and docs/s6a_wiring_notes.md).
Nothing recorded that sequence, `project/Content/Python/build_release_level.py`
(the "official" entry) actually builds the retired v0.7.0 chain, and the level
itself is not in git (Content/MC is ignored). The chain therefore existed only
in the operator's memory -- this file is that memory, executable.

It runs, in order:

  data side (this python, needs numpy -- use the venv):
    1. classify.py                     (only if out/classify is missing)
    2. extract_structures.py           (--uv-mode block: variants + glass slots)
    3. pack_terrain_tiles.py           (24-tile terrain pack)
    4. build_vegetation_pack.py        (S4.5: the trees, packed)
    5. pick_spawn.py                   (searched spawn -> logs/spawn.json)

  editor side (UnrealEditor-Cmd -nullrhi, one process per step, because the
  async mesh builds crash when too many tiles are built in one session):
    6. import_family_materials --stage materials   (T_MC + M_MC for new slots)
    7. build_terrain_meshes.py                     (assets + NORMALS + COLLISION)
    8. build_vegetation_meshes.py                  (assets + actors)
    9. import_family_materials --stage meshes      (structure OBJs + slot bind)
   10. ensure_lighting.py                          (sun/sky, ApplyLook values)
   11. apply_spawn.py                              (PlayerStart <- spawn.json)
   12. hide_voxel_layer.py                         (visibility + collision off,
                                                    gated on terrain collision)
   13. level_census.py                             (read-only final audit)

Then prints the packaging commands (tools/build_shipping.bat +
tools/package_shipping.bat). Nothing here cooks or packages by itself.

Editor steps are verified by their own log's "RESULT: PASS/FAIL" line rather
than the process exit code: UnrealEditor-Cmd's exit code is not a reliable
success signal on this engine (documented in REFACTOR_PLAN §8.6), while every
script in this repo already writes RESULT to $MC_LOG_DIR.

Usage (Windows, from the repo root, venv python -- see tools/mcpaths.py for
where the venv and engine actually live; nothing here hardcodes a drive):

    python tools\\build_world.py
    python tools/build_world.py --skip-data          # editor steps only
    python tools/build_world.py --only census        # one editor step
    python tools/build_world.py --engine <path to UnrealEditor-Cmd.exe>

Logs:  MC_LOG_DIR (default <repo>/../logs) -- every step appends to
       logs/build_world_report.json, which is the reproducibility record:
       the exact steps, their order, and their PASS/FAIL for this build.
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
LOGDIR = os.environ.get("MC_LOG_DIR", os.path.join(os.path.dirname(REPO), "logs"))
PROJECT = os.path.join(REPO, "project", "MCReplica.uproject")
PY_DIR = os.path.join(REPO, "project", "Content", "Python")
# The engine path used to be a literal on the old Q: drive. After the 10-28
# migration that literal survived as a valid-looking string pointing at a drive
# that does not exist, so the first symptom would be "editor exited 1" with no
# other clue. mcpaths derives it from this checkout's own location instead.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from mcpaths import EDITOR_CMD, HOME, UE_ROOT  # noqa: E402

EDITOR = EDITOR_CMD

ENV = dict(os.environ)
ENV["MC_LOG_DIR"] = LOGDIR
# Propagate the resolved roots so editor-side scripts and child processes agree
# with this process even when the caller overrode them on the command line.
ENV["MC2UE5_HOME"] = HOME
ENV["MC_UE_ROOT"] = UE_ROOT


def _say(msg):
    print(msg, flush=True)
    os.makedirs(LOGDIR, exist_ok=True)
    with open(os.path.join(LOGDIR, "build_world_steps.txt"), "a") as fh:
        fh.write(msg + "\n")


def _log_result(step, ok, seconds, detail=""):
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, "build_world_report.json")
    rep = {"steps": []}
    try:
        with open(path) as fh:
            rep = json.load(fh)
    except Exception:
        pass
    rep["steps"].append({"step": step, "ok": bool(ok),
                         "seconds": round(seconds, 1),
                         "detail": str(detail)[:400],
                         "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                         "command": " ".join(sys.argv)})
    with open(path, "w") as fh:
        json.dump(rep, fh, indent=2)


def run_host(step, argv):
    _say("[data] %-28s %s" % (step, " ".join(argv)))
    t0 = time.time()
    p = subprocess.run([sys.executable] + argv, cwd=REPO, env=ENV)
    ok = p.returncode == 0
    _log_result(step, ok, time.time() - t0, "exit=%d" % p.returncode)
    if not ok:
        _say("*** %s FAILED (exit %d) -- stopping" % (step, p.returncode))
        sys.exit(1)
    return ok


def run_editor(step, script, extra_env=None):
    path = script if os.path.isabs(script) else os.path.join(REPO, script)
    if not os.path.isfile(path):
        _say("*** %s: script not found: %s" % (step, path))
        sys.exit(1)
    env = dict(ENV)
    if extra_env:
        env.update(extra_env)
    argv = [EDITOR, PROJECT,
            "-ExecutePythonScript=%s" % path,
            "-nullrhi", "-unattended", "-nopause", "-nosplash", "-stdout"]
    _say("[editor] %-26s %s" % (step, os.path.basename(script)))
    t0 = time.time()
    proc = subprocess.run(argv, cwd=REPO, env=env)
    # The exit code is not trustworthy; each script writes RESULT into its own
    # log under MC_LOG_DIR. Read THAT back and make it the verdict.
    name = os.path.splitext(os.path.basename(script))[0]
    log_path = os.path.join(LOGDIR, "%s.txt" % name)
    verdict = None
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-4000:]
        for line in tail.splitlines():
            if "RESULT:" in line:
                verdict = line
    except Exception:
        verdict = "NO LOG at %s" % log_path
    ok = verdict is not None and "PASS" in verdict
    _log_result(step, ok, time.time() - t0,
                "exit=%d | %s" % (proc.returncode, verdict))
    _say("         %s" % verdict)
    if not ok:
        _say("*** %s FAILED -- stopping (see %s)" % (step, log_path))
        sys.exit(1)
    return ok


def classify_needed():
    role = os.path.join(REPO, "out", "classify", "rolevolume.npy")
    terrain = os.path.join(REPO, "out", "classify", "terrain.npy")
    return not (os.path.isfile(role) and os.path.isfile(terrain))


def main():
    global EDITOR
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--engine", default=EDITOR, help="UnrealEditor-Cmd.exe")
    ap.add_argument("--skip-data", action="store_true",
                    help="skip the host-python data stages (packs exist)")
    ap.add_argument("--only", default=None,
                    help="run ONE editor step by short name "
                         "(materials|terrain|vegetation|meshes|lighting|spawn|"
                         "hide|census)")
    ap.add_argument("--bin", default="voxel_data/full/overworld.bin")
    args = ap.parse_args()
    EDITOR = args.engine

    os.makedirs(LOGDIR, exist_ok=True)
    _say("=" * 74)
    _say("build_world | %s | logdir=%s" % (time.strftime("%F %T"), LOGDIR))
    _say("=" * 74)

    if args.only:
        table = {
            "materials": ("tools/import_family_materials.py",
                          {"MC_STAGE": "materials"}),
            "terrain": ("tools/build_terrain_meshes.py", {}),
            "vegetation": ("project/Content/Python/build_vegetation_meshes.py",
                           {}),
            "meshes": ("tools/import_family_materials.py",
                       {"MC_STAGE": "meshes"}),
            "lighting": ("tools/ensure_lighting.py", {}),
            "spawn": ("project/Content/Python/apply_spawn.py", {}),
            "hide": ("tools/hide_voxel_layer.py", {}),
            "census": ("project/Content/Python/level_census.py", {}),
        }
        if args.only not in table:
            _say("*** unknown step %r; known: %s" % (args.only,
                                                     ", ".join(table)))
            return 1
        script, env = table[args.only]
        run_editor(args.only, script, env)
        _say("done (single step).")
        return 0

    if not args.skip_data:
        if classify_needed():
            run_host("classify", ["tools/classify.py",
                                  "--bin", args.bin, "--out", "out/classify"])
        else:
            _say("[data] classify                     (out/classify exists, skip)")
        run_host("extract_structures",
                 ["tools/extract_structures.py", "--uv-mode", "block",
                  "--bin", args.bin])
        run_host("pack_terrain_tiles",
                 ["tools/pack_terrain_tiles.py", "--bin", args.bin,
                  "--terrain", "out/classify/terrain.npy"])
        run_host("build_vegetation_pack",
                 ["tools/build_vegetation_pack.py", "--bin", args.bin])
        run_host("pick_spawn",
                 ["tools/pick_spawn.py", "--bin", args.bin,
                  "--out", os.path.join(LOGDIR, "spawn.json")])
    else:
        _say("[data] skipped (--skip-data)")

    run_editor("materials", "tools/import_family_materials.py",
               {"MC_STAGE": "materials"})
    run_editor("terrain", "tools/build_terrain_meshes.py")
    run_editor("vegetation", "project/Content/Python/build_vegetation_meshes.py")
    run_editor("meshes", "tools/import_family_materials.py",
               {"MC_STAGE": "meshes"})
    run_editor("lighting", "tools/ensure_lighting.py")
    run_editor("spawn", "project/Content/Python/apply_spawn.py")
    run_editor("hide", "tools/hide_voxel_layer.py")
    run_editor("census", "project/Content/Python/level_census.py")

    _say("")
    _say("=" * 74)
    _say("WORLD REBUILT -- all stages PASS")
    _say("  report: %s" % os.path.join(LOGDIR, "build_world_report.json"))
    _say("Next, cook + package (from an elevated/normal cmd, repo root):")
    _say(r"  tools\build_shipping.bat")
    _say(r"  tools\package_shipping.bat   # -> G:\BaiduNetdiskDownload\real\MC2UE5\dist_verify")
    _say("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())
