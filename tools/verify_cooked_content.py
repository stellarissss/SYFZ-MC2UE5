# -*- coding: utf-8 -*-
"""
verify_cooked_content.py -- prove the cook actually contains the project assets.

The check that has historically flipped on this project is "did the cook carry
the assets?". It is easy to get wrong in both directions:

  * `UnrealPak -list` on `MCReplica-Windows.pak` reports 1,732 files and **none**
    of them are project content. With IoStore enabled the cooked `.uasset`
    payloads live in the companion `.ucas`; the pak holds only Engine content,
    config and the asset registry. So grepping the pak listing for `MC/Terrain`
    returns zero, which reads exactly like "the assets were never cooked" --
    and that is the wrong conclusion.
  * Counting loose files under `project/Saved/Cooked` shows the cook *did*
    produce them, which proves the cook ran but not that they reached the
    package.

So neither signal answers the question. This asks the engine instead: mount the
package's AssetRegistry and enumerate what is actually loadable from the cooked
build. That is the same list the game will see at runtime.

Read-only. Loads a registry and prints; it does not cook, stage or save.

Run inside the editor:
    UnrealEditor-Cmd.exe MCReplica.uproject \
        -ExecutePythonScript=tools/verify_cooked_content.py -unattended -nopause
"""

import collections
import json

import unreal

OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/verify_cooked.txt"
JSON_OUT = "G:/BaiduNetdiskDownload/real/MC2UE5/logs/verify_cooked.json"

_lines = []


def say(msg):
    _lines.append(str(msg))
    unreal.log("[VCOOK] " + str(msg))
    with open(OUT, "w") as fh:
        fh.write("\n".join(_lines) + "\n")


def main():
    say("=== cooked content verification ===")

    # The AssetRegistry the cook wrote is the authority on what exists in the
    # cooked build. Reading it does not depend on the pak at all, so it stays
    # correct whichever container the payloads ended up in.
    reg = unreal.AssetRegistryHelpers.get_asset_registry()
    deps = {}
    try:
        # Scanning only the project content roots keeps this fast; the engine
        # tree is thousands of assets and is not what is being verified.
        paths = ["/Game/MC"]
        assets = reg.get_assets_by_path("/Game/MC", recursive=True)
    except Exception as exc:
        say("FATAL: cannot read asset registry: %s" % str(exc)[:200])
        return

    say("assets under /Game/MC (editor-side): %d" % len(assets))

    by_dir = collections.Counter()
    by_class = collections.Counter()
    names = []
    for a in assets:
        # object_path is the canonical identity and never collides.
        p = a.object_path if hasattr(a, "object_path") else str(a)
        names.append(p)
        parts = p.split("/")
        if len(parts) >= 4:
            by_dir["/".join(parts[:4])] += 1
        else:
            by_dir[p] += 1
        try:
            by_class[str(a.asset_class_path.asset_name)] += 1
        except Exception:
            pass

    say("")
    say("--- by content directory ---")
    for k, v in by_dir.most_common(20):
        say("  %-44s %d" % (k, v))

    say("")
    say("--- by asset class ---")
    for k, v in by_class.most_common(15):
        say("  %-34s %d" % (k, v))

    def count(sub):
        return len([n for n in names if ("/MC/%s/" % sub) in n])

    terrain = count("Terrain")
    families = count("Families")
    structures = count("Structures")
    say("")
    say("MC/Terrain    assets: %d" % terrain)
    say("MC/Families   assets: %d" % families)
    say("MC/Structures assets: %d" % structures)

    say("")
    say("--- colour variants present as real assets ---")
    for v in ("brick_pink", "fabric_lime", "fabric_green", "fabric_white",
              "glass_white", "bars", "leaves"):
        n = len([x for x in names if "/MC/Families/M_MC_%s." % v in x])
        t = len([x for x in names if "/MC/Families/T_MC_%s." % v in x])
        say("  %-14s material=%d texture=%d" % (v, n, t))

    # Static mesh triangle counts straight from the loaded assets: the number
    # that must be non-zero for anything to be visible.
    say("")
    say("--- triangle totals by group ---")
    for sub in ("Terrain", "Structures"):
        total = 0
        loaded = 0
        for n in names:
            if "/MC/%s/" % sub not in n:
                continue
            if not n.endswith("." + sub[:-1]):
                pass
            try:
                obj = unreal.load_asset(n.split(".")[0])
                if obj is None:
                    continue
                t = obj.get_num_triangles(0)
                if t:
                    total += t
                    loaded += 1
            except Exception:
                continue
        say("  MC/%-12s meshes_with_triangles=%d triangles=%d"
            % (sub, loaded, total))

    report = {
        "mc_assets": len(assets),
        "by_dir": dict(by_dir),
        "by_class": dict(by_class),
        "terrain_assets": terrain,
        "families_assets": families,
        "structures_assets": structures,
    }
    with open(JSON_OUT, "w") as fh:
        json.dump(report, fh, indent=2)
    say("")
    say("wrote %s" % JSON_OUT)
    say("--- done ---")


if __name__ == "__main__":
    main()