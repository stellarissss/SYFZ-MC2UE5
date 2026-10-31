# -*- coding: utf-8 -*-
"""
test_extract_structures.py -- correctness tests for the S3/S4 mesher.

The mesher's failure modes are all silent. A flipped winding, an off-by-one
face plane, or a UV that straddles two atlas cells produces a file that imports
cleanly, reports a plausible triangle count, and renders inside-out or
mis-textured. Nothing errors. So the properties are asserted here rather than
eyeballed in the viewport.

Run:  python3 tools/test_extract_structures.py
"""

import json
import os
import sys
import unittest
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import extract_structures as E                          # noqa: E402
from block_textures import slot_names, slots_of         # noqa: E402

#: 1-based slot -> original Minecraft texture name. Slot 0 is air, slot 1 is
#: the generated `other` fallback. `extract_structures.SLOT_NAMES` is
#: `slot_names()` too, so the two agree by construction; this module-level copy
#: exists so the assertions below read as data, not as a call.
SLOTS = slot_names()


def mesh(solid, mat=None, cap=4, offset=(0, 0)):
    """Mesh with one material volume reused for all three faces by default.

    The mesher takes a (top, side, bottom) triple now, so a single volume has to
    be replicated explicitly -- passing one array would unpack its first axis
    into three wrong-shaped "volumes" instead of failing.
    """
    if mat is None:
        mat = solid.astype(np.uint16)
    mats = tuple(mat) if isinstance(mat, (tuple, list)) else (mat, mat, mat)
    return E.mesh_volume(solid, solid, mats, cap, offset)


def read_obj(path):
    verts, norms, uvs, faces, mats = [], [], [], [], []
    cur = None
    for line in open(path):
        if line.startswith("v "):
            verts.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            uvs.append([float(x) for x in line.split()[1:4][:2]])
        elif line.startswith("vn "):
            norms.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("usemtl "):
            cur = line.split()[1]
        elif line.startswith("f "):
            faces.append([int(t.split("/")[0]) - 1 for t in line.split()[1:]])
            mats.append(cur)
    return (np.array(verts), np.array(norms), np.array(uvs),
            np.array(faces), mats)


class TestFaceCulling(unittest.TestCase):
    def test_single_voxel_is_six_quads(self):
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        _c, _uv, _n, quads = mesh(s)
        self.assertEqual(len(quads), 6)

    def test_no_interior_faces(self):
        """A solid 4x4x4 cube: only the 6 outer faces, never the interior."""
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:5, 1:5] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        # 6 faces, each merged into a single 4x4 quad.
        self.assertEqual(len(quads), 6)
        self.assertEqual(sum(q[2] * q[3] for q in quads), 6 * 16)

    def test_two_touching_voxels_share_no_face(self):
        """Two voxels in a row: 2 end caps + 4 merged sides = 6 quads.

        Naively it is 12 (6 each) minus the 2 shared faces, and greedy merging
        then collapses each 2x1 side into one quad. Getting 6 rather than 8 is
        the proof that the shared faces really were cancelled *and* that the
        merge ran.
        """
        s = np.zeros((4, 3, 3), bool)
        s[1, 1, 1] = True
        s[2, 1, 1] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        self.assertEqual(len(quads), 6)
        self.assertEqual(sum(q[2] * q[3] for q in quads), 4 * 2 + 2 * 1)

    def test_hollow_shell_emits_inner_faces(self):
        """A 3x3x3 shell has a cavity, so it has an inside face too."""
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        s[2, 2, 2] = False
        _c, _uv, _n, quads = mesh(s)
        # Outer 6 + the cavity's 6.
        self.assertEqual(len(quads), 12)


class TestWinding(unittest.TestCase):
    def _assert_outward(self, solid, cap=8):
        corners, _uv, normals, quads = mesh(solid, cap=cap)
        for base, _slot, _w, _h in quads:
            p0, p1, p2 = corners[base:base + 3]
            cr = np.cross(p1 - p0, p2 - p0)
            self.assertGreater(np.dot(cr, normals[base]), 0,
                               "winding disagrees with the stored normal")

    def _assert_volume(self, solid, expected_m3, cap=8):
        """Signed volume must equal the voxel count, in both triangles.

        This is the strongest orientation invariant available here, and unlike
        edge pairing it survives greedy meshing. Greedy merging emits one quad
        per rectangle, so where a long edge on one face meets two shorter
        collinear edges on the next there is a **T-junction** with no matching
        reverse edge. Those are not inversions -- the geometry is still closed
        and still correctly wound -- but they mean "every edge has an exact
        reverse" is simply the wrong assertion for a greedy mesher, and a test
        built on it reports a healthy mesh as broken.

        A flipped face anywhere makes the volume come out wrong, and summing one
        triangle per quad instead of both makes it come out exactly half, which
        is a plausible-looking wrong answer rather than an obvious one.
        """
        corners, _uv, _n, quads = mesh(solid, cap=cap)
        vol = 0.0
        for base, _s, _w, _h in quads:
            a, b, c = corners[base], corners[base + 1], corners[base + 2]
            vol += np.dot(a, np.cross(b, c)) / 6.0
            a, c, d = corners[base], corners[base + 2], corners[base + 3]
            vol += np.dot(a, np.cross(c, d)) / 6.0
        self.assertAlmostEqual(vol / 1e6, float(expected_m3), places=3,
                               msg="signed volume %.3f m3, expected %s m3"
                                   % (vol / 1e6, expected_m3))

    def test_cube(self):
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        self._assert_outward(s)
        self._assert_volume(s, 27)

    def test_shell(self):
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1:4, 1:4] = True
        s[2, 2, 2] = False
        self._assert_outward(s)
        # 27 voxels minus the 1-voxel cavity.
        self._assert_volume(s, 26)

    def test_l_shape(self):
        """An asymmetric shape: a symmetric test set cannot catch a flipped
        in-plane axis, because the error is symmetric about the diagonal."""
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:3, 1:5] = True
        s[1:3, 3:5, 1:5] = True
        self._assert_outward(s)
        # 4x2x4 slab plus a 2x2x4 arm.
        self._assert_volume(s, 32 + 16)

    def test_parity_matches_axes(self):
        """The winding decision must follow the axis parity, not a constant."""
        for d in range(3):
            other = [a for a in range(3) if a != d]
            parity = E.hand_parity([other[0], other[1], d])
            self.assertIn(parity, (-1, 1))
        self.assertEqual(E.hand_parity([0, 1, 2]), 1)
        self.assertEqual(E.hand_parity([1, 2, 0]), 1)
        self.assertEqual(E.hand_parity([0, 2, 1]), -1)


class TestGeometry(unittest.TestCase):
    def test_closed_cube_volume(self):
        """Signed volume of a closed 4x4x4 cube = 64 m^3.

        This is the only test that catches a *systematically* wrong face plane:
        a mesh can be consistently wound and still enclose the wrong volume.
        """
        s = np.zeros((6, 6, 6), bool)
        s[1:5, 1:5, 1:5] = True
        corners, _uv, _n, quads = mesh(s, cap=8)
        vol = 0.0
        for base, _s, _w, _h in quads:
            # Both triangles of the quad. Summing one gives exactly half, which
            # is a real trap: it looks like a plausible wrong answer.
            a, b, c = corners[base], corners[base + 1], corners[base + 2]
            vol += np.dot(a, np.cross(b, c)) / 6.0
            a, c, d = corners[base], corners[base + 2], corners[base + 3]
            vol += np.dot(a, np.cross(c, d)) / 6.0
        self.assertAlmostEqual(vol / 1e6, 64.0, places=3)

    def test_face_plane_position(self):
        """A single voxel's faces sit exactly on its cube's boundary planes."""
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        corners, _uv, normals, quads = mesh(s)
        seen = {}
        for base, _slot, _w, _h in quads:
            n = normals[base]
            axis = int(np.argmax(np.abs(n)))
            sign = int(np.sign(n[axis]))
            seen[(axis, sign)] = corners[base][axis]
        # Voxel (1,1,1) spans block 1..2, i.e. 100..200 cm on each axis.
        self.assertEqual(seen[(0, -1)], 100.0)
        self.assertEqual(seen[(0, +1)], 200.0)
        self.assertEqual(seen[(1, -1)], 100.0)
        self.assertEqual(seen[(1, +1)], 200.0)
        self.assertEqual(seen[(2, -1)], 100.0)
        self.assertEqual(seen[(2, +1)], 200.0)

    def test_axis_mapping(self):
        """Minecraft +z must be Unreal +Y, and block height must be Unreal Z."""
        self.assertEqual(E.MC_TO_UE, (0, 2, 1))
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        corners, _uv, normals, _q = mesh(s, offset=(10, 20))
        # x -> UE X (carrying the +10 block offset), y height -> UE Z (no
        # offset), z -> UE Y (carrying the +20 block offset). Voxel 1 spans one
        # block either side of its origin, so x = (1..2 + 10) * 100 cm.
        self.assertEqual(sorted(set(corners[:, 0])), [1100.0, 1200.0])
        self.assertEqual(sorted(set(corners[:, 1])), [2100.0, 2200.0])
        self.assertEqual(sorted(set(corners[:, 2])), [100.0, 200.0])

    def test_world_offset(self):
        s = np.zeros((3, 3, 3), bool)
        s[1, 1, 1] = True
        a, _uv, _n, _q = mesh(s, offset=(0, 0))
        b, _uv, _n, _q = mesh(s, offset=(100, -50))
        # The offset is a block coordinate, so +100 blocks on x is +10,000 cm
        # on UE X, and -50 blocks on z is -5,000 cm on UE Y.
        np.testing.assert_allclose(b - a,
                                   np.tile([10000.0, -5000.0, 0.0],
                                           (len(a), 1)))


class TestGreedyMerging(unittest.TestCase):
    def test_cap_controls_merge(self):
        s = np.zeros((9, 3, 3), bool)
        s[1:8, 1, 1] = True
        counts = {cap: len(mesh(s, cap=cap)[3]) for cap in (1, 2, 4, 8)}
        self.assertGreater(counts[1], counts[2])
        self.assertGreaterEqual(counts[2], counts[4])
        # A 7-long bar caps out at 2 merged faces plus the end caps.
        self.assertLessEqual(counts[8], 8)

    def test_merge_respects_material(self):
        """Two materials side by side must not merge into one quad.

        This is the slot-correctness test: a merged quad spanning two slots
        would stretch one texture over both, and the seam is invisible in a
        triangle count.
        """
        s = np.zeros((5, 4, 4), bool)
        s[1:4, 1, 1] = True
        mat = np.zeros((5, 4, 4), np.uint8)
        mat[1:4, 1, 1] = 1
        mat[2, 1, 1] = 2
        _c, _uv, _n, quads = mesh(s, mat=mat, cap=8)
        # The odd material out cannot merge with its neighbours on either side.
        self.assertGreaterEqual(len(quads), 4)

    def test_merge_is_greedy_not_maximal_rectangles(self):
        """An L must not be covered by two overlapping rectangles."""
        s = np.zeros((5, 5, 5), bool)
        s[1:4, 1, 1:4] = True
        s[3, 2:4, 1:4] = True
        _c, _uv, _n, quads = mesh(s, cap=8)
        # 2 faces of 3x1 + 2 faces of 1x3 + the elbow's 2 single faces.
        self.assertGreater(len(quads), 4)


class TestBlockUnitUVs(unittest.TestCase):
    """UVs are block units: exactly one texture repeat per block.

    This class used to assert "every UV lands inside one cell of the packed
    atlas". There is no atlas any more -- each slot is its own 16x16 texture,
    sampled with Wrap -- so "inside its cell" is not a property of anything and
    the layout assertions (cell overlap, atlas power-of-two) are deleted rather
    than weakened: they described a container that no longer exists.

    The failure they guarded against is still real, though. A quad whose UV
    spans more than the quad's own width/height stretches one texture over a
    merged facade instead of repeating per block, and nothing in the OBJ says
    so. That is what is asserted here: each quad's four UVs must be exactly the
    corners of [0, w] x [0, h], where w/h are the quad's block extents.
    """

    def test_uvs_span_exactly_the_quad(self):
        s = np.zeros((9, 6, 6), bool)
        s[1:8, 1:5, 1:5] = True
        mat = np.zeros((9, 6, 6), np.uint16)
        mat[1:8, 1:5, 1:5] = 1
        corners, uvs, normals, quads = mesh(s, mat=mat, cap=4)
        self.assertGreater(len(quads), 0)
        for base, slot, w, h in quads:
            got = sorted((round(float(u), 6), round(float(v), 6))
                         for u, v in uvs[base:base + 4])
            want = sorted([(0.0, 0.0), (float(w), 0.0),
                           (float(w), float(h)), (0.0, float(h))])
            self.assertEqual(got, want,
                             "quad %d (%dx%d) UVs %s do not span one repeat "
                             "per block" % (base // 4, w, h, got))

    def test_every_quad_carries_a_legal_slot(self):
        """A slot of 0 would name SLOTS[-1]; past the end would IndexError."""
        s = np.zeros((9, 6, 6), bool)
        s[1:8, 1:5, 1:5] = True
        mat = np.zeros((9, 6, 6), np.uint16)
        mat[1:8, 1:5, 1:5] = 7
        _c, _uv, _n, quads = mesh(s, mat=mat, cap=8)
        for _base, slot, _w, _h in quads:
            self.assertGreaterEqual(int(slot), 1)
            self.assertLessEqual(int(slot), len(SLOTS))

    def test_slot_table_is_1_based_and_unique(self):
        """Slot 1 is the fallback and no two slots share a texture name.

        Duplicates would mean two slots that must be textured identically --
        a silent doubling of the import work and of the material count.
        """
        self.assertEqual(SLOTS[0], "other")
        self.assertEqual(len(set(SLOTS)), len(SLOTS),
                         "duplicate slot names in the table")
        for name in ("minecraft:stone", "minecraft:grass_block",
                     "minecraft:water", "minecraft:oak_planks"):
            for slot in slots_of(name):
                self.assertTrue(1 <= slot <= len(SLOTS),
                                "%s -> slot %d outside 1..%d"
                                % (name, slot, len(SLOTS)))


class TestMaterialVolume(unittest.TestCase):
    """The voxel -> per-face slot map, checked for internal consistency.

    These exist because of a specific bug: the block name was resolved only on
    the first chunk that used a given palette index, so on every later chunk the
    name leaked from the previous iteration. A voxel's `nameid` and its material
    slot then came from two different blocks and nothing detected it -- the
    water layer rendered with brick, rock and wood while still being *called*
    water. Asserting that name and slot agree catches that class of bug without
    needing to know the right answer for every block.

    The cache now stores three slot volumes (top / side / bottom) rather than one
    19-family code, so every assertion here is made on all three: a face that
    disagrees with its siblings about whether the voxel exists is the same class
    of silent wrong answer.
    """

    CACHE = "out/materials/voxelmat.npz"

    def setUp(self):
        if not os.path.isfile(self.CACHE):
            self.skipTest("run tools/extract_structures.py first")
        self.z = np.load(self.CACHE, allow_pickle=False)
        self.names = [str(s) for s in self.z["names"]]
        self.mats = (self.z["top"], self.z["side"], self.z["bottom"])

    def test_every_solid_voxel_has_a_legal_slot_on_all_three_faces(self):
        """top/side/bottom must agree on occupancy and all be 1..len(SLOTS).

        Slot 0 is air, so a solid voxel carrying 0 on any face would be
        textured with whatever happens to sit at index -1 -- silently, since
        SLOTS[-1] is a perfectly valid name.
        """
        solid = self.mats[0] != 0
        self.assertGreater(int(solid.sum()), 0)
        n = len(SLOTS)
        for label, vol in zip(("top", "side", "bottom"), self.mats):
            np.testing.assert_array_equal(
                vol != 0, solid,
                err_msg="%s disagrees with top about which voxels exist" % label)
            filled = vol[solid]
            self.assertGreaterEqual(int(filled.min()), 1,
                                    "%s has slot 0 on a solid voxel" % label)
            self.assertLessEqual(int(filled.max()), n,
                                 "%s has slot %d, table has %d entries"
                                 % (label, int(filled.max()), n))

    def test_slot_matches_name_for_every_voxel(self):
        nameid = self.z["nameid"]
        top, side, bottom = self.mats
        solid = top != 0
        # Every distinct (nameid, top, side, bottom) combination actually used.
        quads = np.unique(np.stack([nameid[solid], top[solid],
                                    side[solid], bottom[solid]], axis=1), axis=0)
        self.assertGreater(len(quads), 50, "suspiciously few name/slot tuples")
        bad = []
        for nid, t, s, b in quads:
            name = self.names[int(nid)]
            # Re-derived from the block NAME, not from the palette, so a leak
            # between two palette entries shows up as a mismatch here.
            expected = tuple(SLOTS[v - 1] for v in slots_of(name))
            got = (SLOTS[int(t) - 1], SLOTS[int(s) - 1],
                   SLOTS[int(b) - 1])
            if got != expected:
                bad.append((name, got, expected))
        self.assertEqual(bad, [],
                         "%d block names carry the wrong texture slot: %s"
                         % (len(bad), bad[:8]))

    def test_the_generated_fallback_slot_is_never_sampled(self):
        """Slot 1 `other` is generated, not from the save, so nothing may use it.

        The inverted form of the old "generated families are reachable" test.
        `other` used to be the landing place for blocks the family mappers did
        not know, so *not* seeing it would have meant the fallback was dead
        weight. Now `other` exists only as a visible "unresolved" grey, and the
        table's whole job is to resolve every campus block to real art: a voxel
        on slot 1 means a block name fell through the manifest and would render
        as a flat neutral block. That is a bug, so the assertion is that it does
        not happen -- while the fallback still answers for an unknown name.
        """
        used = {int(v) for vol in self.mats for v in np.unique(vol)}
        self.assertIn(0, used, "no air in the volume; the cache looks wrong")
        self.assertNotIn(1, used,
                         "fallback slot 1 (%r) is sampled by real voxels"
                         % SLOTS[0])
        self.assertEqual(slots_of("minecraft:this_block_does_not_exist"),
                         (1, 1, 1), "the fallback path no longer resolves")

    def test_water_voxels_use_the_water_slot(self):
        role_path = "out/classify/rolevolume.npy"
        if not os.path.isfile(role_path):
            self.skipTest("no role volume")
        role = np.load(role_path)
        w = role == E.WATER
        self.assertGreater(int(w.sum()), 0)
        want = {SLOTS[s - 1] for s in slots_of("minecraft:water")}
        got = set()
        for vol in self.mats:
            got |= {SLOTS[int(f) - 1] for f in np.unique(vol[w]) if f}
        self.assertEqual(got, want)


class TestOutputFiles(unittest.TestCase):
    """Checks against the real emitted files, skipped if not yet built."""

    MANIFEST = "out/structures/manifest.json"

    def setUp(self):
        if not os.path.isfile(self.MANIFEST):
            self.skipTest("run tools/extract_structures.py first")
        import json
        self.man = json.load(open(self.MANIFEST))

    def test_bbox_matches_structures_json(self):
        bad = [b["id"] for b in self.man["buildings"]
               if not b["bbox_matches_structures_json"]]
        self.assertEqual(bad, [],
                         "buildings whose mesh bbox moved: %s" % bad)

    def test_every_obj_has_vertex_normals(self):
        """The v/vt/vn triple is mandatory.

        reimport_terrain.py records the cost of omitting it: the importer
        silently discarded ~90% of the geometry.
        """
        for b in self.man["buildings"]:
            for part in b["meshes"].values():
                path = os.path.join("out/structures", part["obj"])
                if not os.path.isfile(path):
                    continue
                # vt/vn/usemtl come after every vertex, so a mesh with more
                # than a few thousand vertices pushes them well past any prefix
                # worth reading. Count the records instead of sampling -- and
                # report counts, never file contents, so a failure does not
                # dump a megabyte of vertices into the log.
                counts = {"v": 0, "vt": 0, "vn": 0, "f": 0, "usemtl": 0}
                with open(path) as fh:
                    for line in fh:
                        tag = line.split(" ", 1)[0]
                        if tag in counts:
                            counts[tag] += 1
                self.assertGreater(counts["v"], 0, path)
                self.assertEqual(counts["vt"], counts["v"], path)
                self.assertEqual(counts["vn"], counts["v"], path)
                self.assertEqual(counts["f"], counts["v"] // 4 * 2, path)
                self.assertGreater(counts["usemtl"], 0,
                                   "%s has faces but no material" % path)

    def test_structure_and_detail_partition_the_buildings(self):
        """The 88 buildings partition exactly; the rest is reported, not lost."""
        blk = self.man["blocks"]
        self.assertEqual(blk["structure"] + blk["detail"],
                         blk["in_reported_buildings"])
        self.assertEqual(blk["in_reported_buildings"]
                         + blk["unclaimed_fragments"], blk["labelled_total"])

    def test_triangle_reduction_is_real(self):
        t = self.man["triangles"]
        self.assertLess(t["all_outputs"], t["naive_12_per_block_same_voxels"])
        self.assertLess(t["all_outputs"], t["old_cube_layer"])

    def test_water_is_emitted(self):
        self.assertIsNotNone(self.man["water"]["obj"])
        self.assertGreater(self.man["water"]["blocks"], 0)

    def test_water_layer_uses_only_the_water_slot(self):
        """A water face textured with brick is the stale-cache bug, visible.

        `build_material_volume` caches the voxel->slot map. When a mapping
        rule changes and the cache is not invalidated, the old map keeps being
        served and every run still reports success -- the water layer came out
        carrying brick, rock and wood because of exactly that. One assertion on
        the emitted material list turns a silent wrong answer into a failure.
        """
        want = sorted({SLOTS[s - 1] for s in slots_of("minecraft:water")})
        self.assertEqual(self.man["water"]["material_slots"], want)

    def test_material_cache_is_not_stale(self):
        """The cache must record the inputs that produced it."""
        cache = "out/materials/voxelmat.npz"
        if not os.path.isfile(cache):
            self.skipTest("no material cache")
        import numpy as np
        z = np.load(cache, allow_pickle=False)
        self.assertIn("stamp", z.files,
                      "cache has no stamp; it cannot know it is stale")
        stamp = json.loads(str(z["stamp"][0]))
        self.assertEqual(stamp["slot_names"], slot_names())
        self.assertEqual(stamp["campus"], list(E.CAMPUS))


if __name__ == "__main__":
    unittest.main(verbosity=2)