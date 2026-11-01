# -*- coding: utf-8 -*-
"""
probe_unclaimed_top.py -- why does a solid block get no mesh at all?

`qa_obj_top_vs_truth.py` says which columns the level does not draw, and its
owner table says the faces that do reach them come from *below* the save's top
block. That leaves one question: the blocks themselves are solid, so why is no
face emitted for them?

`extract_structures.py` meshes two masks per building:

    labels == oid  AND  role == STRUCTURE
    labels == oid  AND  role == DETAIL

so a voxel with a building role but no building label is meshed by nobody --
it is in `occupied` (every face around it is culled as if it were drawn) and in
no `solid`, which is the worst of both: invisible AND suppressing its
neighbours' faces. This tool counts exactly that population and prints the
blocks responsible.

Reads only: out/classify/rolevolume.npy, out/classify/structure_labels.npy,
out/classify/structures.json, out/materials/voxelmat.npz.

Usage
  python tools/probe_unclaimed_top.py [--top-y 999]
"""

import argparse
import collections
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import classify                                                  # noqa: E402
from reference_map import CAMPUS, MATERIAL_CACHE                 # noqa: E402

ROLE_NAMES = {getattr(classify, n): n for n in
              ("EMPTY", "TERRAIN", "STRUCTURE", "VEGETATION", "WATER",
               "DETAIL")}


def say(msg, lines=None):
    print(msg)
    if lines is not None:
        lines.append(str(msg))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--classify", default=os.path.join(REPO, "out/classify"))
    ap.add_argument("--top-y", type=int, default=999,
                    help="only report columns whose top block is at or below "
                         "this y (the campus top is ~72; default reports all)")
    ap.add_argument("--masks", default=os.path.join(
                        REPO, "out/qa/obj_top_vs_truth_masks.npz"),
                    help="masks written by qa_obj_top_vs_truth.py; explains "
                         "exactly the columns that tool flagged")
    args = ap.parse_args(argv)

    lines = []
    role = np.load(os.path.join(args.classify, "rolevolume.npy"))
    labels = np.load(os.path.join(args.classify, "structure_labels.npy"))
    with open(os.path.join(args.classify, "structures.json")) as fh:
        structures = json.load(fh)
    known = {int(o["id"]) for o in structures}

    z = np.load(os.path.join(REPO, MATERIAL_CACHE), allow_pickle=False)
    top_vol = z["top"]
    nameid = z["nameid"]
    names = [str(s) for s in z["names"]]
    W, H, D = top_vol.shape
    say("campus %s  volume %dx%dx%d" % (list(CAMPUS), W, H, D), lines)

    # ---- the whole volume: who owns what ---------------------------------- #
    building = (role == classify.STRUCTURE) | (role == classify.DETAIL)
    known_mask = np.isin(labels, list(known))
    # "Unclaimed" is a label that structures.json does not list, NOT label 0.
    # The two are different populations and the distinction is the whole point
    # of this tool: `structure_labels.npy` holds every connected component the
    # classifier found (422 of them), while `structures.json` keeps only the
    # ones it agreed to call buildings (88). Everything in between is meshed by
    # nobody yet still sits in `occupied`, so it is invisible AND it culls the
    # faces of the blocks around it. An earlier version of this file tested
    # `labels == 0` here, found nothing (label 0 owns zero voxels), and
    # reported PASS while 3,436 voxels were being dropped.
    unclaimed = building & ~known_mask

    say("", lines)
    say("voxels with a building role: %d" % int(building.sum()), lines)
    say("  claimed by a building in structures.json : %d"
        % int((building & known_mask).sum()), lines)
    say("  label 0 (no component)                   : %d"
        % int((building & (labels == 0)).sum()), lines)
    say("  labelled but NOT in structures.json      : %d  %.2f%%"
        % (int(unclaimed.sum()),
           100.0 * unclaimed.sum() / max(1, building.sum())), lines)
    present = sorted(set(np.unique(labels).tolist()) - {0})
    orphan_labels = [x for x in present if x not in known]
    say("  distinct labels in the volume            : %d" % len(present),
        lines)
    say("  ... of which absent from structures.json : %d"
        % len(orphan_labels), lines)

    # ---- the columns the level failed to draw ----------------------------- #
    top_y = np.full((W, D), -1, dtype=np.int16)
    for y in range(H):
        top_y = np.where(top_vol[:, y, :] != 0, y, top_y)
    top_y = np.where(top_y <= args.top_y, top_y, -1)

    # A column is "unclaimed at the top" when its highest block is a building
    # block that no building mesh will ever emit.
    top_unclaimed = np.zeros((W, D), bool)
    for y in range(H):
        top_unclaimed |= (top_y == y) & unclaimed[:, y, :]

    say("", lines)
    say("columns whose top block is unclaimed: %d"
        % int(top_unclaimed.sum()), lines)
    if top_unclaimed.any():
        say("  (= the `missing` population of qa_obj_top_vs_truth.py, plus the"
            " `wrong` ones whose real roof never existed)", lines)

    blk = collections.Counter()
    yy = collections.Counter()
    for r, c in zip(*np.nonzero(top_unclaimed)):
        y = int(top_y[r, c])
        blk[names[int(nameid[r, y, c])]] += 1
        yy[y] += 1
    say("", lines)
    say("## unclaimed top blocks by block name", lines)
    for b, n in blk.most_common(30):
        say("  %-36s %6d" % (b, n), lines)
    say("", lines)
    say("## ... by height", lines)
    for y, n in sorted(yy.items()):
        say("  y=%-4d %6d" % (y, n), lines)

    # Which orphan labels are these? One label with 800 columns is a filter
    # threshold to relax; 800 labels with one column each is a rule to change.
    lab_of_top = collections.Counter()
    for r, c in zip(*np.nonzero(top_unclaimed)):
        y = int(top_y[r, c])
        lab_of_top[int(labels[r, y, c])] += 1
    if lab_of_top:
        say("", lines)
        say("## orphan labels behind those columns (%d distinct)"
            % len(lab_of_top), lines)
        for lb, n in lab_of_top.most_common(20):
            say("  label %-5d %6d columns" % (lb, n), lines)

    # ---- is it the label pass or the role pass? --------------------------- #
    # A voxel can also be building-role, correctly labelled, and still never
    # drawn if the mesher's own mask disagrees -- but the masks are derived
    # from exactly these two arrays, so if this table is empty the bug is
    # downstream of classification.
    say("", lines)
    say("## component sizes of the unclaimed population (3-connectivity)", lines)
    try:
        from scipy import ndimage
        lab, n = ndimage.label(unclaimed)
        sizes = np.bincount(lab.ravel())[1:]
        if sizes.size:
            big = np.sort(sizes)[::-1][:10]
            say("  %d components, sizes: %s" % (n, ", ".join(str(int(s))
                                                            for s in big)),
                lines)
            objs = ndimage.find_objects(lab)
            order = np.argsort(sizes)[::-1][:5]
            for i in order:
                sl = objs[i]
                say("  comp %d: %d voxels, x[%d..%d] y[%d..%d] z[%d..%d]"
                    % (i + 1, int(sizes[i]),
                       sl[0].start + CAMPUS[0], sl[0].stop - 1 + CAMPUS[0],
                       sl[1].start, sl[1].stop - 1,
                       sl[2].start + CAMPUS[2], sl[2].stop - 1 + CAMPUS[2]),
                    lines)
    except ImportError:
        say("  scipy missing, skipped", lines)

    # ---- explain the columns the comparison tool flagged ------------------ #
    if os.path.isfile(args.masks):
        m = np.load(args.masks, allow_pickle=False)
        top_vol_full = z["top"]
        say("", lines)
        say("## the flagged columns, voxel by voxel", lines)
        for label in ("missing", "wrong"):
            mask = m[label]
            say("", lines)
            say("-- %s: %d columns" % (label, int(mask.sum())), lines)
            rows, cols = np.nonzero(mask)
            # A flagged column has a real block at top_y. The question is what
            # sits *above* it, because that is what culled its roof face:
            # `mesh_volume` emits a +Y face only where `occupied` is clear, so
            # anything with a role but no material silently suppresses the face
            # below it and draws nothing itself.
            above_occ = above_slot0 = above_mat0 = 0
            above_role = collections.Counter()
            above_name = collections.Counter()
            for r, c in zip(rows, cols):
                y = int(m["top_y"][r, c])
                if y < 0 or y + 1 >= H:
                    continue
                if role[r, y + 1, c] != 0:
                    above_occ += 1
                    above_role[int(role[r, y + 1, c])] += 1
                    nm = names[int(nameid[r, y + 1, c])]
                    above_name[nm] += 1
                    if top_vol_full[r, y + 1, c] == 0:
                        above_slot0 += 1
                else:
                    above_mat0 += 1
            n = max(1, len(rows))
            say("   block above the save's top block:", lines)
            say("     occupied (role != 0)        : %5d  %5.1f%%"
                % (above_occ, 100.0 * above_occ / n), lines)
            say("       ... of those, top slot == 0 : %5d  (draws nothing AND "
                "culls the face below)" % above_slot0, lines)
            say("     clear (role == 0)           : %5d  %5.1f%%"
                % (above_mat0, 100.0 * above_mat0 / n), lines)
            if above_name:
                say("     the blockers are:", lines)
                for nm, k in above_name.most_common(8):
                    say("       %-34s %5d" % (nm, k), lines)

    out = os.path.join(REPO, "out/qa/unclaimed_top.txt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    say("", lines)
    say("wrote %s" % out, lines)
    total = int(top_unclaimed.sum())
    say("", lines)
    say("RESULT: %s -- %d columns have a top block no building mesh can emit"
        % ("PASS" if total == 0 else "FAIL", total), lines)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
