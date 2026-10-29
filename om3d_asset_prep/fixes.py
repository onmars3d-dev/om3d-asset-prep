# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene Prep fixes.

run_fixes(objects, opt, dry_run) applies the chosen fixes in a safe order and
returns a report of what changed (or, with dry_run, what would change). No UI
code here. The operator wraps it in one undo step.

Order: hierarchy, mesh cleanup, transforms, origins, materials, names.
World positions never move: every parent change restores its children's
world matrices, and origin changes move the mesh the opposite way.
Linked (library) data and meshes shared by several objects are skipped where
changing them would affect other objects.
"""
import re
from dataclasses import dataclass, fields

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import mesh_checks
from . import naming

STEPS = (
    ("hierarchy", "Hierarchy"),
    ("mesh", "Mesh cleanup"),
    ("transforms", "Transforms"),
    ("origins", "Origins"),
    ("materials", "Materials"),
    ("names", "Names"),
)


@dataclass
class FixOptions:
    remove_empty_parents: bool = False
    flatten: bool = False
    merge_by_distance: bool = False
    delete_loose: bool = False
    recalc_normals: bool = False
    transforms: bool = True
    negative_scale: bool = False
    apply_location: bool = False
    origin: str = "KEEP"          # KEEP, BOUNDS, BOTTOM, WORLD
    remove_empty_slots: bool = True
    purge_materials: bool = True
    rename: bool = False
    asset_name: str = ""
    strip_duplicates: bool = True
    match_data_names: bool = True
    tolerance: float = 1e-4
    convention: naming.Convention = None
    dissolve_degenerate: bool = False
    triangulate_ngons: bool = False


def only(**kw):
    """FixOptions with every fix off except the ones passed in."""
    base = {f.name: False for f in fields(FixOptions) if f.type is bool}
    base.update(kw)
    return FixOptions(**base)


class Report:
    def __init__(self, dry_run):
        self.dry_run = dry_run
        self.changes = {k: [] for k, _ in STEPS}
        self.skipped = {k: [] for k, _ in STEPS}

    def change(self, step, text):
        self.changes[step].append(text)

    def skip(self, step, text):
        self.skipped[step].append(text)

    def count(self):
        return sum(len(v) for v in self.changes.values())

    def as_dict(self):
        return {"dry_run": self.dry_run, "changes": self.changes, "skipped": self.skipped}

    def lines(self):
        out = []
        for key, label in STEPS:
            for t in self.changes[key]:
                out.append(f"{label}: {t}")
        for key, label in STEPS:
            for t in self.skipped[key]:
                out.append(f"Skipped, {label.lower()}: {t}")
        return out

    def summary(self):
        n = self.count()
        skipped = sum(len(v) for v in self.skipped.values())
        if n:
            verb = "Would change" if self.dry_run else "Changed"
            text = f"{verb} {n} item{'s' if n != 1 else ''}"
        else:
            text = "Nothing to change" if self.dry_run else "Nothing changed"
        if skipped:
            text += f", {skipped} skipped"
        return text


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _linked(obj):
    return obj.library is not None or (obj.data is not None and getattr(obj.data, "library", None))


def _alive(obj):
    """False once the object has been deleted (its Python handle goes stale)."""
    try:
        return bpy.data.objects.get(obj.name) == obj
    except ReferenceError:
        return False


def _depth(obj):
    d, p = 0, obj.parent
    while p is not None:
        d += 1
        p = p.parent
    return d


def _set_world_keep_children(obj, new_world):
    """Give obj a new world matrix without moving any of its children."""
    kids = [(c, c.matrix_world.copy()) for c in obj.children]
    obj.matrix_world = new_world
    for c, mw in kids:
        c.matrix_world = mw


def _reparent_keep_world(obj, new_parent):
    mw = obj.matrix_world.copy()
    obj.parent = new_parent
    obj.matrix_parent_inverse = new_parent.matrix_world.inverted() if new_parent else Matrix()
    obj.matrix_world = mw


def _single_user_mesh(obj, rep, step):
    if obj.type != "MESH":
        return False
    if _linked(obj):
        rep.skip(step, f"{obj.name} (linked from a library)")
        return False
    if obj.data.users > 1:
        rep.skip(step, f"{obj.name} (mesh shared with {obj.data.users - 1} other object(s))")
        return False
    return True


def _edit_bmesh(me, fn):
    """Run fn(bm) on a copy of the mesh; write back only if fn returns True."""
    bm = bmesh.new()
    try:
        bm.from_mesh(me)
        if fn(bm):
            bm.to_mesh(me)
            me.update()
    finally:
        bm.free()


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

def _fix_hierarchy(objects, opt, rep):
    if opt.remove_empty_parents:
        empties = {o for o in objects if o.type == "EMPTY" and o.children}
        empties |= {o.parent for o in objects if o.parent is not None and o.parent.type == "EMPTY"}
        for e in sorted(empties, key=_depth, reverse=True):
            if _linked(e):
                rep.skip("hierarchy", f"{e.name} (linked from a library)")
                continue
            if e.instance_type == "COLLECTION":
                rep.skip("hierarchy", f"{e.name} (collection instance)")
                continue
            if e.animation_data and e.animation_data.action:
                rep.skip("hierarchy", f"{e.name} (animated)")
                continue
            if e.constraints:
                rep.skip("hierarchy", f"{e.name} (has constraints)")
                continue
            n = len(e.children)
            rep.change("hierarchy", f"removed empty {e.name}, kept its {n} child(ren) in place")
            if not rep.dry_run:
                for c in list(e.children):
                    _reparent_keep_world(c, e.parent)
                bpy.data.objects.remove(e, do_unlink=True)
    if opt.flatten:
        for o in sorted([o for o in objects if _alive(o)], key=_depth, reverse=True):
            if o.parent is not None and not _linked(o):
                rep.change("hierarchy", f"unparented {o.name} from {o.parent.name}")
                if not rep.dry_run:
                    _reparent_keep_world(o, None)


def _fix_mesh(meshes, opt, rep):
    if not (opt.merge_by_distance or opt.delete_loose or opt.recalc_normals
            or opt.dissolve_degenerate or opt.triangulate_ngons):
        return
    done = set()
    for obj in meshes:
        me = obj.data
        if me.name in done or _linked(obj):
            continue
        done.add(me.name)
        notes = []

        def work(bm):
            changed = False
            tol = mesh_checks.scaled_tolerance(bm, opt.tolerance)
            if opt.dissolve_degenerate:
                bad = [f for f in bm.faces if f.calc_area() <= 1e-10]
                if bad:
                    edges = list({e for f in bad for e in f.edges})
                    bmesh.ops.dissolve_degenerate(bm, dist=tol, edges=edges)
                    left = [f for f in bm.faces if f.calc_area() <= 1e-10]
                    if left:
                        bmesh.ops.delete(bm, geom=left, context="FACES_ONLY")
                    notes.append(f"removed {len(bad)} collapsed faces")
                    changed = True
            if opt.delete_loose:
                lv = [v for v in bm.verts if not v.link_faces and not v.link_edges]
                le = [e for e in bm.edges if not e.link_faces]
                if lv or le:
                    notes.append(f"deleted {len(lv) + len(le)} loose verts/edges")
                    bmesh.ops.delete(bm, geom=le, context="EDGES")
                    lv = [v for v in bm.verts if not v.link_faces and not v.link_edges]
                    bmesh.ops.delete(bm, geom=lv, context="VERTS")
                    changed = True
            if opt.merge_by_distance:
                before = len(bm.verts)
                bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=tol)
                merged = before - len(bm.verts)
                if merged:
                    notes.append(f"merged {merged} overlapping verts")
                    changed = True
            if opt.triangulate_ngons:
                ngons = [f for f in bm.faces if len(f.verts) > 4]
                if ngons:
                    bmesh.ops.triangulate(bm, faces=ngons, quad_method="BEAUTY", ngon_method="BEAUTY")
                    notes.append(f"triangulated {len(ngons)} n-gons")
                    changed = True
            if opt.recalc_normals:
                bad = mesh_checks.flipped_normals(bm)
                seeds = set(bad["faces"])
                for e in bad["edges"]:
                    seeds.update(e.link_faces)
                if seeds:
                    island_faces = set()
                    for island in mesh_checks._islands(bm):
                        if any(f in seeds for f in island):
                            island_faces.update(island)
                    before = {f: f.normal.copy() for f in island_faces}
                    bmesh.ops.recalc_face_normals(bm, faces=list(island_faces))
                    flipped = sum(1 for f, n in before.items() if f.normal.dot(n) < 0)
                    if flipped:
                        notes.append(f"flipped {flipped} faces to face outward")
                        changed = True
            return changed and not rep.dry_run

        _edit_bmesh(me, work)
        if notes:
            users = f" (shared by {me.users} objects)" if me.users > 1 else ""
            rep.change("mesh", f"{obj.name}: " + ", ".join(notes) + users)


def _fix_transforms(meshes, opt, rep):
    if not opt.transforms:
        return
    eps = 1e-6
    for obj in sorted(meshes, key=_depth):
        basis = obj.matrix_basis.copy()
        loc, rot, scale = basis.decompose()
        has_rot = rot.angle > eps
        has_scale = any(abs(s - 1.0) > eps for s in scale)
        has_loc = opt.apply_location and loc.length > eps
        if not (has_rot or has_scale or has_loc):
            continue
        if not _single_user_mesh(obj, rep, "transforms"):
            continue
        negative = basis.to_3x3().determinant() < 0
        if negative and not opt.negative_scale:
            rep.skip("transforms", f"{obj.name} (negative scale; tick Include Negative Scale)")
            continue
        applied = basis.copy() if opt.apply_location else Matrix.Translation(-loc) @ basis
        if negative:
            has_rot, has_scale = False, True  # decompose() is unreliable on mirrored matrices
        what = [w for w, on in (("rotation", has_rot), ("scale", has_scale), ("location", has_loc)) if on]
        note = f"{obj.name}: applied {' and '.join(what)}"
        if negative:
            note += ", flipped normals back (was mirrored)"
        rep.change("transforms", note)
        if rep.dry_run:
            continue
        new_world = obj.matrix_world @ applied.inverted()
        obj.data.transform(applied, shape_keys=True)
        if negative:
            obj.data.flip_normals()
        _set_world_keep_children(obj, new_world)


def _fix_origins(meshes, opt, rep):
    if opt.origin == "KEEP":
        return
    import numpy as np
    for obj in meshes:
        if not _single_user_mesh(obj, rep, "origins"):
            continue
        me = obj.data
        n = len(me.vertices)
        if n == 0:
            continue
        co = np.empty(n * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(n, 3)
        mw = obj.matrix_world.copy()
        m = np.array(mw)
        world = co @ m[:3, :3].T + m[:3, 3]
        lo, hi = world.min(axis=0), world.max(axis=0)
        if opt.origin == "WORLD":
            target = Vector((0.0, 0.0, 0.0))
        else:
            c = (lo + hi) / 2.0
            target = Vector((c[0], c[1], lo[2] if opt.origin == "BOTTOM" else c[2]))
        local = mw.inverted() @ target
        if local.length < 1e-6:
            continue
        label = {"BOUNDS": "bounds center", "BOTTOM": "bottom center", "WORLD": "world origin"}[opt.origin]
        rep.change("origins", f"{obj.name}: origin to {label}")
        if rep.dry_run:
            continue
        me.transform(Matrix.Translation(-local), shape_keys=True)
        _set_world_keep_children(obj, mw @ Matrix.Translation(local))


def _fix_materials(meshes, opt, rep):
    if opt.remove_empty_slots:
        done = set()
        for obj in meshes:
            me = obj.data
            if me.name in done or _linked(obj):
                continue
            done.add(me.name)
            empty = [i for i, s in enumerate(obj.material_slots) if s.material is None]
            if not empty:
                continue
            used = {p.material_index for p in me.polygons}
            removed = 0
            for i in reversed(empty):
                slot = obj.material_slots[i]
                if slot.link != "DATA":
                    rep.skip("materials", f"{obj.name} slot {i + 1} (linked to the object, not the mesh)")
                    continue
                if i in used:
                    rep.skip("materials", f"{obj.name} slot {i + 1} (faces still assigned to it)")
                    continue
                removed += 1
                if not rep.dry_run:
                    me.materials.pop(index=i)
            if removed:
                rep.change("materials", f"{obj.name}: removed {removed} empty slot(s)")
    if opt.purge_materials:
        for m in [m for m in bpy.data.materials if m.users == 0 and not m.use_fake_user]:
            if m.library:
                continue
            rep.change("materials", f"deleted unused material {m.name}")
            if not rep.dry_run:
                bpy.data.materials.remove(m)


_ROLE_WORDS = re.compile(r"(?i)^(low|lo|lp|lowpoly|high|hi|hp|highpoly)$")
_HIGH_WORDS = re.compile(r"(?i)(^|[^a-z])(high|hi|hp|highpoly)([^a-z]|$)")


def _token(text):
    """'crate_lid-01' -> 'CrateLid01'. Words split on anything not a letter or digit."""
    words = [w for w in re.split(r"[^A-Za-z0-9]+", text) if w and not _ROLE_WORDS.match(w)]
    return "".join(w[:1].upper() + w[1:] for w in words)


def _asset_for(obj, opt):
    if opt.asset_name:
        return _token(opt.asset_name) or "Asset"
    for col in obj.users_collection:
        if col.name not in ("Collection", "Scene Collection"):
            t = _token(col.name)
            if t:
                return t
    return "Asset"


def _fix_names(objects, opt, rep):
    conv = opt.convention or naming.Convention()
    taken = set(bpy.data.objects.keys())
    planned = {}

    def free(name, current):
        return name == current or (name not in taken and name not in planned.values())

    meshes = [o for o in objects if o.type == "MESH" and not _linked(o)]
    if opt.rename:
        counter = {}
        for obj in meshes:
            base = naming.strip_dup_suffix(obj.name)
            if naming.is_collision_name(base) or conv.matches(base):
                continue
            asset = _asset_for(obj, opt)
            role = "high" if _HIGH_WORDS.search(base) else "low"
            junk = naming.is_junk_name(base)
            part = "" if junk else _token(base)
            if part.lower() == asset.lower():
                part = ""
            if not part:
                counter[asset] = counter.get(asset, 0) + 1
                part = f"Part{counter[asset]:02d}"
            new = conv.make(asset, part, role)
            n = 2
            while not free(new, obj.name):
                new = conv.make(asset, f"{part}{n:02d}", role)
                n += 1
            planned[obj.name] = new
    if opt.strip_duplicates:
        for obj in meshes:
            cur = planned.get(obj.name, obj.name)
            if naming.has_dup_suffix(cur):
                base = naming.strip_dup_suffix(cur)
                if free(base, obj.name):
                    planned[obj.name] = base
                else:
                    rep.skip("names", f"{obj.name} (another object is already called {base})")
    for old, new in planned.items():
        if new != old:
            rep.change("names", f"{old} -> {new}")
            if not rep.dry_run:
                bpy.data.objects[old].name = new
    if opt.match_data_names:
        for obj in meshes:
            name = planned.get(obj.name, obj.name) if rep.dry_run else obj.name
            me = obj.data
            if me.users != 1 or me.name == name:
                continue
            other = bpy.data.meshes.get(name)
            if other is not None and other != me:
                rep.skip("names", f"mesh data of {name} (a mesh called {name} already exists)")
                continue
            rep.change("names", f"mesh data {me.name} -> {name}")
            if not rep.dry_run:
                me.name = name


def run_fixes(objects, opt, dry_run=False):
    """Apply (or with dry_run, plan) the chosen fixes. Returns a Report."""
    rep = Report(dry_run)
    objects = [o for o in objects if o is not None]
    _fix_hierarchy(objects, opt, rep)
    alive = objects if dry_run else [o for o in objects if _alive(o)]
    meshes = [o for o in alive if o.type == "MESH"]
    _fix_mesh(meshes, opt, rep)
    _fix_transforms(meshes, opt, rep)
    _fix_origins(meshes, opt, rep)
    _fix_materials(meshes, opt, rep)
    _fix_names(alive, opt, rep)
    return rep
