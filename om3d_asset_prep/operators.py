# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene Prep operators. Thin wrappers around scene_checks."""
import time

import bmesh
import bpy

from . import fixes
from . import overlay
from . import scene_checks

SEP = "\n"


def store_report(state, report, keep_time=False):
    """Copy a report dict into the scene's UI state, keeping expand/eye toggles."""
    flags = {i.key: (i.expanded, i.show_in_viewport) for i in state.issues}
    state.issues.clear()
    for key, data in report["issues"].items():
        item = state.issues.add()
        item.key = key
        item.label = data["label"]
        item.severity = data["severity"]
        item.level = data["level"]
        if key in flags:
            item.expanded, item.show_in_viewport = flags[key]
        if data["level"] == "FILE":
            item.object_count = 0
            item.count = len(data["items"])
            item.names = SEP.join(data["items"])
            item.counts = SEP.join("1" for _ in data["items"])
        else:
            names = sorted(data["objects"])
            item.object_count = len(names)
            item.count = sum(data["objects"].values())
            item.names = SEP.join(names)
            item.counts = SEP.join(str(data["objects"][n]) for n in names)
    state.has_run = True
    state.objects_checked = report["objects_checked"]
    state.meshes_checked = report["meshes_checked"]
    state.summary = scene_checks.summary_line(report)
    if not keep_time:
        state.last_run = time.strftime("%H:%M:%S")


def issue_rows(item):
    """[(name, count)] for an issue item."""
    if not item.names:
        return []
    names = item.names.split(SEP)
    counts = [int(c) for c in item.counts.split(SEP)] if item.counts else [0] * len(names)
    return list(zip(names, counts))


def _find_issue(state, key):
    for item in state.issues:
        if item.key == key:
            return item
    return None


def _target_objects(context, state, key, object_name):
    item = _find_issue(state, key)
    if item is None:
        return []
    names = [object_name] if object_name else [n for n, _ in issue_rows(item)]
    objs = []
    for n in names:
        obj = context.scene.objects.get(n)
        if obj is not None and obj.visible_get() and obj.name in context.view_layer.objects:
            objs.append(obj)
    return objs


def _label(key):
    info = scene_checks.CHECK_INFO.get(key)
    return info["label"].lower() if info else key


class OM3D_OT_scene_prep_check(bpy.types.Operator):
    """Check the scene for prep problems. Reports only, nothing is changed"""
    bl_idname = "om3d.scene_prep_check"
    bl_label = "Check Scene"
    bl_options = {"REGISTER"}

    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[("PANEL", "Panel Setting", "Use the scope set in the Scene Prep panel"),
               ("SELECTED", "Selected", "Selected objects"),
               ("VISIBLE", "Visible", "Objects visible in the view layer"),
               ("ALL", "All", "Every object in the scene")],
        default="PANEL",
    )

    def execute(self, context):
        state = context.scene.om3d_scene_prep
        scope = state.scope if self.scope == "PANEL" else self.scope
        objs = scene_checks.gather_objects(context, scope)
        geometry = {}
        report = scene_checks.check_objects(objs, scene_checks.options_from_prefs(context),
                                            geometry_out=geometry)
        store_report(state, report)
        overlay.set_report(context.scene, report, geometry)
        self.report({"INFO"}, state.summary)
        return {"FINISHED"}


class OM3D_OT_scene_prep_toggle(bpy.types.Operator):
    """Show or hide the objects with this issue"""
    bl_idname = "om3d.scene_prep_toggle"
    bl_label = "Show Objects"
    bl_options = {"INTERNAL"}

    key: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        text = scene_checks.HELP.get(properties.key, "")
        return (text + "\nClick to list the objects.") if text else "Click to list the objects."

    def execute(self, context):
        item = _find_issue(context.scene.om3d_scene_prep, self.key)
        if item:
            item.expanded = not item.expanded
        return {"FINISHED"}


class OM3D_OT_scene_prep_select(bpy.types.Operator):
    """Select the objects with this issue"""
    bl_idname = "om3d.scene_prep_select"
    bl_label = "Select Objects"
    bl_options = {"REGISTER", "UNDO"}

    key: bpy.props.StringProperty()
    object_name: bpy.props.StringProperty(description="Only this object (blank = all)")

    @classmethod
    def description(cls, context, properties):
        if properties.object_name:
            return f"Select {properties.object_name}"
        return f"Select every object with {_label(properties.key)}"

    def execute(self, context):
        state = context.scene.om3d_scene_prep
        objs = _target_objects(context, state, self.key, self.object_name)
        if not objs:
            self.report({"WARNING"}, "Nothing to select (objects renamed, hidden or deleted?)")
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for o in context.selected_objects:
            o.select_set(False)
        for o in objs:
            o.select_set(True)
        context.view_layer.objects.active = objs[0]
        self.report({"INFO"}, f"Selected {len(objs)} object(s)")
        return {"FINISHED"}


class OM3D_OT_scene_prep_select_elements(bpy.types.Operator):
    """Go to Edit Mode with the problem verts, edges or faces selected"""
    bl_idname = "om3d.scene_prep_select_elements"
    bl_label = "Select Elements"
    bl_options = {"REGISTER", "UNDO"}

    key: bpy.props.StringProperty()
    object_name: bpy.props.StringProperty(description="Only this object (blank = all)")

    @classmethod
    def description(cls, context, properties):
        where = properties.object_name or "every object with this issue"
        return f"Edit Mode on {where}, with the {_label(properties.key)} selected"

    def execute(self, context):
        if self.key not in scene_checks.ELEMENT_CHECKS:
            return {"CANCELLED"}
        if scene_checks.topo_draw_running():
            self.report({"WARNING"}, "Exit Topo Draw first")
            return {"CANCELLED"}
        state = context.scene.om3d_scene_prep
        objs = [o for o in _target_objects(context, state, self.key, self.object_name)
                if o.type == "MESH"]
        if not objs:
            self.report({"WARNING"}, "Nothing to select (objects renamed, hidden or deleted?)")
            return {"CANCELLED"}
        opt = scene_checks.options_from_prefs(context)

        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        for o in context.selected_objects:
            o.select_set(False)
        for o in objs:
            o.select_set(True)
        context.view_layer.objects.active = objs[0]
        bpy.ops.object.mode_set(mode="EDIT")

        # Work out which select mode fits before touching the selection.
        found_by_obj = []
        kinds = set()
        seen_meshes = set()
        for o in objs:
            if o.data.name in seen_meshes:  # instances share one edit mesh
                continue
            seen_meshes.add(o.data.name)
            bm = bmesh.from_edit_mesh(o.data)  # one wrapper per mesh, held below
            found = scene_checks.element_findings(bm, self.key, opt)
            kinds |= {k for k, v in found.items() if v}
            found_by_obj.append((o, bm, found))
        ts = context.tool_settings
        if kinds == {"faces"}:
            ts.mesh_select_mode = (False, False, True)
        elif "verts" in kinds:
            ts.mesh_select_mode = (True, "edges" in kinds, False)
        else:
            ts.mesh_select_mode = (False, True, "faces" in kinds)

        total = 0
        for o, bm, found in found_by_obj:
            for seq in (bm.faces, bm.edges, bm.verts):
                for el in seq:
                    el.select = False
            for kind in ("faces", "edges", "verts"):
                for el in found.get(kind, ()):
                    el.select_set(True)
                    total += 1
            bm.select_flush_mode()
            bmesh.update_edit_mesh(o.data, loop_triangles=False, destructive=False)
        self.report({"INFO"}, f"Selected {total} element(s) on {len(objs)} object(s)")
        return {"FINISHED"}


class OM3D_OT_scene_prep_clear(bpy.types.Operator):
    """Clear the Scene Prep report and the viewport highlight"""
    bl_idname = "om3d.scene_prep_clear"
    bl_label = "Clear Report"
    bl_options = {"REGISTER"}

    full: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})

    @classmethod
    def description(cls, context, props):
        if props.full:
            return ("Reset Scene Prep: clear the report, the highlight and the fix log, and go "
                    "back to Check mode. Nothing in the scene changes")
        return "Clear the Scene Prep report and the viewport highlight"

    def execute(self, context):
        clear_state(context.scene.om3d_scene_prep, full=self.full)
        overlay.clear()
        return {"FINISHED"}


def clear_state(state, full=False):
    state.issues.clear()
    state.has_run = False
    state.summary = ""
    state.last_run = ""
    if full:
        state.fix_has_run = False
        state.fix_lines = state.fix_summary = state.fix_time = ""
        state.fix_was_preview = False
        state.show_fix_log = False
        state.show_legend = False
        state.mode = "CHECK"


def fix_options(context):
    st = context.scene.om3d_scene_prep
    addon = context.preferences.addons.get(__package__)
    p = addon.preferences if addon else None
    return fixes.FixOptions(
        remove_empty_parents=st.fix_empty_parents, flatten=st.fix_flatten,
        merge_by_distance=st.fix_merge, delete_loose=st.fix_loose, recalc_normals=st.fix_normals,
        transforms=st.fix_transforms, negative_scale=st.fix_negative,
        apply_location=p.apply_location if p else False, origin=st.fix_origin,
        remove_empty_slots=st.fix_slots, purge_materials=st.fix_purge,
        rename=st.fix_rename, asset_name=st.fix_asset_name,
        dissolve_degenerate=st.fix_degenerate, triangulate_ngons=st.fix_triangulate,
        strip_duplicates=st.fix_strip_dup, match_data_names=st.fix_data_names,
        tolerance=p.overlap_tolerance if p else 1e-4,
        convention=p.convention() if p else None,
    )


def _recheck(context):
    state = context.scene.om3d_scene_prep
    geometry = {}
    report = scene_checks.check_objects(scene_checks.gather_objects(context, state.scope),
                                        scene_checks.options_from_prefs(context),
                                        geometry_out=geometry)
    store_report(state, report)
    overlay.set_report(context.scene, report, geometry)


class OM3D_OT_scene_prep_fix(bpy.types.Operator):
    """Apply the ticked fixes to the objects in scope. One undo step"""
    bl_idname = "om3d.scene_prep_fix"
    bl_label = "Fix"
    bl_options = {"REGISTER", "UNDO"}

    dry_run: bpy.props.BoolProperty(name="Preview Only", default=False)
    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[("PANEL", "Panel Setting", "Use the scope set in the Scene Prep panel"),
               ("SELECTED", "Selected", "Selected objects"),
               ("VISIBLE", "Visible", "Objects visible in the view layer"),
               ("ALL", "All", "Every object in the scene")],
        default="PANEL",
    )

    @classmethod
    def description(cls, context, properties):
        if properties.dry_run:
            return "List what the ticked fixes would change. Nothing is changed"
        return "Apply the ticked fixes to the objects in scope. Ctrl+Z undoes the whole fix"

    def invoke(self, context, event):
        if self.dry_run:
            return self.execute(context)
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if scene_checks.topo_draw_running():
            self.report({"WARNING"}, "Exit Topo Draw first")
            return {"CANCELLED"}
        state = context.scene.om3d_scene_prep
        scope = state.scope if self.scope == "PANEL" else self.scope
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        objs = scene_checks.gather_objects(context, scope)
        try:
            rep = fixes.run_fixes(objs, fix_options(context), dry_run=self.dry_run)
        except Exception as exc:
            # Keep the undo step: Ctrl+Z rolls back anything done before the error.
            self.report({"ERROR"}, f"Fix stopped: {exc}. Ctrl+Z undoes what ran.")
            return {"FINISHED"}
        state.fix_lines = SEP.join(rep.lines())
        state.fix_summary = rep.summary()
        state.fix_was_preview = self.dry_run
        state.fix_time = time.strftime("%H:%M:%S")
        state.fix_has_run = True
        if not self.dry_run:
            context.view_layer.update()
            _recheck(context)
        self.report({"INFO"}, state.fix_summary)
        return {"FINISHED"}


# issue key -> (button text, the one fix it runs)
ISSUE_FIX = {
    "scale_negative": ("Apply scale (mirrored too)", dict(transforms=True, negative_scale=True)),
    "scale_nonuniform": ("Apply rotation and scale", dict(transforms=True)),
    "scale_unapplied": ("Apply rotation and scale", dict(transforms=True)),
    "rotation_unapplied": ("Apply rotation and scale", dict(transforms=True)),
    "flipped_normals": ("Recalculate flipped normals", dict(recalc_normals=True)),
    "zero_area": ("Remove collapsed faces", dict(dissolve_degenerate=True)),
    "ngons": ("Triangulate n-gons", dict(triangulate_ngons=True)),
    "loose_geometry": ("Delete loose verts and edges", dict(delete_loose=True)),
    "overlapping_verts": ("Merge by distance", dict(merge_by_distance=True)),
    "empty_parent": ("Remove group empties", dict(remove_empty_parents=True)),
    "material_empty_slot": ("Remove empty slots", dict(remove_empty_slots=True)),
    "material_unused": ("Delete unused materials", dict(purge_materials=True)),
    "name_duplicate": ("Strip .001 suffixes", dict(strip_duplicates=True)),
    "name_data_mismatch": ("Match mesh data names", dict(match_data_names=True)),
    "name_junk": ("Rename to convention", dict(rename=True)),
    "name_convention": ("Rename to convention", dict(rename=True)),
}


def _scope_objects(context):
    return scene_checks.gather_objects(context, context.scene.om3d_scene_prep.scope)


class OM3D_OT_scene_prep_refresh_issue(bpy.types.Operator):
    """Re-check just this issue, without running the whole scene check"""
    bl_idname = "om3d.scene_prep_refresh_issue"
    bl_label = "Refresh Issue"
    bl_options = {"REGISTER"}

    key: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        return f"Re-check {_label(properties.key)} only, without running the whole scene check"

    def execute(self, context):
        state = context.scene.om3d_scene_prep
        report = overlay.CACHE["report"]
        if report is None or overlay.CACHE["scene"] != context.scene.name:
            return bpy.ops.om3d.scene_prep_check()
        scene_checks.refresh_issue(report, self.key, _scope_objects(context),
                                   scene_checks.options_from_prefs(context), overlay.CACHE["geometry"])
        store_report(state, report, keep_time=True)
        overlay.invalidate()
        item = _find_issue(state, self.key)
        self.report({"INFO"}, f"{_label(self.key).capitalize()}: "
                              f"{item.count if item else 0} left")
        return {"FINISHED"}


class OM3D_OT_scene_prep_fix_issue(bpy.types.Operator):
    """Run the fix for this issue on the objects listed under it. Ctrl+Z undoes it"""
    bl_idname = "om3d.scene_prep_fix_issue"
    bl_label = "Fix Issue"
    bl_options = {"REGISTER", "UNDO"}

    key: bpy.props.StringProperty()

    @classmethod
    def description(cls, context, properties):
        text = ISSUE_FIX.get(properties.key, ("", None))[0]
        return f"{text} on the objects listed under this issue. Ctrl+Z undoes it"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        if self.key not in ISSUE_FIX:
            return {"CANCELLED"}
        if scene_checks.topo_draw_running():
            self.report({"WARNING"}, "Exit Topo Draw first")
            return {"CANCELLED"}
        state = context.scene.om3d_scene_prep
        item = _find_issue(state, self.key)
        if item is None:
            return {"CANCELLED"}
        if context.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        names = [n for n, _ in issue_rows(item)] if item.level != "FILE" else []
        objs = [context.scene.objects[n] for n in names if n in context.scene.objects]
        base = fix_options(context)
        opt = fixes.only(**ISSUE_FIX[self.key][1])
        opt.tolerance, opt.convention, opt.asset_name = base.tolerance, base.convention, base.asset_name
        opt.apply_location = base.apply_location
        old_names = [o.name for o in objs]
        try:
            rep = fixes.run_fixes(objs, opt)
        except Exception as exc:
            self.report({"ERROR"}, f"Fix stopped: {exc}. Ctrl+Z undoes what ran.")
            return {"FINISHED"}
        state.fix_lines = SEP.join(rep.lines())
        state.fix_summary = rep.summary()
        state.fix_was_preview = False
        state.fix_time = time.strftime("%H:%M:%S")
        state.fix_has_run = True
        context.view_layer.update()
        report = overlay.CACHE["report"]
        if report is None or overlay.CACHE["scene"] != context.scene.name:
            _recheck(context)
        else:
            geometry = overlay.CACHE["geometry"]
            chk = scene_checks.options_from_prefs(context)
            # like Live: skip the two slow checks on dense meshes and keep their last result
            chk.skip_heavy_over = scene_checks.LIVE_HEAVY_LIMIT
            alive = [o for o in objs if fixes._alive(o)]
            gone = [old for old, o in zip(old_names, objs) if not fixes._alive(o) or o.name != old]
            scene_checks.forget(report, geometry, gone)  # renamed or deleted objects only
            meshes = [o for o in alive if o.type == "MESH"]
            if meshes:
                scene_checks.refresh_objects(report, meshes, chk, geometry)
            for key in scene_checks.SCENE_WIDE:
                scene_checks.refresh_issue(report, key, _scope_objects(context), chk, geometry)
            store_report(state, report, keep_time=True)
            overlay.invalidate()
        self.report({"INFO"}, rep.summary())
        return {"FINISHED"}


classes = (
    OM3D_OT_scene_prep_check,
    OM3D_OT_scene_prep_toggle,
    OM3D_OT_scene_prep_select,
    OM3D_OT_scene_prep_select_elements,
    OM3D_OT_scene_prep_clear,
    OM3D_OT_scene_prep_fix,
    OM3D_OT_scene_prep_refresh_issue,
    OM3D_OT_scene_prep_fix_issue,
)
