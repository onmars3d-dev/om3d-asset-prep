# SPDX-License-Identifier: GPL-3.0-or-later
"""Batch Export core: one FBX per asset with a preset (Unreal, Substance, Marmoset).

Your objects are never changed. Each export builds temporary copies from the
evaluated meshes (modifiers applied), bakes the world transform in, moves the
asset to the origin, exports the copies, and deletes them again. Originals whose
names the copies need are renamed for the moment of export and put back.
"""
import os
import re
import time
from dataclasses import dataclass, field

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import bake
from . import naming

TMP_SUFFIX = "__om3d_export_tmp"
TMP_COLLECTION = "__OM3D_Export_Tmp"
LOD_RE = re.compile(r"^(?P<base>.+?)[_.]LOD(?P<n>\d+)$", re.IGNORECASE)

PRESET_KEYS = ("UNREAL", "SUBSTANCE", "MARMOSET")

# Defaults from the spec table. Editable copies live in add-on preferences.
PRESET_DEFAULTS = {
    "UNREAL": dict(axis_forward="-Y", axis_up="Z", global_scale=1.0, apply_unit_scale=True,
                   smoothing="FACE", triangulate=False, include_high=False, collision=True,
                   lods=True, strip_low=True),
    "SUBSTANCE": dict(axis_forward="Y", axis_up="Z", global_scale=1.0, apply_unit_scale=True,
                      smoothing="FACE", triangulate=True, include_high=True, collision=False,
                      lods=False, strip_low=False),
    "MARMOSET": dict(axis_forward="Y", axis_up="Z", global_scale=1.0, apply_unit_scale=True,
                     smoothing="FACE", triangulate=True, include_high=True, collision=False,
                     lods=False, strip_low=False),
}


@dataclass
class Settings:
    preset: str = "UNREAL"
    axis_forward: str = "-Y"
    axis_up: str = "Z"
    global_scale: float = 1.0
    apply_unit_scale: bool = True
    smoothing: str = "FACE"
    triangulate: bool = False
    include_high: bool = False
    collision: bool = True
    lods: bool = True
    strip_low: bool = True
    merge_highs: bool = True     # several highs per low become one <base>_high mesh
    group_by: str = "ASSET"      # ASSET (name) or COLLECTION
    pivot: str = "BOTTOM"        # BOTTOM, CENTER, KEEP
    root: str = ""               # absolute export folder


@dataclass
class Item:
    obj: object
    name: str                    # name in the FBX


@dataclass
class FileJob:
    asset: str
    kind: str                    # "low", "high", "game", "lod1"...
    path: str
    items: list = field(default_factory=list)   # Item
    merge: list = field(default_factory=list)   # [(name, [objs])] meshes joined into one
    tris: int = 0


@dataclass
class AssetJob:
    asset: str
    objects: list = field(default_factory=list)
    files: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    pivot: Vector = None


@dataclass
class Plan:
    assets: list = field(default_factory=list)
    skipped: list = field(default_factory=list)   # (object name, reason)
    errors: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------

def _parse(conv, name):
    """(info dict, lod level). Strips .001 and a _LODn suffix before parsing."""
    base = naming.strip_dup_suffix(name)
    lod = 0
    m = LOD_RE.match(base)
    if m:
        base, lod = m.group("base"), int(m.group("n"))
    return conv.parse(base), lod, base


def _collision_target(conv, name):
    """Asset a UCX_ collision mesh belongs to, from UCX_SM_<Asset>_..."""
    rest = name[4:]
    if not rest.startswith(conv.prefix):
        return None
    body = rest[len(conv.prefix):]
    return body.split(conv.sep or "_")[0] or None


def tri_count(obj, depsgraph):
    ev = obj.evaluated_get(depsgraph)
    try:
        me = ev.to_mesh()
    except RuntimeError:
        return 0
    try:
        return sum(len(p.vertices) - 2 for p in me.polygons)
    finally:
        ev.to_mesh_clear()


def strip_low(conv, name):
    tail = (conv.sep or "_") + (conv.low or "low")
    return name[:-len(tail)] if name.endswith(tail) else name


def plan(context, objects, conv, s, scene_report=None):
    """Work out every file to write. Nothing is touched."""
    p = Plan()
    if not s.root:
        p.errors.append("Set an export folder first")
    groups = {}   # asset -> {"low": [(obj, lod)], "high": [], "col": []}

    def bucket(asset):
        return groups.setdefault(asset, {"low": [], "high": [], "col": []})

    for obj in objects:
        if obj.type != "MESH":
            continue
        if naming.is_collision_name(obj.name):
            if s.collision:
                asset = (obj.users_collection[0].name if s.group_by == "COLLECTION"
                         and obj.users_collection else _collision_target(conv, obj.name))
                if asset:
                    bucket(asset)["col"].append(obj)
                else:
                    p.skipped.append((obj.name, "collision name doesn't say which asset"))
            continue
        info, lod, _base = _parse(conv, obj.name)
        if s.group_by == "COLLECTION":
            if not obj.users_collection or obj.users_collection[0] == context.scene.collection:
                p.skipped.append((obj.name, "not in a collection"))
                continue
            asset = obj.users_collection[0].name
            role = info["role"] if info and info["role"] else "low"
        else:
            if info is None:
                p.skipped.append((obj.name, "name doesn't match the convention"))
                continue
            asset = info["asset"]
            role = info["role"] or "low"
        if lod and not s.lods:
            p.skipped.append((obj.name, f"LOD{lod} (LODs off in this preset)"))
            continue
        bucket(asset)[role].append((obj, lod) if role == "low" else obj)

    dg = context.evaluated_depsgraph_get()
    sep = conv.sep or "_"
    for asset in sorted(groups):
        g = groups[asset]
        job = AssetJob(asset)
        lows = g["low"]
        if not lows:
            job.warnings.append("No low objects, only highs: nothing to export"
                                if g["high"] else "Nothing to export")
            p.assets.append(job)
            continue
        folder = os.path.join(s.root, asset)
        stem = conv.prefix + asset
        lod_levels = sorted({lod for _o, lod in lows})
        game = s.preset == "UNREAL"
        for lod in lod_levels:
            objs = [o for o, l in lows if l == lod]
            if game:
                name = stem + (f"{sep}LOD{lod}" if lod else "") + ".fbx"
                kind = f"lod{lod}" if lod else "game"
            else:
                if lod:
                    continue
                name = f"{stem}{sep}{conv.low or 'low'}.fbx"
                kind = "low"
            f = FileJob(asset, kind, os.path.join(folder, name))
            for o in objs:
                n = naming.strip_dup_suffix(o.name)
                if lod:
                    m = LOD_RE.match(n)
                    n = m.group("base") if m else n
                    n = (strip_low(conv, n) if s.strip_low else n) + f"{sep}LOD{lod}"
                elif s.strip_low:
                    n = strip_low(conv, n)
                f.items.append(Item(o, n))
            if lod == 0 and s.collision:
                for c in g["col"]:
                    n = naming.strip_dup_suffix(c.name)
                    if s.strip_low:
                        n = n.replace(f"{sep}{conv.low or 'low'}{sep}", sep)
                        n = strip_low(conv, n)
                    f.items.append(Item(c, n))
            f.tris = sum(tri_count(i.obj, dg) for i in f.items)
            job.files.append(f)
        if s.include_high and g["high"]:
            f = FileJob(asset, "high", os.path.join(folder, f"{stem}{sep}{conv.high or 'high'}.fbx"))
            by_base = {}
            for h in g["high"]:
                info, _lod, _b = _parse(conv, h.name)
                key = bake.base_name(conv, info["asset"], info["part"]) if info else h.name
                by_base.setdefault(key, []).append(h)
            for base, hs in sorted(by_base.items()):
                hs.sort(key=lambda o: o.name)
                if s.merge_highs and len(hs) > 1:
                    f.merge.append((f"{base}{sep}{conv.high or 'high'}", hs))
                elif s.merge_highs:
                    f.items.append(Item(hs[0], f"{base}{sep}{conv.high or 'high'}"))
                else:
                    for h in hs:
                        f.items.append(Item(h, naming.strip_dup_suffix(h.name)))
            f.tris = sum(tri_count(o, dg) for o in g["high"])
            job.files.append(f)
        elif s.include_high and s.preset != "UNREAL":
            job.warnings.append("No highs found for the bake file")
        job.objects = [o for o, _l in lows] + g["high"] + g["col"]
        job.pivot = _pivot([o for o, _l in lows], s.pivot)
        _name_clashes(job)
        if scene_report:
            job.warnings += scene_warnings(scene_report, job.objects)
        p.assets.append(job)
    return p


def _name_clashes(job):
    for f in job.files:
        seen = {}
        names = [i.name for i in f.items] + [m[0] for m in f.merge]
        for n in names:
            seen[n] = seen.get(n, 0) + 1
        for n, c in seen.items():
            if c > 1:
                job.warnings.append(f"{os.path.basename(f.path)}: {c} meshes would be named {n}")


def _pivot(objs, mode):
    if mode == "KEEP" or not objs:
        return Vector((0.0, 0.0, 0.0))
    b = bake.world_bounds(objs)
    if b is None:
        return Vector((0.0, 0.0, 0.0))
    lo, hi = b
    c = (lo + hi) * 0.5
    if mode == "BOTTOM":
        c.z = lo.z
    return c


def scene_warnings(report, objects):
    """Scene Prep errors and warnings that touch these objects."""
    names = {o.name for o in objects}
    out = []
    for _key, data in report.get("issues", {}).items():
        if data.get("severity") not in ("ERROR", "WARNING") or data.get("level") == "FILE":
            continue
        hit = names & set(data.get("objects", {}))
        if hit:
            tag = "Error" if data["severity"] == "ERROR" else "Warning"
            out.append(f"Scene Prep {tag}: {data['label']} on {len(hit)} object"
                       f"{'s' if len(hit) > 1 else ''}")
    return out


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _mesh_copy(obj, depsgraph, offset):
    """New mesh datablock: evaluated mesh with world transform baked, moved by -offset."""
    ev = obj.evaluated_get(depsgraph)
    me = bpy.data.meshes.new_from_object(ev, preserve_all_data_layers=True, depsgraph=depsgraph)
    m = Matrix.Translation(-offset) @ obj.matrix_world
    me.transform(m)
    if m.determinant() < 0:
        me.flip_normals()
    if not me.materials and obj.material_slots:
        for slot in obj.material_slots:
            me.materials.append(slot.material)
    return me


def _join(meshes, name):
    """One mesh from several, keeping each face's material."""
    mats = []
    bm = bmesh.new()
    for me in meshes:
        before = len(bm.faces)
        remap = []
        for m in me.materials:
            if m not in mats:
                mats.append(m)
            remap.append(mats.index(m))
        bm.from_mesh(me)
        bm.faces.ensure_lookup_table()
        for f in bm.faces[before:]:
            f.material_index = remap[f.material_index] if f.material_index < len(remap) else 0
    out = bpy.data.meshes.new(name)
    bm.to_mesh(out)
    bm.free()
    for m in mats:
        out.materials.append(m)
    return out


def _fbx_kwargs(s, path):
    return dict(
        filepath=path, use_selection=True, use_visible=False, use_active_collection=False,
        object_types={"MESH"}, use_mesh_modifiers=False, mesh_smooth_type=s.smoothing,
        use_triangles=s.triangulate, use_tspace=False, use_custom_props=False,
        add_leaf_bones=False, bake_anim=False, axis_forward=s.axis_forward, axis_up=s.axis_up,
        global_scale=s.global_scale, apply_unit_scale=s.apply_unit_scale,
        apply_scale_options="FBX_SCALE_NONE", path_mode="AUTO", batch_mode="OFF")


def run(context, p, s):
    """Write every file in the plan. Returns [(path, ok, message)]."""
    results = []
    scene = context.scene
    vl = context.view_layer
    dg = context.evaluated_depsgraph_get()
    old_sel = [o for o in vl.objects if o.select_get()]
    old_active = vl.objects.active
    old_mode = context.mode
    if old_mode != "OBJECT" and context.active_object is not None:
        bpy.ops.object.mode_set(mode="OBJECT")
    tmp_col = bpy.data.collections.new(TMP_COLLECTION)
    scene.collection.children.link(tmp_col)
    try:
        for job in p.assets:
            for f in job.files:
                results.append(_write(context, dg, tmp_col, job, f, s))
    finally:
        for o in list(tmp_col.objects):
            me = o.data
            bpy.data.objects.remove(o)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
        bpy.data.collections.remove(tmp_col)
        for o in vl.objects:
            o.select_set(o in old_sel)
        vl.objects.active = old_active
        if old_mode == "EDIT_MESH" and old_active is not None:
            bpy.ops.object.mode_set(mode="EDIT")
    return results


def _write(context, dg, tmp_col, job, f, s):
    renamed = []
    temps = []
    try:
        os.makedirs(os.path.dirname(f.path), exist_ok=True)
        wanted = [(i.name, [i.obj]) for i in f.items] + list(f.merge)
        for name, _objs in wanted:  # free up the names the copies need
            other = bpy.data.objects.get(name)
            if other is not None:
                renamed.append((other, name))
                other.name = name + TMP_SUFFIX
        for name, objs in wanted:
            meshes = [_mesh_copy(o, dg, job.pivot) for o in objs]
            if len(meshes) > 1:
                me = _join(meshes, name)
                for m in meshes:
                    bpy.data.meshes.remove(m)
            else:
                me = meshes[0]
                me.name = name
            ob = bpy.data.objects.new(name, me)
            tmp_col.objects.link(ob)
            temps.append(ob)
        vl = context.view_layer
        for o in vl.objects:
            o.select_set(False)
        for ob in temps:
            ob.select_set(True)
        vl.objects.active = temps[0] if temps else None
        bad = [ob.name for ob, (name, _o) in zip(temps, wanted) if ob.name != name]
        r = bpy.ops.export_scene.fbx(**_fbx_kwargs(s, f.path))
        if "FINISHED" not in r:
            return (f.path, False, "FBX exporter cancelled")
        msg = f"{len(temps)} mesh{'es' if len(temps) != 1 else ''}, {f.tris:,} tris"
        if bad:
            msg += f" (renamed: {', '.join(bad)})"
        return (f.path, True, msg)
    except Exception as exc:  # report and carry on with the next file
        return (f.path, False, str(exc))
    finally:
        for ob in temps:
            me = ob.data
            bpy.data.objects.remove(ob)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)
        for other, name in renamed:
            other.name = name


def write_log(root, p, results, s):
    """Plain-text log next to the exports. Returns its path (or None)."""
    if not root:
        return None
    lines = [f"OM3D Batch Export  {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"Preset {s.preset}, axis {s.axis_forward} forward / {s.axis_up} up, "
             f"scale {s.global_scale}, smoothing {s.smoothing}, "
             f"triangulate {'on' if s.triangulate else 'off'}", ""]
    res = {r[0]: r for r in results}
    for job in p.assets:
        lines.append(job.asset)
        for f in job.files:
            r = res.get(f.path)
            state = "OK  " if r and r[1] else "FAIL"
            lines.append(f"  {state} {f.path}  ({r[2] if r else 'not written'})")
        for w in job.warnings:
            lines.append(f"  warn {w}")
        lines.append("")
    if p.skipped:
        lines.append("Skipped")
        lines += [f"  {n}: {why}" for n, why in p.skipped]
    path = os.path.join(root, "om3d_export_log.txt")
    try:
        os.makedirs(root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return None
    return path
