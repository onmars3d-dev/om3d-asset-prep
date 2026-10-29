# SPDX-License-Identifier: GPL-3.0-or-later
"""Read-only mesh checks on a BMesh.

Every function takes a BMesh and returns a dict of offending elements:
{"verts": [...], "edges": [...], "faces": [...]} (keys only when used).
Nothing here writes to the mesh, so callers can run it on a throwaway
bmesh.new() copy for reporting, or on the edit-mode BMesh to select results.
"""
import math

from mathutils import kdtree


def ngons(bm):
    return {"faces": [f for f in bm.faces if len(f.verts) > 4]}


def _fan_count(v):
    """How many separate face fans meet at a vert. More than 1 = bowtie."""
    seen = set()
    groups = 0
    for f in v.link_faces:
        if f in seen:
            continue
        groups += 1
        seen.add(f)
        stack = [f]
        while stack:
            cur = stack.pop()
            for e in cur.edges:
                if v not in e.verts:
                    continue
                for g in e.link_faces:
                    if g not in seen:
                        seen.add(g)
                        stack.append(g)
    return groups


def non_manifold(bm):
    """Edges shared by 3+ faces, and bowtie verts (two fans touching at a point).

    Open borders and wire edges are not counted here. Borders are normal on
    game assets, and wire edges are reported by loose_geometry.
    """
    edges = [e for e in bm.edges if len(e.link_faces) > 2]
    verts = [v for v in bm.verts if len(v.link_faces) > 1 and _fan_count(v) > 1]
    return {"edges": edges, "verts": verts}


def _islands(bm):
    seen = set()
    for f in bm.faces:
        if f in seen:
            continue
        island = []
        seen.add(f)
        stack = [f]
        while stack:
            cur = stack.pop()
            island.append(cur)
            for e in cur.edges:
                for g in e.link_faces:
                    if g not in seen:
                        seen.add(g)
                        stack.append(g)
        yield island


def _signed_volume(faces):
    vol = 0.0
    for f in faces:
        co = [v.co for v in f.verts]
        a = co[0]
        for i in range(1, len(co) - 1):
            vol += a.dot(co[i].cross(co[i + 1]))
    return vol / 6.0


def _mostly_inward(island, samples=40):
    """Ray parity: from a face, shoot along its normal and count how often the ray crosses
    the shell. Odd = the normal points into the shell. True when most sampled faces do.
    Confirms a negative volume, which can also come from a shell that folds through itself."""
    from mathutils.bvhtree import BVHTree
    index = {}
    verts, polys = [], []
    for f in island:
        p = []
        for v in f.verts:
            if v not in index:
                index[v] = len(verts)
                verts.append(v.co.copy())
            p.append(index[v])
        polys.append(p)
    tree = BVHTree.FromPolygons(verts, polys)
    step = max(1, len(island) // samples)
    size = max((max(c[i] for c in verts) - min(c[i] for c in verts)) for i in range(3)) or 1.0
    nudge = size * 1e-6
    inward = outward = 0
    for f in island[::step]:
        n = f.normal
        if n.length < 0.5:
            continue
        p = f.calc_center_median() + n * nudge
        hits = 0
        for _ in range(64):
            h = tree.ray_cast(p, n)
            if h[0] is None:
                break
            hits += 1
            p = h[0] + n * nudge
        if hits % 2:
            inward += 1
        else:
            outward += 1
    return inward > outward


def flipped_normals(bm, volume_eps=1e-12):
    """Two kinds of flip.

    1. Inconsistent winding: a face whose neighbour points the other way.
       Reported as the edges between them.
    2. Inside-out shells: a closed, consistent shell with negative volume whose normals
       a ray test confirms point inward. Reported as the shell's faces.
    """
    bad_edges = [e for e in bm.edges if len(e.link_faces) == 2 and not e.is_contiguous]
    bad_faces = []
    for island in _islands(bm):
        closed = True
        for f in island:
            for e in f.edges:
                if len(e.link_faces) != 2 or not e.is_contiguous:
                    closed = False
                    break
            if not closed:
                break
        if closed and _signed_volume(island) < -volume_eps and _mostly_inward(island):
            bad_faces.extend(island)
    return {"edges": bad_edges, "faces": bad_faces}


def loose_geometry(bm):
    return {"verts": [v for v in bm.verts if not v.link_faces],
            "edges": [e for e in bm.edges if not e.link_faces]}


def zero_area(bm, threshold=1e-10):
    """Collapsed faces: squashed into a line or a point, so they have no area."""
    return {"faces": [f for f in bm.faces if f.calc_area() <= threshold]}


def open_edges(bm):
    """Border edges used by only one face."""
    return {"edges": [e for e in bm.edges if len(e.link_faces) == 1]}


# --------------------------------------------------------------------------
# Overlap helpers (2D triangles, grid broad phase + SAT narrow phase)
# --------------------------------------------------------------------------

def _signed_area_2d(pts):
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a * 0.5


def _tri_overlap(a, b, eps):
    """True when two 2D triangles overlap by more than eps. Touching is not overlap."""
    for tri in (a, b):
        for i in range(3):
            p, q = tri[i], tri[(i + 1) % 3]
            nx, ny = q[1] - p[1], p[0] - q[0]
            length = math.hypot(nx, ny)
            if length == 0.0:
                return False
            nx /= length
            ny /= length
            pa = [nx * x + ny * y for x, y in a]
            pb = [nx * x + ny * y for x, y in b]
            if max(pa) <= min(pb) + eps or max(pb) <= min(pa) + eps:
                return False
    return True


def _overlapping(tris, eps):
    """Indices of 2D triangles that overlap at least one other triangle."""
    n = len(tris)
    if n < 2:
        return set()
    boxes = []
    size = 0.0
    for t in tris:
        xs = (t[0][0], t[1][0], t[2][0])
        ys = (t[0][1], t[1][1], t[2][1])
        b = (min(xs), min(ys), max(xs), max(ys))
        boxes.append(b)
        size += max(b[2] - b[0], b[3] - b[1])
    cell = max(2.0 * size / n, 1e-9)
    grid = {}
    big = []
    for i, b in enumerate(boxes):
        x0, x1 = int(math.floor(b[0] / cell)), int(math.floor(b[2] / cell))
        y0, y1 = int(math.floor(b[1] / cell)), int(math.floor(b[3] / cell))
        if (x1 - x0) > 32 or (y1 - y0) > 32:
            big.append(i)
            continue
        for gx in range(x0, x1 + 1):
            for gy in range(y0, y1 + 1):
                grid.setdefault((gx, gy), []).append(i)

    hit = set()
    tested = set()

    def test(i, j):
        if i == j or (i in hit and j in hit):
            return
        key = (i, j) if i < j else (j, i)
        if key in tested:
            return
        tested.add(key)
        bi, bj = boxes[i], boxes[j]
        if (bi[2] <= bj[0] + eps or bj[2] <= bi[0] + eps or
                bi[3] <= bj[1] + eps or bj[3] <= bi[1] + eps):
            return
        if _tri_overlap(tris[i], tris[j], eps):
            hit.add(i)
            hit.add(j)

    for idxs in grid.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                test(idxs[a], idxs[b])
    for i in big:
        for j in range(n):
            test(i, j)
    return hit


def _loop_tris(bm):
    return bm.calc_loop_triangles()


def uv_flipped(bm, eps=1e-12):
    """Faces whose UVs are mirrored (wound the opposite way to the face)."""
    uv = bm.loops.layers.uv.active
    if uv is None:
        return {}
    return {"faces": [f for f in bm.faces
                      if _signed_area_2d([tuple(l[uv].uv) for l in f.loops]) < -eps]}


def uv_overlap(bm, eps=1e-7):
    """Faces whose UVs overlap other UV faces with the same material on the active UV map.
    Each material is its own texture set, so overlaps across materials don't count."""
    uv = bm.loops.layers.uv.active
    if uv is None:
        return {}
    groups = {}
    for lt in _loop_tris(bm):
        t = tuple((l[uv].uv.x, l[uv].uv.y) for l in lt)
        if abs(_signed_area_2d(t)) < 1e-14:
            continue
        tris, faces = groups.setdefault(lt[0].face.material_index, ([], []))
        tris.append(t)
        faces.append(lt[0].face)
    out = {}
    for tris, faces in groups.values():
        for i in sorted(_overlapping(tris, eps)):
            out[faces[i]] = None
    return {"faces": list(out)}


def _clip_area(a, b):
    """Area of the overlap of two 2D triangles (Sutherland-Hodgman)."""
    def ccw(t):
        return list(t) if _signed_area_2d(t) > 0 else list(t)[::-1]

    def inside(p, u, v):
        return (v[0] - u[0]) * (p[1] - u[1]) - (v[1] - u[1]) * (p[0] - u[0]) >= 0

    def cross(p1, p2, u, v):
        dc = (u[0] - v[0], u[1] - v[1])
        dp = (p1[0] - p2[0], p1[1] - p2[1])
        n1 = u[0] * v[1] - u[1] * v[0]
        n2 = p1[0] * p2[1] - p1[1] * p2[0]
        d = dc[0] * dp[1] - dc[1] * dp[0]
        if abs(d) < 1e-30:
            return p2
        return ((n1 * dp[0] - n2 * dc[0]) / d, (n1 * dp[1] - n2 * dc[1]) / d)

    out, clipper = ccw(a), ccw(b)
    for i in range(3):
        u, v = clipper[i], clipper[(i + 1) % 3]
        src, out = out, []
        if not src:
            break
        prev = src[-1]
        for cur in src:
            if inside(cur, u, v):
                if not inside(prev, u, v):
                    out.append(cross(prev, cur, u, v))
                out.append(cur)
            elif inside(prev, u, v):
                out.append(cross(prev, cur, u, v))
            prev = cur
    return abs(_signed_area_2d(out)) if len(out) >= 3 else 0.0


def overlapping_faces(bm, tol=1e-4, min_share=0.01):
    """Faces stacked on top of each other in the same plane.

    Counts two faces facing the same way (they z-fight), or a face folded back over a
    neighbour it shares a vert with. Two separate surfaces pressed back to back (a bolt
    head sitting on a plate) face opposite ways, can't z-fight, and aren't counted.
    Overlaps under min_share of the smaller triangle are grazes and don't count either.
    """
    q = 200.0
    buckets = {}
    for lt in _loop_tris(bm):
        a, b, c = (l.vert.co for l in lt)
        n = (b - a).cross(c - a)
        if n.length < 1e-12:
            continue
        n.normalize()
        raw = n.copy()
        for comp in n:
            if abs(comp) > 1e-6:
                if comp < 0:
                    n.negate()
                break
        d = n.dot(a)
        key = (round(n.x * q), round(n.y * q), round(n.z * q), round(d / tol))
        buckets.setdefault(key, []).append((lt, n, raw))
    hit = {}
    for items in buckets.values():
        if len(items) < 2:
            continue
        n = items[0][1]
        ax = max(range(3), key=lambda i: abs(n[i]))
        k0, k1 = [i for i in range(3) if i != ax]
        tris = [tuple((l.vert.co[k0], l.vert.co[k1]) for l in lt) for lt, _n, _r in items]
        cand = sorted(_overlapping(tris, tol * 0.1))
        for x, i in enumerate(cand):
            for j in cand[x + 1:]:
                fi, fj = items[i][0][0].face, items[j][0][0].face
                if fi == fj or (fi in hit and fj in hit):
                    continue
                if items[i][2].dot(items[j][2]) < 0 and not (set(fi.verts) & set(fj.verts)):
                    continue  # back to back: separate surfaces touching
                if not _tri_overlap(tris[i], tris[j], tol * 0.1):
                    continue
                small = min(abs(_signed_area_2d(tris[i])), abs(_signed_area_2d(tris[j])))
                if _clip_area(tris[i], tris[j]) < small * min_share:
                    continue
                hit[fi] = None
                hit[fj] = None
    return {"faces": list(hit)}


def overlapping_verts(bm, tol=1e-4):
    """Verts sitting on top of each other without being merged."""
    verts = list(bm.verts)
    if len(verts) < 2:
        return {}
    kd = kdtree.KDTree(len(verts))
    for i, v in enumerate(verts):
        kd.insert(v.co, i)
    kd.balance()
    hit = []
    for i, v in enumerate(verts):
        linked = {e.other_vert(v) for e in v.link_edges}
        for _co, j, _dist in kd.find_range(v.co, tol):
            # verts joined by an edge are a short edge, not an unwelded duplicate
            if j != i and verts[j] not in linked:
                hit.append(v)
                break
    return {"verts": hit}


def tverts(bm, tol=1e-4, angle_eps=0.01):
    """T-verts.

    1. A vert that makes a straight (180 degree) corner in a face, so it sits
       in the middle of a side. The face is really a triangle or n-gon.
    2. A border vert touching another border edge it isn't connected to
       (a T-junction crack).
    """
    found = {}
    limit = math.pi - angle_eps
    for f in bm.faces:
        for lp in f.loops:
            if lp.edge.calc_length() < 1e-9 or lp.link_loop_prev.edge.calc_length() < 1e-9:
                continue
            if lp.calc_angle() > limit:
                found[lp.vert] = None

    border = [e for e in bm.edges if len(e.link_faces) == 1]
    if border:
        cell = max(sum(e.calc_length() for e in border) / len(border), tol * 10.0)
        grid = {}
        for e in border:
            a, b = e.verts[0].co, e.verts[1].co
            lo = [int(math.floor((min(a[i], b[i]) - tol) / cell)) for i in range(3)]
            hi = [int(math.floor((max(a[i], b[i]) + tol) / cell)) for i in range(3)]
            if any(hi[i] - lo[i] > 32 for i in range(3)):
                continue
            for gx in range(lo[0], hi[0] + 1):
                for gy in range(lo[1], hi[1] + 1):
                    for gz in range(lo[2], hi[2] + 1):
                        grid.setdefault((gx, gy, gz), []).append(e)
        for v in {v for e in border for v in e.verts}:
            if v in found:
                continue
            key = tuple(int(math.floor(v.co[i] / cell)) for i in range(3))
            for e in grid.get(key, ()):
                if v in e.verts:
                    continue
                a, b = e.verts[0].co, e.verts[1].co
                ab = b - a
                l2 = ab.length_squared
                if l2 < 1e-18:
                    continue
                t = (v.co - a).dot(ab) / l2
                if t <= 0.001 or t >= 0.999:
                    continue
                if (a + ab * t - v.co).length <= tol:
                    found[v] = None
                    break
    return {"verts": list(found)}


def scaled_tolerance(bm, tol, ratio=1e-4):
    """The overlap tolerance, shrunk on small meshes: 0.1 mm is real detail on a 5 mm bolt."""
    if not bm.verts:
        return tol
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for v in bm.verts:
        c = v.co
        for i in range(3):
            if c[i] < lo[i]:
                lo[i] = c[i]
            if c[i] > hi[i]:
                hi[i] = c[i]
    diag = math.sqrt(sum((hi[i] - lo[i]) ** 2 for i in range(3)))
    return min(tol, diag * ratio) if diag > 0 else tol


def count(found):
    return sum(len(v) for v in found.values())
