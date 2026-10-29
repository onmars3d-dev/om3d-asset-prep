# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene Prep checks: runs every check over a list of objects.

Read-only. Mesh checks run on a bmesh.new() copy that is freed afterwards
(or on the live edit-mode BMesh, read only), so the scene is never modified.
No UI code lives here, so it can be called from scripts, tests, or Claude:

    from bl_ext.user_default.om3d_asset_prep import scene_checks
    report = scene_checks.check_objects(objs, scene_checks.Options())

Custom checkers (like 3ds Max xView's scripted checkers):

    def no_tris(bm, opt):
        return {"faces": [f for f in bm.faces if len(f.verts) == 3]}
    scene_checks.register_check("tris", "Triangles", "INFO", "ELEMENT", no_tris,
                                help="Faces with 3 sides.")
"""
import math
from dataclasses import dataclass, field, replace

import bmesh
import bpy

from . import mesh_checks
from . import naming

# key, label, severity, level
#   level OBJECT  = whole objects are the problem
#   level ELEMENT = specific verts/edges/faces, selectable in Edit Mode, highlightable
#   level FILE    = file data (materials), no objects to select
BUILTIN = (
    ("scale_negative", "Negative scale", "ERROR", "OBJECT"),
    ("uv_missing", "No UV map", "ERROR", "OBJECT"),
    ("non_manifold", "Non-manifold geometry", "ERROR", "ELEMENT"),
    ("flipped_normals", "Flipped normals", "ERROR", "ELEMENT"),
    ("zero_area", "Collapsed faces", "ERROR", "ELEMENT"),
    ("overlapping_faces", "Overlapping faces", "ERROR", "ELEMENT"),
    ("scale_nonuniform", "Non-uniform scale", "WARNING", "OBJECT"),
    ("scale_unapplied", "Unapplied scale", "WARNING", "OBJECT"),
    ("rotation_unapplied", "Unapplied rotation", "WARNING", "OBJECT"),
    ("uv_multiple", "More than one UV map", "WARNING", "OBJECT"),
    ("ngons", "N-gons", "WARNING", "ELEMENT"),
    ("loose_geometry", "Loose verts and edges", "WARNING", "ELEMENT"),
    ("overlapping_verts", "Overlapping verts", "WARNING", "ELEMENT"),
    ("tverts", "T-verts", "WARNING", "ELEMENT"),
    ("uv_overlap", "Overlapping UVs", "WARNING", "ELEMENT"),
    ("name_junk", "Import junk names", "WARNING", "OBJECT"),
    ("name_convention", "Names off convention", "WARNING", "OBJECT"),
    ("name_duplicate", "Duplicate suffix (.001)", "WARNING", "OBJECT"),
    ("empty_parent", "Group empties", "WARNING", "OBJECT"),
    ("material_empty_slot", "Empty material slots", "WARNING", "OBJECT"),
    ("uv_flipped", "Flipped UVs", "INFO", "ELEMENT"),
    ("open_edges", "Open edges", "INFO", "ELEMENT"),
    ("name_data_mismatch", "Mesh data name differs", "INFO", "OBJECT"),
    ("material_unused", "Unused materials", "INFO", "FILE"),
)

HELP = {
    "scale_negative": "Mirrored with negative scale. Normals flip when it's applied and exports "
                      "can come out inside out. Apply scale, then recalculate normals.",
    "uv_missing": "No UV map, so it can't be textured or baked.",
    "non_manifold": "Edges shared by 3 or more faces, or faces touching at a single vert. "
                    "Breaks bakes, booleans and normals.",
    "flipped_normals": "Faces pointing the wrong way, or a whole shell inside out. "
                       "Shows up black or invisible in engine.",
    "zero_area": "Faces squashed flat into a line or a point, so you can't see them. They break "
                 "shading, bakes and triangulation. Usually left over from merges. Highlighted "
                 "as green lines.",
    "overlapping_faces": "Faces stacked in the same plane: two facing the same way (z-fighting "
                         "flicker in the viewport and in engine), or one folded back over its "
                         "neighbour. Separate parts touching back to back don't count.",
    "scale_nonuniform": "Scale differs per axis. Apply it before baking or exporting, "
                        "or bevels and normals skew.",
    "scale_unapplied": "Scale isn't 1. Apply it so export size and modifiers behave.",
    "rotation_unapplied": "Rotation isn't zero. Apply it unless the pivot rotation is on purpose.",
    "uv_multiple": "More than one UV map. Make sure the right one is first and remove spares.",
    "ngons": "Faces with more than 4 sides. They triangulate unpredictably in engine and in bakes.",
    "loose_geometry": "Verts or edges that aren't part of any face. Invisible, but they still "
                      "export and throw off bounds and pivots.",
    "overlapping_verts": "Verts on top of each other that aren't joined by an edge or merged. "
                         "Causes open seams and shading breaks. The distance shrinks on small "
                         "objects so fine detail isn't flagged. Merge by distance fixes it.",
    "tverts": "A vert sitting in the middle of a straight side of a face, or touching an edge "
              "it isn't connected to. Leaves cracks and bad shading.",
    "uv_overlap": "UV faces overlapping other faces with the same material on the active UV map "
                  "(each material is its own texture set). Fine if you stacked them on purpose, "
                  "but bakes need unique UVs. On round parts this is usually box projection "
                  "stacking the faces either side of 45 degrees; re-unwrap with seams.",
    "name_junk": "Default names from Maya, Max, Blender or an importer (pCube3, polySurface12, "
                 "Box001, Cube).",
    "name_convention": "Doesn't match the naming convention in the add-on preferences.",
    "name_duplicate": "Has a .001 style suffix from duplicating or importing.",
    "empty_parent": "Empties used only to group objects (they have children). Usually leftover "
                    "FBX or Maya groups, and they export as extra nodes.",
    "material_empty_slot": "Material slots with no material. Faces assigned to them export "
                           "without one.",
    "uv_flipped": "Mirrored UV faces. Common with symmetry; watch for normal map seams.",
    "open_edges": "Border edges with only one face. Normal on game props, but holes in a closed "
                  "mesh show up here.",
    "name_data_mismatch": "Mesh data name differs from the object name. Worth tidying for "
                          "exports and scripting.",
    "material_unused": "Materials nothing uses. They're dropped on save unless they have a "
                       "fake user.",
}

# Checks that start switched off in preferences (noisy on most game props).
OFF_BY_DEFAULT = {"open_edges"}

# Slow on dense meshes. Live refresh skips these above Options.skip_heavy_over faces
# and keeps their last result until the next full Check Scene.
HEAVY = {"uv_overlap", "overlapping_faces"}
LIVE_HEAVY_LIMIT = 5000

CHECKS = list(BUILTIN)
CHECK_INFO = {c[0]: {"label": c[1], "severity": c[2], "level": c[3]} for c in CHECKS}

ELEMENT_CHECKS = {
    "ngons": lambda bm, opt: mesh_checks.ngons(bm),
    "non_manifold": lambda bm, opt: mesh_checks.non_manifold(bm),
    "flipped_normals": lambda bm, opt: mesh_checks.flipped_normals(bm),
    "loose_geometry": lambda bm, opt: mesh_checks.loose_geometry(bm),
    "zero_area": lambda bm, opt: mesh_checks.zero_area(bm, opt.zero_area_threshold),
    "overlapping_faces": lambda bm, opt: mesh_checks.overlapping_faces(bm, opt.overlap_tolerance),
    "overlapping_verts": lambda bm, opt: mesh_checks.overlapping_verts(bm, opt.overlap_tolerance),
    "tverts": lambda bm, opt: mesh_checks.tverts(bm, opt.overlap_tolerance),
    "uv_overlap": lambda bm, opt: mesh_checks.uv_overlap(bm),
    "uv_flipped": lambda bm, opt: mesh_checks.uv_flipped(bm),
    "open_edges": lambda bm, opt: mesh_checks.open_edges(bm),
}

# Custom OBJECT checks: fn(obj, opt) -> int (0 = fine)
OBJECT_CHECKS = {}

SEVERITY_ORDER = {"ERROR": 0, "WARNING": 1, "INFO": 2}
EPS = 1e-5


def register_check(key, label, severity, level, fn, help=""):
    """Add a custom checker. ELEMENT: fn(bm, opt) -> {"verts"/"edges"/"faces": [...]}.
    OBJECT: fn(obj, opt) -> count. Re-registering a key replaces it."""
    if severity not in SEVERITY_ORDER or level not in ("ELEMENT", "OBJECT"):
        raise ValueError("severity must be ERROR/WARNING/INFO, level ELEMENT/OBJECT")
    unregister_check(key)
    CHECKS.append((key, label, severity, level))
    CHECKS.sort(key=lambda c: SEVERITY_ORDER[c[2]])
    CHECK_INFO[key] = {"label": label, "severity": severity, "level": level}
    HELP[key] = help
    (ELEMENT_CHECKS if level == "ELEMENT" else OBJECT_CHECKS)[key] = fn


def unregister_check(key):
    if key in {c[0] for c in BUILTIN}:
        return
    CHECKS[:] = [c for c in CHECKS if c[0] != key]
    for d in (CHECK_INFO, ELEMENT_CHECKS, OBJECT_CHECKS):
        d.pop(key, None)


@dataclass
class Options:
    convention: naming.Convention = field(default_factory=naming.Convention)
    blender_defaults_are_junk: bool = True
    zero_area_threshold: float = 1e-10
    overlap_tolerance: float = 1e-4
    enabled: frozenset = None  # None = every check
    skip_heavy_over: int = 0   # 0 = never skip heavy checks

    def on(self, key):
        return self.enabled is None or key in self.enabled


def options_from_prefs(context):
    addon = context.preferences.addons.get(__package__)
    if addon is None:
        return Options()
    p = addon.preferences
    enabled = frozenset(k for k in CHECK_INFO if getattr(p, f"use_{k}", True))
    return Options(
        convention=naming.Convention(prefix=p.prefix, sep=p.separator, low=p.low_suffix,
                                     high=p.high_suffix, require_suffix=p.require_suffix),
        blender_defaults_are_junk=p.blender_defaults_are_junk,
        zero_area_threshold=p.zero_area_threshold,
        overlap_tolerance=p.overlap_tolerance,
        enabled=enabled,
    )


def gather_objects(context, scope):
    if scope == "SELECTED":
        return list(context.selected_objects)
    if scope == "ALL":
        return list(context.scene.objects)
    return [o for o in context.view_layer.objects if o.visible_get()]


def topo_draw_running():
    wm = bpy.context.window_manager
    return bool(getattr(wm, "om3d_topo_running", False))


def read_bmesh(obj):
    """(bm, owned). In Edit Mode reads the live edit mesh, read only.
    Never grabs an edit-mesh wrapper while Topo Draw runs (it holds its own)."""
    if obj.mode == "EDIT" and not topo_draw_running():
        return bmesh.from_edit_mesh(obj.data), False
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    return bm, True


def scaled_options(bm, opt):
    """Options with the overlap tolerance shrunk to fit this mesh's size."""
    tol = mesh_checks.scaled_tolerance(bm, opt.overlap_tolerance)
    return opt if tol >= opt.overlap_tolerance else replace(opt, overlap_tolerance=tol)


def element_findings(bm, key, opt=None):
    """Offending elements for one ELEMENT check, on any BMesh."""
    return ELEMENT_CHECKS[key](bm, scaled_options(bm, opt or Options()))


def _geometry(key, found, tri_map):
    """Local-space highlight geometry for one check's findings.
    tris/lines/points = the faces-and-edges style; verts = every vert involved,
    each once (the vertices style)."""
    tris, lines, points = [], [], []
    involved = {}
    for f in found.get("faces", ()):
        for v in f.verts:
            involved[v] = None
        if key == "zero_area":
            for e in f.edges:
                lines += [tuple(e.verts[0].co), tuple(e.verts[1].co)]
            points += [tuple(v.co) for v in f.verts]
        else:
            for lt in tri_map().get(f, ()):
                tris += [tuple(lp.vert.co) for lp in lt]
    for e in found.get("edges", ()):
        involved[e.verts[0]] = None
        involved[e.verts[1]] = None
        lines += [tuple(e.verts[0].co), tuple(e.verts[1].co)]
    for v in found.get("verts", ()):
        involved[v] = None
    points += [tuple(v.co) for v in found.get("verts", ())]
    return {"tris": tris, "lines": lines, "points": points,
            "verts": [tuple(v.co) for v in involved]}


def check_mesh(bm, opt, want_geometry=True):
    """Element checks on one BMesh. Returns ({key: count}, {key: geometry}, skipped keys)."""
    counts, geom, skipped = {}, {}, set()
    dense = opt.skip_heavy_over and len(bm.faces) > opt.skip_heavy_over
    opt = scaled_options(bm, opt)
    cache = {}

    def tri_map():
        if "tris" not in cache:
            m = {}
            for lt in bm.calc_loop_triangles():
                m.setdefault(lt[0].face, []).append(lt)
            cache["tris"] = m
        return cache["tris"]

    for key, fn in ELEMENT_CHECKS.items():
        if not opt.on(key):
            continue
        if dense and key in HEAVY:
            skipped.add(key)
            continue
        found = fn(bm, opt)
        n = mesh_checks.count(found)
        if n:
            counts[key] = n
            if want_geometry:
                geom[key] = _geometry(key, found, tri_map)
    return counts, geom, skipped


def _effective_scale(obj):
    return [obj.scale[i] * obj.delta_scale[i] for i in range(3)]


def _has_rotation(obj):
    if obj.rotation_mode == "QUATERNION":
        rot = obj.rotation_quaternion
        angle = rot.angle if rot.magnitude > 0 else 0.0
    elif obj.rotation_mode == "AXIS_ANGLE":
        angle = obj.rotation_axis_angle[0]
    else:
        angle = max(abs(a) for a in obj.rotation_euler)
    angle = abs(math.remainder(angle, math.tau))
    delta = max(abs(math.remainder(a, math.tau)) for a in obj.delta_rotation_euler)
    q = obj.delta_rotation_quaternion
    delta_q = q.angle if q.magnitude > 0 else 0.0
    return max(angle, delta, abs(math.remainder(delta_q, math.tau))) > EPS


def _scale_issue(obj):
    s = _effective_scale(obj)
    if any(c < 0 for c in s):
        return "scale_negative"
    if max(s) - min(s) > EPS * max(1.0, max(abs(c) for c in s)):
        return "scale_nonuniform"
    if abs(s[0] - 1.0) > EPS:
        return "scale_unapplied"
    return None


def check_objects(objects, opt=None, geometry_out=None):
    """Run every enabled check. Returns a plain dict, safe to json.dumps.

    {"objects_checked": n, "meshes_checked": n, "checked": [mesh object names],
     "issues": {key: {"label", "severity", "level",
                      "objects": {name: count}, "items": [names]}}}
    geometry_out, if given, is filled with {object name: {key: geometry}}.
    """
    opt = opt or Options()
    conv = opt.convention
    found = {}

    def add(key, name, n=1):
        if opt.on(key):
            found.setdefault(key, {})[name] = found.get(key, {}).get(name, 0) + n

    meshes = [o for o in objects if o.type == "MESH"]
    mesh_cache = {}
    skipped = {}
    need_mesh = any(opt.on(k) for k in ELEMENT_CHECKS)

    for obj in meshes:
        name = obj.name

        issue = _scale_issue(obj)
        if issue:
            add(issue, name)
        if _has_rotation(obj):
            add("rotation_unapplied", name)

        n_uv = len(obj.data.uv_layers)
        if n_uv == 0:
            add("uv_missing", name)
        elif n_uv > 1:
            add("uv_multiple", name, n_uv)

        base = naming.strip_dup_suffix(name)
        if naming.has_dup_suffix(name):
            add("name_duplicate", name)
        if naming.is_junk_name(name, opt.blender_defaults_are_junk):
            add("name_junk", name)
        elif not naming.is_collision_name(base) and not conv.matches(base):
            add("name_convention", name)
        if obj.data.name != name and obj.data.users == 1:
            add("name_data_mismatch", name)

        empty_slots = sum(1 for s in obj.material_slots if s.material is None)
        if empty_slots:
            add("material_empty_slot", name, empty_slots)

        for key, fn in OBJECT_CHECKS.items():
            if opt.on(key):
                n = int(fn(obj, opt) or 0)
                if n:
                    add(key, name, n)

        # Geometry, once per mesh datablock (instances share results). Skipped when no
        # mesh-level check is on, e.g. when refreshing a single object-level issue.
        if not need_mesh:
            continue
        me = obj.data
        if me.name not in mesh_cache:
            bm, owned = read_bmesh(obj)
            try:
                mesh_cache[me.name] = check_mesh(bm, opt, geometry_out is not None)
            finally:
                if owned:
                    bm.free()
        counts, geom, skip = mesh_cache[me.name]
        if skip:
            skipped[name] = sorted(skip)
        for key, n in counts.items():
            add(key, name, n)
        if geometry_out is not None:
            geometry_out[name] = geom

    empties = {o for o in objects if o.type == "EMPTY" and o.children
               and o.instance_type != "COLLECTION"}
    empties |= {o.parent for o in objects if o.parent and o.parent.type == "EMPTY"
                and o.parent.instance_type != "COLLECTION"}
    for e in empties:
        add("empty_parent", e.name, len(e.children))

    issues = {}
    for key, *_ in CHECKS:
        if key in found:
            issues[key] = dict(CHECK_INFO[key], objects=found[key], items=[])

    if opt.on("material_unused"):
        # Fake-user materials are kept on purpose, so they don't count.
        unused = sorted(m.name for m in bpy.data.materials if m.users == 0)
        if unused:
            issues["material_unused"] = dict(CHECK_INFO["material_unused"],
                                             objects={}, items=unused)

    return {"objects_checked": len(objects), "meshes_checked": len(meshes),
            "checked": [o.name for o in meshes], "issues": issues, "skipped": skipped}


SCENE_WIDE = {"empty_parent", "material_unused"}


def refresh_objects(report, objects, opt, geometry):
    """Re-check a few objects and merge the results into an existing report."""
    names = {o.name for o in objects}
    old_counts = {k: {n: report["issues"][k]["objects"][n] for n in names
                      if n in report["issues"].get(k, {}).get("objects", {})}
                  for k in HEAVY if k in report["issues"]}
    old_geom = {n: {k: g for k, g in geometry.get(n, {}).items() if k in HEAVY} for n in names}
    for key, data in report["issues"].items():
        if key not in SCENE_WIDE:
            for n in names:
                data["objects"].pop(n, None)
    sub = check_objects(objects, opt, geometry_out=geometry)
    for key, data in sub["issues"].items():
        if key in SCENE_WIDE:
            continue
        report["issues"].setdefault(key, dict(data, objects={}))["objects"].update(data["objects"])
    # Dense meshes skipped heavy checks: keep their last result.
    for n, keys in sub.get("skipped", {}).items():
        for k in keys:
            if n in old_counts.get(k, {}):
                report["issues"].setdefault(k, dict(CHECK_INFO[k], objects={}, items=[]))[
                    "objects"][n] = old_counts[k][n]
            if k in old_geom.get(n, {}):
                geometry.setdefault(n, {})[k] = old_geom[n][k]
    ordered = {}
    for key, *_ in CHECKS:
        data = report["issues"].get(key)
        if data and (data["objects"] or data["items"]):
            ordered[key] = data
    report["issues"] = ordered
    return report


def ordered(issues):
    out = {}
    for key, *_ in CHECKS:
        data = issues.get(key)
        if data and (data["objects"] or data["items"]):
            out[key] = data
    return out


def refresh_issue(report, key, objects, opt, geometry):
    """Re-run one check over objects and replace that issue (and its highlight) in the report."""
    geo = {}
    sub = check_objects(objects, replace(opt, enabled=frozenset({key})), geometry_out=geo)
    report["issues"].pop(key, None)
    if key in sub["issues"]:
        report["issues"][key] = sub["issues"][key]
    for per in geometry.values():
        per.pop(key, None)
    for name, per in geo.items():
        if key in per:
            geometry.setdefault(name, {})[key] = per[key]
    report["issues"] = ordered(report["issues"])
    return report


def forget(report, geometry, names):
    """Drop objects (by name) from the report and the highlight, e.g. after a rename."""
    for data in report["issues"].values():
        for n in names:
            data["objects"].pop(n, None)
    for n in names:
        geometry.pop(n, None)
    report["issues"] = ordered(report["issues"])


def summary_line(report):
    issues = report["issues"]
    errors = sum(1 for i in issues.values() if i["severity"] == "ERROR")
    if not issues:
        return f"{report['objects_checked']} objects, no issues"
    return f"{report['objects_checked']} objects, {len(issues)} issues, {errors} errors"
