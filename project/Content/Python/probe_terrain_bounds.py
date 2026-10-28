# -*- coding: utf-8 -*-
"""probe_terrain_bounds.py -- is the terrain's RENDER-DATA bounding box sane?

Why this exists
---------------
2026-10-28, launching the editor from the migrated G: drive, the log carried:

    LogStaticMesh: Warning: FStaticMeshRenderData::Serialize found NaN in
    Bounds of '/Game/MC/Terrain/terrain_0112_-544'

That matters more than it looks. Every asset-level check this project had run
so far -- triangle count 676,656/676,656, normals 387,096/387,096, 109 material
slots with 0 None, 24 actors visible=True, sections(0)=8/9/4, actor bounding
boxes overlapping the camera -- was computed from the mesh description or the
actor transform. **None of them looked at FStaticMeshRenderData's bounds**,
which is what the visibility/culling path actually uses. A mesh with NaN bounds
is not culled *correctly*, it is culled *always*: the renderer cannot produce a
distance to it, so it never reaches a draw call. That is a complete explanation
for "geometry is perfect and nothing appears", and it is invisible to every
check we had.

The source vertex data is NOT the problem -- out/terrain_pack/*.pos was
verified finite on all 24 tiles (0 non-finite values). So if the bounds are NaN
it is introduced while building the render data, and the place to look is
build_terrain_meshes.py, not the packer.

This script reads the truth from the engine and prints one line per tile:

    <asset>  tris=<n> sections=<n>  bounds origin/extent/sphere  VERDICT

Run inside the editor:
    UnrealEditor-Cmd.exe <uproject> -ExecutePythonScript=<this> \
        -nullrhi -unattended -nopause -nosplash -stdout
"""

import math
import os

import unreal

OUT_NAME = "terrain_bounds.txt"
_log = []


def say(msg):
    _log.append(str(msg))
    unreal.log("[MCBOUNDS] " + str(msg))
    path = _out_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(_log) + "\n")


def _out_path():
    d = os.environ.get("MC_LOG_DIR")
    if not d:
        here = os.path.dirname(os.path.abspath(__file__))
        mc2ue5 = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(here))))
        d = os.path.join(mc2ue5, "logs")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, OUT_NAME)


def finite(*vals):
    for v in vals:
        if v is None or math.isnan(v) or math.isinf(v):
            return False
    return True


def probe(asset_path):
    mesh = unreal.load_asset(asset_path)
    if mesh is None:
        return "LOAD FAILED"

    tris = -1
    try:
        tris = unreal.StaticMeshLibrary.get_static_mesh_triangle_count(mesh)
    except Exception:
        pass

    sections = -1
    try:
        sections = mesh.get_num_sections(0)
    except Exception:
        pass

    # StaticMesh.get_bounding_box() returns the box the renderer uses
    # (FBoxSphereBounds of render data), which is exactly what we want.
    # API note (learned by failing): unreal.Box exposes `min` / `max` /
    # `is_valid`, NOT FBoxSphereBounds' origin/extent/sphere_radius. Don't guess
    # the shape -- print dir(box) if this ever changes again.
    box = None
    try:
        box = mesh.get_bounding_box()
    except Exception as exc:
        return "get_bounding_box FAILED: %s" % str(exc)[:60]

    if box is None:
        return "bounds None"

    lo = box.min
    hi = box.max
    ox = (lo.x + hi.x) * 0.5
    oy = (lo.y + hi.y) * 0.5
    oz = (lo.z + hi.z) * 0.5
    ex = (hi.x - lo.x) * 0.5
    ey = (hi.y - lo.y) * 0.5
    ez = (hi.z - lo.z) * 0.5
    r = math.sqrt(ex * ex + ey * ey + ez * ez)
    ok = finite(lo.x, lo.y, lo.z, hi.x, hi.y, hi.z, ex, ey, ez, r)
    verdict = "OK" if ok else "*** NaN/Inf in bounds ***"
    return ("tris=%-7d sections=%-2d origin=(%9.1f %9.1f %9.1f) "
            "extent=(%9.1f %9.1f %9.1f) r=%-9.1f %s"
            % (tris, sections, ox, oy, oz, ex, ey, ez, r, verdict))


def main():
    say("=== terrain StaticMesh render-data bounds ===")
    ar = unreal.AssetRegistryHelpers.get_asset_registry()
    filt = unreal.ARFilter(
        class_paths=[unreal.TopLevelAssetPath("/Script/Engine", "StaticMesh")],
        package_paths=["/Game/MC/Terrain"],
        recursive_paths=True,
    )
    names = sorted(str(a.package_name) for a in ar.get_assets(filt))
    say("assets under /Game/MC/Terrain: %d" % len(names))

    bad = 0
    for pkg in names:
        path = pkg.split(".")[0]
        desc = probe(path)
        short = path.rsplit("/", 1)[-1]
        say("  %-26s %s" % (short, desc))
        if "NaN" in desc or "FAILED" in desc or "None" in desc:
            bad += 1

    say("")
    say("=== summary ===")
    say("terrain assets: %d   bad bounds: %d" % (len(names), bad))
    if names and bad == len(names):
        say("RESULT: FAIL -- every terrain mesh has NaN/Inf render bounds; the "
            "culling path can never place them, which alone explains 0% "
            "terrain pixels")
        return 1
    if bad:
        say("RESULT: FAIL -- %d of %d terrain meshes have NaN/Inf bounds" % (bad, len(names)))
        return 1
    say("RESULT: PASS -- all %d terrain mesh bounds are finite" % len(names))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
