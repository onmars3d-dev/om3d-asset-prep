# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake Prep core: pair _low and _high objects by name, check each pair,
explode pairs apart, and write bake-group collections.

Pairing keys off the naming convention Scene Prep enforces:
    SM_<Asset>_<Part>_low   pairs with   SM_<Asset>_<Part>_high[_<Tag>]
so one low can have several highs (_high, _high_bolts, _high_trim).
"""
from dataclasses import dataclass, field

import bpy
from mathutils import Vector

from . import mesh_checks
from . import naming

STACKED_PROP = "om3d_stacked_uvs"     # on a low: its overlapping UVs are on purpose
EXPLODE_PROP = "om3d_explode_offset"  # world offset added by Explode, removed by Reset
GROUP_PROP = "om3d_bake_group"        # marks collections Bake Groups created

AXES = {"X": 0, "Y": 1, "Z": 2}


@dataclass
class Pair:
    asset: str
    part: str
    base: str                                  # SM_<Asset>_<Part>, the bake group name
    lows: list = field(default_factory=list)   # objects
    highs: list = field(default_factory=list)
    problems: list = field(default_factory=list)


@dataclass
class PairResult:
    pairs: list = field(default_factory=list)          # complete pairs, sorted by base
    lone_lows: list = field(default_factory=list)      # Pair with no highs
    lone_highs: list = field(default_factory=list)     # Pair with no lows
    off_convention: list = field(default_factory=list)  # mesh objects whose name doesn't parse

    def assets(self):
        return sorted({p.asset for p in self.pairs})


def base_name(conv, asset, part):
    return conv.make(asset, part, role=None)


def find_pairs(objects, conv, check=True):
    """Group mesh objects into low/high pairs by name."""
    groups = {}
    res = PairResult()
    for obj in objects:
        if obj.type != "MESH" or naming.is_collision_name(obj.name):
            continue
        info = conv.parse(naming.strip_dup_suffix(obj.name))
        if info is None or info["role"] is None:
            res.off_convention.append(obj)
            continue
        key = (info["asset"], info["part"] or "")
        pair = groups.get(key)
        if pair is None:
            pair = groups[key] = Pair(info["asset"], info["part"] or "",
                                      base_name(conv, info["asset"], info["part"]))
        (pair.lows if info["role"] == "low" else pair.highs).append(obj)
    for key in sorted(groups):
        pair = groups[key]
        pair.lows.sort(key=lambda o: o.name)
        pair.highs.sort(key=lambda o: o.name)
        if pair.lows and pair.highs:
            if check:
                pair.problems = check_pair(pair)
            res.pairs.append(pair)
        elif pair.lows:
            res.lone_lows.append(pair)
        else:
            res.lone_highs.append(pair)
    res.off_convention.sort(key=lambda o: o.name)
    return res


# --------------------------------------------------------------------------
# Per-pair checks
# --------------------------------------------------------------------------

def world_bounds(objects):
    """(min, max) world-space corners of the objects' bounding boxes, or None."""
    lo = Vector((1e30, 1e30, 1e30))
    hi = Vector((-1e30, -1e30, -1e30))
    found = False
    for obj in objects:
        mw = obj.matrix_world
        for c in obj.bound_box:
            p = mw @ Vector(c)
            for i in range(3):
                lo[i] = min(lo[i], p[i])
                hi[i] = max(hi[i], p[i])
            found = True
    return (lo, hi) if found else None


def coverage(low_bounds, high_bounds):
    """Worst per-axis share of the low's bounding box that the highs' box covers (0..1).
    Flat axes (a plane's thickness) only need the high to reach that height."""
    (l0, l1), (h0, h1) = low_bounds, high_bounds
    diag = (l1 - l0).length or 1.0
    slack = diag * 0.02  # a high a hair smaller than the low still covers it
    worst = 1.0
    for i in range(3):
        ext = l1[i] - l0[i]
        if ext <= diag * 1e-3:
            mid = (l0[i] + l1[i]) * 0.5
            share = 1.0 if h0[i] - slack <= mid <= h1[i] + slack else 0.0
        else:
            share = max(0.0, min(l1[i], h1[i] + slack) - max(l0[i], h0[i] - slack)) / ext
        worst = min(worst, share)
    return worst


def _rot_scale(obj):
    _loc, rot, scale = obj.matrix_world.decompose()
    return rot, scale


def check_pair(pair):
    """Short problem strings for one pair (empty list = ready to bake)."""
    out = []
    for low in pair.lows:
        me = low.data
        if not me.uv_layers:
            out.append(f"{low.name}: no UV map")
        elif not low.get(STACKED_PROP):
            bm = mesh_checks_bmesh(low)
            try:
                n = len(mesh_checks.uv_overlap(bm).get("faces", ()))
            finally:
                bm.free()
            if n:
                out.append(f"{low.name}: {n} overlapping UV faces")
    lb = world_bounds(pair.lows)
    hb = world_bounds(pair.highs)
    if lb and hb:
        share = coverage(lb, hb)
        if share <= 0.0:
            out.append("High doesn't overlap the low (moved or exploded apart?)")
        elif share < 0.8:
            out.append(f"High only covers {round(share * 100)}% of the low")
    ref = pair.lows[0]
    r0, s0 = _rot_scale(ref)
    for obj in pair.lows[1:] + pair.highs:
        r, s = _rot_scale(obj)
        if r0.rotation_difference(r).angle > 1e-3 or (s - s0).length > 1e-3 * max(s0.length, 1e-6):
            out.append(f"{obj.name}: rotation/scale differs from {ref.name}")
    return out


def mesh_checks_bmesh(obj):
    import bmesh
    bm = bmesh.new()
    if obj.mode == "EDIT":
        bm.free()
        bm = bmesh.from_edit_mesh(obj.data).copy()
    else:
        bm.from_mesh(obj.data)
    return bm


# --------------------------------------------------------------------------
# Explode / reset
# --------------------------------------------------------------------------

def _movers(objects):
    """Objects to move, skipping any whose parent is also moving (it follows the parent)."""
    s = set(objects)
    out = []
    for o in objects:
        p = o.parent
        skip = False
        while p is not None:
            if p in s:
                skip = True
                break
            p = p.parent
        if not skip:
            out.append(o)
    return out


def _shift(obj, delta):
    mw = obj.matrix_world.copy()
    mw.translation += delta
    obj.matrix_world = mw
    old = Vector(obj.get(EXPLODE_PROP, (0.0, 0.0, 0.0)))
    new = old + delta
    if new.length < 1e-9:
        if EXPLODE_PROP in obj:
            del obj[EXPLODE_PROP]
    else:
        obj[EXPLODE_PROP] = tuple(new)


def explode(pairs, axis="X", gap=None):
    """Line pairs up along an axis, each pair's box clear of the last.
    Returns the number of pairs moved. Run Reset first to start from the original layout."""
    i = AXES[axis]
    boxes = []
    for p in pairs:
        objs = p.lows + p.highs
        b = world_bounds(objs)
        if b:
            boxes.append((p, objs, b))
    if not boxes:
        return 0
    if gap is None or gap <= 0:
        biggest = max((b[1] - b[0]).length for _p, _o, b in boxes)
        gap = biggest * 0.25
    cursor = boxes[0][2][0][i]
    moved = 0
    for p, objs, (b0, b1) in boxes:
        delta = Vector((0.0, 0.0, 0.0))
        delta[i] = cursor - b0[i]
        if abs(delta[i]) > 1e-9:
            for o in _movers(objs):
                _shift(o, delta)
            moved += 1
        cursor += (b1[i] - b0[i]) + gap
    return moved


def reset_explode(objects):
    """Undo Explode on these objects. Returns how many moved back."""
    n = 0
    for o in _movers([o for o in objects if EXPLODE_PROP in o]):
        mw = o.matrix_world.copy()
        mw.translation -= Vector(o[EXPLODE_PROP])
        o.matrix_world = mw
        del o[EXPLODE_PROP]
        n += 1
    # children of moved parents carry a stale offset of their own only if they
    # were moved separately; clear any leftovers so Reset is idempotent
    for o in objects:
        if EXPLODE_PROP in o:
            del o[EXPLODE_PROP]
    return n


def exploded(objects):
    return any(EXPLODE_PROP in o for o in objects)


# --------------------------------------------------------------------------
# Bake group collections
# --------------------------------------------------------------------------

ROOT_NAME = "Bake Groups"


def write_bake_groups(scene, pairs, move=False):
    """One collection per pair (named for the Marmoset bake group) under a 'Bake Groups'
    collection. Objects are linked in, so your own collections stay as they are
    (move=True moves them instead). Rebuilds the collections it made last time."""
    root = bpy.data.collections.get(ROOT_NAME)
    if root is None:
        root = bpy.data.collections.new(ROOT_NAME)
        root[GROUP_PROP] = True
    if root.name not in scene.collection.children:
        scene.collection.children.link(root)
    for child in list(root.children):
        if child.get(GROUP_PROP):
            for o in list(child.objects):
                if len(o.users_collection) == 1:  # don't orphan an object
                    scene.collection.objects.link(o)
                child.objects.unlink(o)
            bpy.data.collections.remove(child)
    made = 0
    for p in pairs:
        col = bpy.data.collections.new(p.base)
        col[GROUP_PROP] = True
        root.children.link(col)
        for o in p.lows + p.highs:
            if move:
                for c in list(o.users_collection):
                    c.objects.unlink(o)
            if o.name not in col.objects:
                col.objects.link(o)
        made += 1
    return root, made


# --------------------------------------------------------------------------
# Name Pairs: match highs to lows by position, then name both to the convention
# --------------------------------------------------------------------------

import re as _re

_ROLE = _re.compile(r"(?i)^(low|lo|lp|lowpoly|high|hi|hp|highpoly)$")
_NUM = _re.compile(r"^\d+$")
SAMPLES = 256


def _words(name):
    return [w for w in _re.split(r"[^A-Za-z0-9]+", naming.strip_dup_suffix(name)) if w]


def _camel(words):
    return "".join(w[:1].upper() + w[1:] for w in words)


def part_from_name(name):
    """Meaningful part name from an old object name, or '' when it's junk
    ('Cylinder.015' -> '', 'Scale_Viewfinder.014' -> 'ScaleViewfinder')."""
    if naming.is_junk_name(name):
        return ""
    words = [w for w in _words(name) if not _ROLE.match(w) and not _NUM.match(w)]
    return _camel(words)


def tag_from_name(name, low_name=None):
    """Extra words on a high beyond its base ('Cube.001_screw_01' -> 'Screw01').
    The base is a generic first word (Cube, pCube...) or the words of its low's name;
    the numbers right after it (.001, 015) belong to the base too."""
    words = [w for w in _words(name) if not _ROLE.match(w)]
    low_words = [w.lower() for w in _words(low_name or "") if not _ROLE.match(w)
                 and not _NUM.match(w)]
    if words and (naming.BLENDER_DEFAULTS.match(words[0])
                  or any(p.match(words[0]) for p in naming.JUNK_PATTERNS)):
        words = words[1:]
    else:
        k = 0
        while words and k < len(low_words):
            if _NUM.match(words[0]):
                words = words[1:]
            elif words[0].lower() == low_words[k]:
                words = words[1:]
                k += 1
            else:
                break
    while words and _NUM.match(words[0]):
        words = words[1:]
    return _camel(words)


def _eval_mesh_points(obj, depsgraph):
    """World-space verts and polygons of the evaluated mesh (modifiers applied)."""
    ev = obj.evaluated_get(depsgraph)
    me = ev.to_mesh()
    try:
        mw = obj.matrix_world
        verts = [mw @ v.co for v in me.vertices]
        polys = [tuple(p.vertices) for p in me.polygons]
    finally:
        ev.to_mesh_clear()
    return verts, polys


def _world_bvh(obj, depsgraph):
    from mathutils.bvhtree import BVHTree
    verts, polys = _eval_mesh_points(obj, depsgraph)
    if not polys:
        return None
    return BVHTree.FromPolygons(verts, polys)


def _samples(obj, depsgraph):
    verts, _p = _eval_mesh_points(obj, depsgraph)
    step = max(1, len(verts) // SAMPLES)
    return verts[::step]


def _inside(p, box, pad):
    lo, hi = box
    return all(lo[i] - pad <= p[i] <= hi[i] + pad for i in range(3))


@dataclass(eq=False)
class Match:
    high: object
    low: object = None
    dist: float = 0.0
    runner_up: object = None
    close_call: bool = False
    reason: str = ""
    scores: dict = field(default_factory=dict)  # low -> median distance


def match_highs(lows, highs):
    """For each high, the low whose surface its verts sit closest to.
    Candidates are lows whose (padded) box holds at least half the high's sample verts;
    the score is the median distance from those verts to the low's surface."""
    dg = bpy.context.evaluated_depsgraph_get()
    info = []
    for lo in lows:
        b = world_bounds([lo])
        tree = _world_bvh(lo, dg) if b is not None else None
        if tree is None:
            continue
        info.append((lo, b, (b[1] - b[0]).length, tree))
    out = []
    for h in highs:
        m = Match(h)
        pts = _samples(h, dg)
        hb = world_bounds([h])
        if not pts or hb is None:
            m.reason = "no geometry"
            out.append(m)
            continue
        hdiag = (hb[1] - hb[0]).length
        cands = [x for x in info
                 if sum(_inside(p, x[1], x[2] * 0.1) for p in pts) >= len(pts) * 0.5]
        if not cands:
            cands = info
        scored = []
        for lo, _b, ldiag, tree in cands:
            ds = []
            for p in pts:
                hit = tree.find_nearest(p)
                ds.append(hit[3] if hit[0] is not None else 1e30)
            ds.sort()
            scored.append((ds[len(ds) // 2], ldiag, lo))
        scored.sort(key=lambda s: (s[0], s[1]))
        m.scores = {lo: d for d, _ld, lo in scored}
        if not scored:
            m.reason = "no lows to match"
            out.append(m)
            continue
        best = scored[0]
        if best[0] > max(hdiag, best[1] * 0.05) * 0.5:
            m.reason = "no low close enough"
            m.dist = best[0]
            out.append(m)
            continue
        m.low, m.dist = best[2], best[0]
        if len(scored) > 1:
            m.runner_up = scored[1][2]
            m.close_call = scored[1][0] <= best[0] * 1.25 + best[1] * 1e-4
        out.append(m)
    return out


@dataclass
class NameEntry:
    low: object
    new_low: str
    highs: list = field(default_factory=list)   # [(Match, new name)]


@dataclass
class NamePlan:
    entries: list = field(default_factory=list)
    unmatched: list = field(default_factory=list)  # Match with no low
    asset: str = ""


def plan_names(lows, highs, conv, asset, keep_names=True):
    """Work out the new names. Nothing changes until apply_names()."""
    lows = sorted([o for o in lows if o.type == "MESH"], key=lambda o: o.name)
    highs = sorted([o for o in highs if o.type == "MESH"], key=lambda o: o.name)
    matches = match_highs(lows, highs)
    plan = NamePlan(asset=asset)
    by_low = {lo: [] for lo in lows}
    for m in matches:
        if m.low is None:
            plan.unmatched.append(m)
        else:
            by_low[m.low].append(m)
    _fill_empty_lows(lows, by_low)
    moving = set(lows) | {m.high for m in matches if m.low is not None}
    taken = {n for n in bpy.data.objects.keys() if bpy.data.objects[n] not in moving}
    used = set()
    counter = [0]

    def free(name):
        return name not in taken and name not in used

    for lo in lows:
        part = ""
        if keep_names:
            info = conv.parse(naming.strip_dup_suffix(lo.name))
            part = (info["part"] or info["asset"]) if info else part_from_name(lo.name)
        if not part:
            counter[0] += 1
            part = f"Part{counter[0]:02d}"
        base_part, n = part, 2
        while not free(conv.make(asset, part, "low")) or not free(conv.make(asset, part, "high")):
            part = f"{base_part}{n:02d}"
            n += 1
        entry = NameEntry(lo, conv.make(asset, part, "low"))
        used.add(entry.new_low)
        ms = by_low[lo]
        # main high: the untagged one with the biggest box; the rest get a tag
        def vol(m):
            b = world_bounds([m.high])
            d = b[1] - b[0]
            return d.x * d.y * d.z + d.length * 1e-6
        tags = {m: tag_from_name(m.high.name, lo.name) for m in ms}
        untagged = sorted([m for m in ms if not tags[m]], key=vol, reverse=True)
        tagged = sorted([m for m in ms if tags[m]], key=lambda m: m.high.name)
        order = untagged + tagged
        extra = 1
        for i, m in enumerate(order):
            if i == 0 and m in untagged:
                tag = None
            else:
                tag = tags[m]
                if not tag:
                    extra += 1
                    tag = f"{extra:02d}"
            name = conv.make(asset, part, "high", tag)
            k = 2
            while not free(name):
                name = conv.make(asset, part, "high", f"{tag or ''}{k:02d}")
                k += 1
            used.add(name)
            entry.highs.append((m, name))
        plan.entries.append(entry)
    return plan


def _fill_empty_lows(lows, by_low, ratio=3.0):
    """A low left with no high takes an extra (untagged) high from a low that has two or
    more, when that high sits nearly as close to it. Marked as a close call to review."""
    for lo in lows:
        if by_low[lo]:
            continue
        best = None
        for other, ms in by_low.items():
            untagged = [m for m in ms if not tag_from_name(m.high.name, other.name)]
            if len(untagged) < 2:
                continue
            for m in untagged:
                d = m.scores.get(lo)
                if d is None or d > max(m.dist, 1e-9) * ratio:
                    continue
                r = d / max(m.dist, 1e-9)
                if best is None or r < best[0]:
                    best = (r, m, other)
        if best:
            _r, m, other = best
            by_low[other].remove(m)
            by_low[lo].append(m)
            m.runner_up, m.low, m.dist = other, lo, m.scores[lo]
            m.close_call = True


def apply_names(plan, data_names=True):
    """Rename in two passes so names inside the set can swap without clashing."""
    pairs = [(e.low, e.new_low) for e in plan.entries]
    pairs += [(m.high, n) for e in plan.entries for m, n in e.highs]
    pairs = [(o, n) for o, n in pairs if o.name != n]
    for i, (o, _n) in enumerate(pairs):
        o.name = f"__om3d_rename_{i}"
    done = 0
    for o, n in pairs:
        o.name = n
        done += 1
        me = o.data
        if data_names and me is not None and me.users == 1 and me.name != n:
            if bpy.data.meshes.get(n) in (None, me):
                me.name = n
    return done
