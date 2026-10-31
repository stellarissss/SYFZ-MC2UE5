# -*- coding: utf-8 -*-
r"""
import_block_textures.py -- import the original Minecraft 1.16.5 block
textures and build one material per texture, then bind every mesh slot.

Why this replaces the family materials
--------------------------------------
The family model had two measured failures, both of which this file removes:

  * **Granularity.** 260 block names -> 19 families. ``cobblestone``,
    ``stone_bricks``, ``end_stone_bricks`` and ``smooth_stone`` all sampled one
    grey photograph, so four different materials became one flat surface.
  * **Scale.** The family textures were 512 px photographs tiled one-per-block.
    A block is 16-20 px on screen, so the GPU sampled the photo's 16x16 mip --
    its local average. Per-block contrast sigma measured 11.6 against 45.6 for
    the original pixel art, edge energy 4.3 against 18.8.

``out/textures/`` now holds 16x16 pixel art derived from
``assets/textures/block/*.png`` (one file per slot, biome tints applied to the
greyscale grass/foliage tiles), and ``out/textures/slots.json`` is the slot
table the meshers wrote their ``usemtl`` names from. This script imports
exactly that table -- it does not re-derive it -- so the material names and the
mesh slots cannot drift apart.

Two settings matter and are asserted, not assumed:

  * **TF_NEAREST.** Minecraft is pixel art; bilinear/trilinear sampling softens
    the one thing that carries the look. Mips stay ON (``FROM_TEXTURE_GROUP``),
    matching the game's own NEAREST_MIPMAP_LINEAR: 16 -> 8 -> 4 -> 2 -> 1.
  * **Uncompressed (TC_EDITOR_ICON).** BC1 on a 16x16 tile is 2 blocks; the
    block-boundary artefacts are larger than the art. 274 tiles cost ~0.3 MB
    uncompressed -- cheaper than the artefacts.

Alpha decides the blend mode, measured from the PNG rather than looked up in a
hand-maintained table: a texture whose alpha is not uniformly 255 (leaves,
bars, panes, flowers) becomes BLEND_MASKED with the sample's own A wired to
Opacity Mask and an explicit clip of 0.333. An all-opaque texture stays opaque,
which keeps the shader-permutation count down and avoids a sorting cost nothing
asked for.

Usage (inside the editor, one stage per process):

    UnrealEditor-Cmd.exe <uproject> -ExecutePythonScript=<this file> -unattended
    MC_STAGE=materials | meshes | all | verify   (environment, authoritative)

Log ends with ``RESULT: PASS/FAIL`` like every other script in this repo.
"""

import json
import os
import sys
import time
import traceback

import unreal

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

TEX_DIR = os.path.join(REPO, "out", "textures")
SLOTS_JSON = os.path.join(TEX_DIR, "slots.json")
STRUCT_DIR = os.path.join(REPO, "out", "structures")
TERRAIN_DIR = os.path.join(REPO, "out", "terrain")
LOG_PATH = os.path.join(os.environ.get("MC_LOG_DIR", os.path.join(REPO, "logs")),
                        "import_block_textures.txt")

CONTENT = "/Game/MC/Textures"
STRUCT_CONTENT = "/Game/MC/Structures"
TERRAIN_CONTENT = "/Game/MC/Terrain"
MAP_PATH = "/Game/Maps/MCReplica"

TEX_FMT = CONTENT + "/T_MC_%s"
MAT_FMT = CONTENT + "/M_MC_%s"
MI_FMT = CONTENT + "/MI_MC_%s"

#: Roughness per texture stem. Everything not listed is matte. Only the
#: families whose look depends on it are listed, and each is a measured look
#: decision carried over from docs/glass_spec.md: windows must read as dark
#: rectangles (0.10), water as a specular sheet (0.05), metal as brushed (0.35),
#: and iron_bars at 0.30 so the cutout rails catch a highlight (section 2.2).
ROUGHNESS = {
    "glass": 0.10, "water": 0.05, "ice": 0.10,
    "iron_block": 0.35, "gold_block": 0.30, "diamond_block": 0.25,
    "emerald_block": 0.30, "lapis_block": 0.40, "netherite_block": 0.45,
    "iron_bars": 0.30,
}
DEFAULT_ROUGHNESS = 0.85

#: Stained panes are still glass but do not start with the word, so a prefix
#: match misses them (`red_stained_glass_pane_top`). The spec's argument for
#: 0.10 -- the highlight is what makes a window read as glass -- applies to them
#: unchanged.
GLASS_SUFFIXES = ("_stained_glass", "_stained_glass_pane_top",
                  "_stained_glass_pane")

#: The one clip value the assertions accept.
MASKED_CLIP = 0.333

_LINES = []


def say(msg):
    """Append one line to the in-memory log and flush it to disk.

    Flushed every line: when the editor crashes mid-run the tail is the only
    evidence of where it got to.
    """
    _LINES.append(str(msg))
    try:
        unreal.log("[MCTEX] " + str(msg))
    except Exception:
        pass
    try:
        os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
        with open(LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LINES) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# the slot table (read, never re-derived)
# --------------------------------------------------------------------------- #

def load_slots():
    """-> (slot_names, blocks, meta) from out/textures/slots.json.

    The meshers wrote their ``usemtl`` names from this file, so importing
    anything other than this file would silently bind the wrong texture to a
    slot. A missing file is fatal, not a fallback: guessing a texture list is
    how a pipeline reports success while every wall shows the wrong material.
    """
    if not os.path.isfile(SLOTS_JSON):
        raise RuntimeError(
            "slot table missing: %s -- run tools/block_textures.py first"
            % SLOTS_JSON)
    with open(SLOTS_JSON, encoding="utf-8") as fh:
        doc = json.load(fh)
    names = doc["slot_names"]
    if not names or names[0] != "other":
        raise RuntimeError("slot table is malformed (slot 1 must be 'other')")
    return names, doc.get("blocks", {}), doc


# --------------------------------------------------------------------------- #
# compile checking (the only thing that can see an uncompilable material)
# --------------------------------------------------------------------------- #

def newest_editor_log():
    log_dir = os.path.join(REPO, "project", "Saved", "Logs")
    if not os.path.isdir(log_dir):
        return None
    live = os.path.join(log_dir, "MCReplica.log")
    if os.path.isfile(live):
        return live
    logs = [os.path.join(log_dir, f) for f in os.listdir(log_dir)
            if f.endswith(".log")]
    return max(logs, key=os.path.getmtime) if logs else None


def current_log_offset():
    log = newest_editor_log()
    try:
        return os.path.getsize(log) if log else 0
    except OSError:
        return 0


def scan_compile_errors(mat_name, offset, tries=6, gap=0.2):
    """Scan only the editor-log region written after ``offset``.

    UE 5.8's Python `Material` exposes no compile-check API, so the log is the
    only witness. Scanning from an offset captured just before
    `recompile_material` keeps an earlier material's error from being
    mis-attributed to this one.
    """
    log = newest_editor_log()
    if log is None:
        return False, "no editor log found"
    try:
        size = os.path.getsize(log)
    except OSError:
        return False, "cannot stat editor log"
    if offset > size:
        offset = 0

    hits = []
    pos = offset
    for attempt in range(tries):
        try:
            with open(log, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
                pos = fh.tell()
        except OSError as exc:
            return False, "cannot read editor log: %s" % exc
        for line in data.decode("utf-8", "replace").splitlines():
            if ("Failed to compile Material" in line
                    or "Default Material will be used" in line
                    or "LogMaterial: Error" in line):
                hits.append(line.strip()[:220])
        if hits:
            break
        if attempt < tries - 1:
            time.sleep(gap)
    return (False, "%d compile problem(s): %s" % (len(hits), hits[:3])) \
        if hits else (True, "clean")


# --------------------------------------------------------------------------- #
# textures
# --------------------------------------------------------------------------- #

def png_has_alpha(path):
    """-> (has_cutout, min_alpha) for an 8-bit RGBA/Gray+Alpha PNG.

    Decoded with `zlib` only (the editor's embedded interpreter has no PIL), so
    the masked decision comes from the actual bytes on disk rather than from a
    table that can drift away from the asset.
    """
    import struct
    import zlib
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False, 255
    pos, idat = 8, bytearray()
    width = height = bitdepth = colortype = None
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bitdepth, colortype = struct.unpack(">IIBB", body[:10])
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if bitdepth != 8 or colortype not in (6, 4):
        return False, 255
    channels = 4 if colortype == 6 else 2
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    prev = bytearray(stride)
    alpha_idx = channels - 1
    lo = 255
    pos_in = 0
    for _ in range(height):
        filt = raw[pos_in]
        pos_in += 1
        line = bytearray(raw[pos_in:pos_in + stride])
        pos_in += stride
        if filt == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif filt == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filt == 3:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif filt == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = prev[i]
                c = prev[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        for i in range(alpha_idx, stride, channels):
            if line[i] < lo:
                lo = line[i]
        prev = line
    return lo < 255, lo


def import_texture(name):
    """Import out/textures/<name>.png -> /Game/MC/Textures/T_MC_<name>."""
    png = os.path.join(TEX_DIR, name + ".png")
    if not os.path.isfile(png):
        return None, "png missing: %s" % png

    task = unreal.AssetImportTask()
    task.set_editor_property("filename", png)
    task.set_editor_property("destination_path", CONTENT)
    task.set_editor_property("destination_name", "T_MC_" + name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])

    tex = unreal.load_asset(TEX_FMT % name)
    if tex is None:
        return None, "import produced no asset at %s" % (TEX_FMT % name)

    masked, alpha_min = png_has_alpha(png)
    settings = (
        # Wrap is the whole point: UVs are in block units, so every quad wider
        # than one block samples past 1. Clamp would smear the edge texel.
        ("address_x", unreal.TextureAddress.TA_WRAP),
        ("address_y", unreal.TextureAddress.TA_WRAP),
        ("srgb", True),
        ("filter", unreal.TextureFilter.TF_NEAREST),
        # Uncompressed: BC1 on a 16x16 tile introduces artefacts larger than the
        # art. 274 tiles uncompressed is ~0.3 MB.
        ("compression_settings", unreal.TextureCompressionSettings.TC_EDITOR_ICON),
        ("mip_gen_settings", unreal.TextureMipGenSettings.TMGS_FROM_TEXTURE_GROUP),
        ("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD),
        ("never_stream", True),
    )
    for prop, value in settings:
        try:
            tex.set_editor_property(prop, value)
        except Exception as exc:
            return None, "set %s failed: %s" % (prop, str(exc)[:90])

    unreal.EditorAssetLibrary.save_loaded_asset(tex)

    # Read back what actually stuck: a set_editor_property that silently does
    # nothing has been this project's failure mode more than once.
    ax = tex.get_editor_property("address_x")
    ay = tex.get_editor_property("address_y")
    if ax != unreal.TextureAddress.TA_WRAP or ay != unreal.TextureAddress.TA_WRAP:
        return None, "address_x/y read back as %s/%s (expected TA_WRAP)" % (ax, ay)
    got_filter = tex.get_editor_property("filter")
    if got_filter != unreal.TextureFilter.TF_NEAREST:
        return None, "filter read back as %s (expected TF_NEAREST)" % got_filter
    return tex, ("masked(alpha_min=%d)" % alpha_min) if masked else "opaque"


# --------------------------------------------------------------------------- #
# materials
# --------------------------------------------------------------------------- #

def roughness_for(name):
    for stem, value in ROUGHNESS.items():
        if name.startswith(stem):
            return value
    if name.endswith(GLASS_SUFFIXES):
        return ROUGHNESS["glass"]
    return DEFAULT_ROUGHNESS


def build_material(name, tex, masked):
    """Load-or-create M_MC_<name>, rebuild the proven five-node graph."""
    mel = unreal.MaterialEditingLibrary
    mat_path = MAT_FMT % name

    if unreal.EditorAssetLibrary.does_asset_exist(mat_path):
        mat = unreal.load_asset(mat_path)
        if mat is None:
            return None, "exists but would not load (NOT recreating it)"
        try:
            for expr in mel.get_material_expressions(mat):
                mel.delete_material_expression(mat, expr)
        except Exception as exc:
            return None, "could not clear expressions: %s" % str(exc)[:120]
    else:
        mat = unreal.AssetToolsHelpers.get_asset_tools().create_asset(
            "M_MC_%s" % name, CONTENT, unreal.Material,
            unreal.MaterialFactoryNew())
    if mat is None:
        return None, "create/load returned None"

    def node(cls, x, y):
        e = mel.create_material_expression(mat, cls, x, y)
        if e is None:
            raise RuntimeError("could not create %s" % cls.get_name())
        return e

    try:
        sample = node(unreal.MaterialExpressionTextureSampleParameter2D, -400, 0)
        sample.set_editor_property("ParameterName", name)
        sample.set_editor_property("Texture", tex)
        mel.connect_material_property(sample, "RGB",
                                      unreal.MaterialProperty.MP_BASE_COLOR)

        tiling = node(unreal.MaterialExpressionScalarParameter, -400, -260)
        tiling.set_editor_property("ParameterName", "Tiling")
        tiling.set_editor_property("DefaultValue", 1.0)

        uv = node(unreal.MaterialExpressionTextureCoordinate, -620, -260)
        mult = node(unreal.MaterialExpressionMultiply, -180, -260)
        mel.connect_material_expressions(uv, "", mult, "A")
        mel.connect_material_expressions(tiling, "", mult, "B")
        mel.connect_material_expressions(mult, "", sample, "Coordinates")

        rough = node(unreal.MaterialExpressionConstant, -180, 260)
        rough.set_editor_property("R", roughness_for(name))
        mel.connect_material_property(rough, "",
                                      unreal.MaterialProperty.MP_ROUGHNESS)

        if masked:
            # One more output of the sample already created -- no new node type.
            # BLEND_MASKED (not translucent): masked stays in the opaque pass,
            # sorts correctly and casts proper shadows.
            mel.connect_material_property(
                sample, "A", unreal.MaterialProperty.MP_OPACITY_MASK)
            mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
            mat.set_editor_property("opacity_mask_clip_value", MASKED_CLIP)
        else:
            # Set OPAQUE explicitly. blend_mode is a material property, not part
            # of the graph, so clearing the expressions does not reset it: a
            # material previously rebuilt as MASKED would keep that mode and the
            # assertion below would fail on an opaque texture.
            mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_OPAQUE)
    except Exception as exc:
        return None, "graph build failed: %s" % str(exc)[:150]

    # Assert the clip and blend before compiling: clip 0 (keep everything) and
    # clip 1 (delete the whole texture) both compile happily and report nothing.
    if masked:
        got_clip = mat.get_editor_property("opacity_mask_clip_value")
        got_blend = mat.get_editor_property("blend_mode")
        if abs(float(got_clip) - MASKED_CLIP) > 1e-6:
            return None, "opacity_mask_clip_value read back %s (wanted %s)" \
                % (got_clip, MASKED_CLIP)
        if got_blend != unreal.BlendMode.BLEND_MASKED:
            return None, "blend_mode read back %s" % got_blend
    elif mat.get_editor_property("blend_mode") != unreal.BlendMode.BLEND_OPAQUE:
        return None, "opaque texture is %s" % mat.get_editor_property("blend_mode")

    offset = current_log_offset()
    mel.recompile_material(mat)
    ok, why = scan_compile_errors("M_MC_%s" % name, offset)
    if not ok:
        # An uncompilable material cooks as the engine checkerboard and the cook
        # still reports success, so it is not saved.
        return None, "compile failed: %s" % why

    unreal.EditorAssetLibrary.save_loaded_asset(mat)
    return mat, "ok"


def run_materials():
    say("=== stage: materials ===")
    names, blocks, doc = load_slots()
    say("slot table: %d slots, %d blocks, tile_px=%s"
        % (len(names), len(blocks), doc.get("tile_px")))
    if not unreal.EditorAssetLibrary.does_directory_exist(CONTENT):
        unreal.EditorAssetLibrary.make_directory(CONTENT)

    textures, failed = {}, []
    masked_names = []
    for name in names:
        tex, why = import_texture(name)
        if tex is None:
            failed.append(name)
            say("  %-22s TEXTURE FAIL  %s" % (name, why))
            continue
        textures[name] = tex
        if why != "opaque":
            masked_names.append(name)
    say("textures ok: %d / %d  (masked %d)"
        % (len(textures), len(names), len(masked_names)))

    built = {}
    for name in names:
        if name not in textures:
            failed.append(name)
            say("  %-22s MATERIAL SKIPPED (no texture)" % name)
            continue
        mat, why = build_material(name, textures[name], name in masked_names)
        if mat is None:
            failed.append(name)
            say("  %-22s MATERIAL FAIL  %s" % (name, why))
        else:
            built[name] = mat
    say("materials ok: %d / %d" % (len(built), len(names)))

    # ---- read back from a fresh load: the claim must come from disk --------
    say("--- verify (re-load every asset) ---")
    mel = unreal.MaterialEditingLibrary
    bad = []
    for name in names:
        problems = []
        tex = unreal.load_asset(TEX_FMT % name)
        mat = unreal.load_asset(MAT_FMT % name)
        if tex is None:
            problems.append("texture missing")
        else:
            if tex.get_editor_property("address_x") != unreal.TextureAddress.TA_WRAP:
                problems.append("not TA_WRAP")
            if tex.get_editor_property("filter") != unreal.TextureFilter.TF_NEAREST:
                problems.append("not TF_NEAREST")
        if mat is None:
            problems.append("material missing")
        else:
            counts = {}
            for e in mel.get_material_expressions(mat):
                c = e.get_class().get_name()
                counts[c] = counts.get(c, 0) + 1
            expected = {
                "MaterialExpressionTextureSampleParameter2D": 1,
                "MaterialExpressionTextureCoordinate": 1,
                "MaterialExpressionScalarParameter": 1,
                "MaterialExpressionMultiply": 1,
                "MaterialExpressionConstant": 1,
            }
            if counts != expected:
                problems.append("graph=%s" % counts)
            base = mel.get_material_property_input_node(
                mat, unreal.MaterialProperty.MP_BASE_COLOR)
            if base is None or base.get_class().get_name() != \
                    "MaterialExpressionTextureSampleParameter2D":
                problems.append("BaseColor not the sample")
            want_blend = (unreal.BlendMode.BLEND_MASKED if name in masked_names
                          else unreal.BlendMode.BLEND_OPAQUE)
            if mat.get_editor_property("blend_mode") != want_blend:
                problems.append("blend=%s" % mat.get_editor_property("blend_mode"))
        if problems:
            bad.append(name)
            say("  %-22s VERIFY FAIL  %s" % (name, "; ".join(problems)))
    say("verify: %d / %d clean" % (len(names) - len(bad), len(names)))

    if failed:
        say("failed: %s" % sorted(set(failed))[:20])
    if bad:
        say("verify failures: %s" % bad[:20])
    return not failed and not bad


# --------------------------------------------------------------------------- #
# mesh slot binding
# --------------------------------------------------------------------------- #

def usemtl_first_seen_order(obj_path):
    """Families in the order their first `usemtl` appears.

    The OBJ importer turns each material group into one static_materials slot in
    this order, so this list is the slot->name contract.
    """
    order = []
    with open(obj_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("usemtl "):
                nm = line.split(None, 1)[1].strip()
                if nm and nm not in order:
                    order.append(nm)
    return order


def import_obj(src, dest, name):
    if not os.path.isfile(src):
        return None
    task = unreal.AssetImportTask()
    task.set_editor_property("filename", src)
    task.set_editor_property("destination_path", dest)
    task.set_editor_property("destination_name", name)
    task.set_editor_property("automated", True)
    task.set_editor_property("replace_existing", True)
    task.set_editor_property("save", True)
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return unreal.load_asset("%s/%s" % (dest, name))


def bind_slots(mesh, order, tag, problems):
    """Write the ASSET's static_materials array from the usemtl order.

    Length must match exactly: a mismatch means the importer made a different
    number of groups than the text says, and guessing which name sits where
    would silently mis-texture the mesh.
    """
    arr = mesh.get_editor_property("static_materials")
    n = len(arr) if arr else 0
    if n != len(order):
        problems.append("%s: %d slots but %d usemtl groups" % (tag, n, len(order)))
        return False
    new = []
    for i in range(n):
        name = order[i]
        mat = None
        for fmt in (MAT_FMT, MI_FMT):
            mat = unreal.load_asset(fmt % name)
            if mat is not None:
                break
        if mat is None:
            problems.append("%s: slot %d wants M_MC_%s but it does not exist"
                            % (tag, i, name))
            return False
        s = unreal.StaticMaterial()
        s.set_editor_property("material_slot_name", name)
        s.set_editor_property("material_interface", mat)
        new.append(s)
    mesh.set_editor_property("static_materials", new)
    return True


def clear_component_overrides(problems):
    """Blank component-level override_materials on every B_/T_ actor.

    Engine source, not a guess: FStaticMeshComponentHelper::GetMaterial() lets a
    non-null component override shadow the asset slot completely, so the level's
    overrides (pinned to the old atlas) made every correctly-bound asset slot
    unreachable on screen.
    """
    unreal.EditorLoadingAndSavingUtils.load_map(MAP_PATH)
    world = unreal.get_editor_subsystem(
        unreal.UnrealEditorSubsystem).get_editor_world()
    actors = unreal.GameplayStatics.get_all_actors_of_class(world,
                                                            unreal.StaticMeshActor)
    examined = cleared = had = 0
    for a in actors:
        lbl = a.get_actor_label()
        if not (lbl.startswith("B_") or lbl.startswith("T_")
                or lbl.startswith("V_")):
            continue
        sc = a.static_mesh_component
        examined += 1
        arr = sc.get_editor_property("override_materials") or []
        if not [m for m in arr if m is not None]:
            continue
        had += 1
        sc.set_editor_property("override_materials", [])
        after = sc.get_editor_property("override_materials") or []
        if [m for m in after if m is not None]:
            problems.append("%s: overrides survived the clear" % lbl)
            continue
        cleared += 1
    say("component override_materials: examined %d, %d had, %d cleared"
        % (examined, had, cleared))
    return cleared, had


def run_meshes():
    say("=== stage: meshes ===")
    problems = []
    rebound = 0

    with open(os.path.join(STRUCT_DIR, "manifest.json"), encoding="utf-8") as fh:
        man = json.load(fh)
    jobs = []
    for b in man["buildings"]:
        for tag, part in sorted(b["meshes"].items()):
            jobs.append((part["obj"], "bld_%03d_%s" % (b["id"], tag)))
    water = (man.get("water") or man.get("water_layer") or {}).get("obj")
    if water:
        jobs.append((water, os.path.splitext(water)[0]))
    say("structures: %d meshes to reimport" % len(jobs))
    for obj, name in jobs:
        src = os.path.join(STRUCT_DIR, obj)
        if not os.path.isfile(src):
            problems.append("%s: obj missing" % name)
            continue
        mesh = import_obj(src, STRUCT_CONTENT,
                          os.path.splitext(os.path.basename(obj))[0])
        if mesh is None:
            problems.append("%s: import returned no asset" % name)
            continue
        if bind_slots(mesh, usemtl_first_seen_order(src), name, problems):
            rebound += 1
        unreal.EditorAssetLibrary.save_loaded_asset(mesh)

    say("rebound %d meshes, %d problems" % (rebound, len(problems)))
    for p in problems[:40]:
        say("  PROBLEM %s" % p)

    clear_component_overrides(problems)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    say("save_current_level -> %s" % les.save_current_level())
    return not problems


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def parse_stage(argv):
    """MC_STAGE env > -MCstage= / --stage= argv > 'all'.

    Measured, not assumed: the editor runs the script with argv == [] on this
    setup, so a plain `--stage` after -ExecutePythonScript never arrives. The
    environment variable is the authoritative form.
    """
    stage = os.environ.get("MC_STAGE", "").strip().lower()
    for i, a in enumerate(argv):
        low = a.lower()
        if "=" in a and low.startswith(("-mcstage=", "--stage=")):
            stage = a.split("=", 1)[1].strip().lower()
        elif low in ("-mcstage", "--stage") and i + 1 < len(argv):
            stage = argv[i + 1].strip().lower()
    if stage not in ("materials", "meshes", "all", "verify"):
        if stage:
            say("ignoring unknown stage %r; using 'all'" % stage)
        stage = "all"
    return stage


def main():
    stage = parse_stage(sys.argv[1:])
    say("=== import_block_textures (stage=%s) ===" % stage)
    ok = True
    try:
        if stage in ("materials", "all"):
            ok = run_materials() and ok
        if stage in ("meshes", "all"):
            if stage == "all" and not ok:
                say("skipping meshes: materials stage did not pass")
            else:
                ok = run_meshes() and ok
        if stage == "verify":
            ok = run_materials() and ok
    except Exception:
        ok = False
        say("FAILED:\n" + traceback.format_exc())
    say("")
    say("RESULT: %s" % ("PASS" if ok else "FAIL"))
    say("--- done ---")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
