# SPDX-License-Identifier: GPL-3.0-or-later
"""OM3D Asset Prep: Scene Prep, Bake Prep and Batch Export in one add-on.

Scene Prep checks (with an xView style viewport highlight) and fixes.
Bake Prep pairs _low/_high objects, checks pairs, explodes them and writes bake groups.
Batch Export writes one FBX per asset with Unreal, Substance and Marmoset presets.
"""
import os

import bpy

from . import bake_ops
from . import export_ops
from . import live
from . import naming
from . import operators
from . import overlay
from . import scene_checks
from .operators import issue_rows


def _read_version():
    try:
        with open(os.path.join(os.path.dirname(__file__), "blender_manifest.toml"),
                  encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("version"):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "?"


VERSION = _read_version()

SEVERITY_ICON = {"ERROR": "CANCEL", "WARNING": "ERROR", "INFO": "INFO"}


def _fmt(n):
    """Short counts so labels don't truncate in the sidebar: 1208 -> 1.2k."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n / 1000:.1f}k"
    return f"{round(n / 1000)}k"


def _redraw(self, context):
    overlay.invalidate()


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------

class OM3D_AP_Preferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    prefix: bpy.props.StringProperty(name="Prefix", default="SM_",
                                     description="Static mesh prefix at the start of every name")
    separator: bpy.props.StringProperty(name="Separator", default="_", maxlen=1,
                                        description="Character between name parts")
    low_suffix: bpy.props.StringProperty(name="Low Suffix", default="low")
    high_suffix: bpy.props.StringProperty(name="High Suffix", default="high")
    require_suffix: bpy.props.BoolProperty(
        name="Require Low/High Suffix", default=True,
        description="Names must end in the low or high suffix to pass")
    blender_defaults_are_junk: bpy.props.BoolProperty(
        name="Flag Blender Default Names", default=True,
        description="Treat Cube, Plane, Cylinder and friends as junk names")
    zero_area_threshold: bpy.props.FloatProperty(
        name="Collapsed Face Threshold", default=1e-10, min=0.0, precision=12,
        description="Faces at or below this area (local units squared) count as collapsed")
    overlap_tolerance: bpy.props.FloatProperty(
        name="Overlap Tolerance", default=0.0001, min=0.0, precision=5, unit="LENGTH",
        description="Distance used by overlapping verts, overlapping faces and T-vert checks")
    highlight_color: bpy.props.FloatVectorProperty(
        name="Highlight Color", subtype="COLOR_GAMMA", size=3, min=0.0, max=1.0,
        default=(0.55, 1.0, 0.55), update=_redraw,
        description="Color of the viewport highlight")
    highlight_style: bpy.props.EnumProperty(
        name="Highlight Style",
        items=[("VERTS", "Vertices", "A dot on each problem vertex (no face fill)", "VERTEXSEL", 0),
               ("FACES", "Faces and Edges", "Fill problem faces and draw problem edges", "FACESEL", 1)],
        default="VERTS", update=_redraw,
        description="How the viewport highlight draws problems")
    highlight_xray: bpy.props.BoolProperty(
        name="Highlight Through Mesh", default=False, update=_redraw,
        description="Draw the highlight on top of everything, even when hidden behind geometry")
    apply_location: bpy.props.BoolProperty(
        name="Apply Location in Fix", default=False,
        description="Fix mode also zeroes location. Off by default, since Batch Export "
                    "moves each asset to the origin at export time")

    preset_unreal: bpy.props.PointerProperty(type=export_ops.PRESET_CLASSES["UNREAL"])
    preset_substance: bpy.props.PointerProperty(type=export_ops.PRESET_CLASSES["SUBSTANCE"])
    preset_marmoset: bpy.props.PointerProperty(type=export_ops.PRESET_CLASSES["MARMOSET"])

    def convention(self):
        return naming.Convention(prefix=self.prefix, sep=self.separator, low=self.low_suffix,
                                 high=self.high_suffix, require_suffix=self.require_suffix)

    def draw(self, context):
        layout = self.layout
        conv = self.convention()

        box = layout.box()
        box.label(text="Naming Convention", icon="SORTALPHA")
        row = box.row()
        row.prop(self, "prefix")
        row.prop(self, "separator")
        row = box.row()
        row.prop(self, "low_suffix")
        row.prop(self, "high_suffix")
        box.prop(self, "require_suffix")
        col = box.column(align=True)
        col.label(text=f"Pattern: {conv.describe()}")
        col.label(text=f"Example: {conv.make('Crate', 'Lid', 'low')}  /  "
                       f"{conv.make('Crate', 'Lid', 'high', 'bolts')}")

        box = layout.box()
        box.label(text="Checks", icon="VIEWZOOM")
        flow = box.grid_flow(row_major=True, columns=2, even_columns=True)
        for key, label, severity, _level in scene_checks.BUILTIN:
            flow.prop(self, f"use_{key}", text=label, icon=SEVERITY_ICON[severity])
        box.prop(self, "blender_defaults_are_junk")
        box.prop(self, "zero_area_threshold")
        box.prop(self, "overlap_tolerance")

        box = layout.box()
        box.label(text="Viewport Highlight", icon="OVERLAY")
        box.prop(self, "highlight_color")
        box.row().prop(self, "highlight_style", expand=True)
        box.prop(self, "highlight_xray")

        box = layout.box()
        box.label(text="Fix", icon="TOOL_SETTINGS")
        box.prop(self, "apply_location")


# One on/off toggle per built-in check, like xView's checker list.
for _key, _label, _sev, _lvl in scene_checks.BUILTIN:
    OM3D_AP_Preferences.__annotations__[f"use_{_key}"] = bpy.props.BoolProperty(
        name=_label, default=_key not in scene_checks.OFF_BY_DEFAULT,
        description=scene_checks.HELP.get(_key, ""))


# --------------------------------------------------------------------------
# Scene state
# --------------------------------------------------------------------------

class OM3D_AP_Issue(bpy.types.PropertyGroup):
    key: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    severity: bpy.props.StringProperty()
    level: bpy.props.StringProperty()
    count: bpy.props.IntProperty()
    object_count: bpy.props.IntProperty()
    names: bpy.props.StringProperty()
    counts: bpy.props.StringProperty()
    expanded: bpy.props.BoolProperty(name="Show Objects", default=False, update=_redraw)
    show_in_viewport: bpy.props.BoolProperty(
        name="Highlight", default=True, update=_redraw,
        description="Show this issue in the green viewport highlight")


class OM3D_AP_ScenePrepState(bpy.types.PropertyGroup):
    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[("SELECTED", "Selected", "Check selected objects"),
               ("VISIBLE", "Visible", "Check objects visible in the view layer"),
               ("ALL", "All", "Check every object in the scene")],
        default="VISIBLE",
    )
    issues: bpy.props.CollectionProperty(type=OM3D_AP_Issue)
    has_run: bpy.props.BoolProperty(default=False)
    objects_checked: bpy.props.IntProperty()
    meshes_checked: bpy.props.IntProperty()
    summary: bpy.props.StringProperty()
    last_run: bpy.props.StringProperty()
    show_overlay: bpy.props.BoolProperty(
        name="Highlight", default=True, update=_redraw,
        description="Highlight problem verts, edges and faces in the viewport")
    live_update: bpy.props.BoolProperty(
        name="Live", default=True,
        description="Re-check objects you edit, a moment after you stop "
                    "(paused while Topo Draw is running)")
    show_legend: bpy.props.BoolProperty(name="Legend", default=False)
    mode: bpy.props.EnumProperty(
        name="Mode",
        items=[("CHECK", "Check", "Report problems only", "VIEWZOOM", 0),
               ("FIX", "Fix", "Fix the problems you tick", "TOOL_SETTINGS", 1)],
        default="CHECK")
    fix_transforms: bpy.props.BoolProperty(
        name="Apply Rotation and Scale", default=True,
        description="Bake rotation and scale into the mesh. Children stay where they are")
    fix_negative: bpy.props.BoolProperty(
        name="Include Negative Scale", default=False,
        description="Also apply mirrored (negative) scale, flipping normals back so faces "
                    "still point outward")
    fix_origin: bpy.props.EnumProperty(
        name="Origin",
        items=[("KEEP", "Keep", "Leave origins where they are"),
               ("BOUNDS", "Bounds Center", "Center of the bounding box"),
               ("BOTTOM", "Bottom Center", "Center of the bottom of the bounding box, for props on the ground"),
               ("WORLD", "World Origin", "The scene origin")],
        default="KEEP")
    fix_empty_parents: bpy.props.BoolProperty(
        name="Remove Group Empties", default=False,
        description="Delete empties that only group objects. Their children keep their place")
    fix_flatten: bpy.props.BoolProperty(
        name="Flatten Hierarchy", default=False,
        description="Unparent every object in scope, keeping its place")
    fix_merge: bpy.props.BoolProperty(
        name="Merge by Distance", default=False,
        description="Merge overlapping verts within the overlap tolerance in preferences")
    fix_degenerate: bpy.props.BoolProperty(
        name="Remove Collapsed Faces", default=False,
        description="Dissolve faces squashed into a line or point (thin slivers may leave a seam)")
    fix_triangulate: bpy.props.BoolProperty(
        name="Triangulate N-gons", default=False,
        description="Split faces with more than 4 sides into triangles (beauty fill)")
    fix_loose: bpy.props.BoolProperty(
        name="Delete Loose Geometry", default=False,
        description="Delete verts and edges that aren't part of any face")
    fix_normals: bpy.props.BoolProperty(
        name="Fix Flipped Normals", default=False,
        description="Recalculate normals outward, only on the parts that have flipped faces")
    fix_slots: bpy.props.BoolProperty(
        name="Remove Empty Material Slots", default=True,
        description="Remove slots with no material that no faces use")
    fix_purge: bpy.props.BoolProperty(
        name="Delete Unused Materials", default=True,
        description="Delete materials nothing uses (fake-user materials are kept)")
    fix_strip_dup: bpy.props.BoolProperty(
        name="Strip .001 Suffixes", default=True,
        description="Remove .001 style suffixes when the plain name is free")
    fix_data_names: bpy.props.BoolProperty(
        name="Match Mesh Data Names", default=True,
        description="Rename each mesh data block to its object's name")
    fix_rename: bpy.props.BoolProperty(
        name="Rename to Convention", default=False,
        description="Rename objects that don't match the naming convention. Junk names "
                    "become Part01, Part02...")
    fix_asset_name: bpy.props.StringProperty(
        name="Asset", default="",
        description="Asset name for renaming. Blank uses each object's collection name")
    fix_lines: bpy.props.StringProperty()
    fix_summary: bpy.props.StringProperty()
    fix_time: bpy.props.StringProperty()
    fix_was_preview: bpy.props.BoolProperty(default=False)
    fix_has_run: bpy.props.BoolProperty(default=False)
    show_fix_log: bpy.props.BoolProperty(name="Show All", default=False)


# --------------------------------------------------------------------------
# Panels
# --------------------------------------------------------------------------

LEGEND = (
    ("CANCEL", "Error: fix before export"),
    ("ERROR", "Warning: probably a problem"),
    ("INFO", "Info: worth a look, often fine"),
    ("HIDE_OFF", "Show in the green highlight"),
    ("VERTEXSEL", "Highlight as dots on vertices"),
    ("FACESEL", "Highlight as filled faces"),
    ("RESTRICT_SELECT_OFF", "Select the objects"),
    ("EDITMODE_HLT", "Select the problem parts"),
    ("FILE_REFRESH", "Re-check just this issue"),
    ("MODIFIER", "Fix this issue (open it to see the button)"),
    ("TRIA_DOWN", "Open an issue to show only its dots"),
    ("QUESTION", "Hover an issue name for details"),
)


class OM3D_PT_asset_prep(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OM3D"
    bl_label = f"Asset Prep  v{VERSION}"

    def draw(self, context):
        prefs = context.preferences.addons[__package__].preferences
        col = self.layout.column(align=True)
        col.label(text=prefs.convention().describe(), icon="SORTALPHA")


def _issue_buttons(row, item, object_name=""):
    """Select and edit buttons, same on parent and child rows."""
    if item.level == "FILE":
        return
    op = row.operator("om3d.scene_prep_select", text="", icon="RESTRICT_SELECT_OFF")
    op.key = item.key
    op.object_name = object_name
    if item.level == "ELEMENT":
        op = row.operator("om3d.scene_prep_select_elements", text="", icon="EDITMODE_HLT")
        op.key = item.key
        op.object_name = object_name


def _draw_fix(layout, state, prefs):
    box = layout.box()
    col = box.column(align=True)
    col.label(text="Transforms", icon="ORIENTATION_GLOBAL")
    col.prop(state, "fix_transforms")
    sub = col.column(align=True)
    sub.active = state.fix_transforms
    sub.prop(state, "fix_negative")
    sub.prop(prefs, "apply_location", text="Apply Location Too")
    col.separator()
    col.prop(state, "fix_origin")
    col.separator()
    col.label(text="Hierarchy", icon="OUTLINER")
    col.prop(state, "fix_empty_parents")
    col.prop(state, "fix_flatten")
    col.separator()
    col.label(text="Mesh", icon="MESH_DATA")
    col.prop(state, "fix_merge")
    col.prop(state, "fix_loose")
    col.prop(state, "fix_normals")
    col.prop(state, "fix_degenerate")
    col.prop(state, "fix_triangulate")
    col.separator()
    col.label(text="Materials", icon="MATERIAL")
    col.prop(state, "fix_slots")
    col.prop(state, "fix_purge")
    col.separator()
    col.label(text="Names", icon="SORTALPHA")
    col.prop(state, "fix_strip_dup")
    col.prop(state, "fix_data_names")
    col.prop(state, "fix_rename")
    if state.fix_rename:
        col.prop(state, "fix_asset_name")

    row = layout.row(align=True)
    row.scale_y = 1.4
    op = row.operator("om3d.scene_prep_fix", text="Preview", icon="HIDE_OFF")
    op.dry_run = True
    op = row.operator("om3d.scene_prep_fix", text="Fix", icon="CHECKMARK")
    op.dry_run = False
    layout.label(text="Fix changes the scene. Ctrl+Z undoes it.", icon="INFO")

    if not state.fix_has_run:
        return
    box = layout.box()
    head = box.row()
    head.label(text=("Preview " if state.fix_was_preview else "Fixed ") + state.fix_time,
               icon="HIDE_OFF" if state.fix_was_preview else "CHECKMARK")
    box.label(text=state.fix_summary)
    lines = [l for l in state.fix_lines.split("\n") if l]
    shown = lines if state.show_fix_log else lines[:12]
    col = box.column(align=True)
    group = None
    for line in shown:
        head, _, rest = line.partition(": ")
        if head != group:
            group = head
            col.separator()
            col.label(text=head, icon="ERROR" if head.startswith("Skipped") else "CHECKMARK")
        obj, sep, detail = rest.partition(": ")
        if sep:
            col.label(text=obj, icon="OBJECT_DATA")
            for part in detail.split(", "):
                col.label(text="      " + part)
        else:
            col.label(text="   " + rest)
    if len(lines) > 12:
        box.prop(state, "show_fix_log", text=f"Show all {len(lines)}" if not state.show_fix_log
                 else "Show fewer", toggle=True)


class OM3D_PT_scene_prep(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OM3D"
    bl_parent_id = "OM3D_PT_asset_prep"
    bl_label = "Scene Prep"

    def draw(self, context):
        layout = self.layout
        state = context.scene.om3d_scene_prep
        prefs = context.preferences.addons[__package__].preferences

        r = layout.row()
        r.alignment = "RIGHT"
        op = r.operator("om3d.scene_prep_clear", text="Reset", icon="LOOP_BACK", emboss=False)
        op.full = True

        layout.row().prop(state, "scope", expand=True)
        layout.row().prop(state, "mode", expand=True)
        if state.mode == "FIX":
            _draw_fix(layout, state, prefs)
        else:
            row = layout.row()
            row.scale_y = 1.4
            row.operator("om3d.scene_prep_check", icon="VIEWZOOM")

        if not state.has_run:
            if state.mode == "CHECK":
                layout.label(text="Check only. Nothing is changed.", icon="LOCKED")
            return
        if state.mode == "CHECK":
            layout.label(text=f"Check only. Last run {state.last_run}", icon="LOCKED")
        else:
            layout.label(text=f"Issues as of {state.last_run}", icon="VIEWZOOM")

        head = layout.row()
        head.label(text=state.summary)
        head.operator("om3d.scene_prep_clear", text="", icon="X", emboss=False)

        row = layout.row(align=True)
        row.prop(state, "show_overlay", toggle=True, icon="OVERLAY")
        sub = row.row(align=True)
        sub.active = state.show_overlay
        sub.prop(prefs, "highlight_color", text="")
        sub.prop(prefs, "highlight_style", expand=True, icon_only=True)
        sub.prop(prefs, "highlight_xray", text="", icon="XRAY")
        row.prop(state, "live_update", toggle=True, icon="FILE_REFRESH")
        if state.show_overlay and not overlay.has_geometry() and any(
                i.level == "ELEMENT" for i in state.issues):
            layout.label(text="Run Check Scene to show the highlight", icon="INFO")

        legend = layout.column(align=True)
        legend.prop(state, "show_legend", emboss=False,
                    icon="TRIA_DOWN" if state.show_legend else "TRIA_RIGHT")
        if state.show_legend:
            box = legend.box().column(align=True)
            for icon, text in LEGEND:
                box.label(text=text, icon=icon)

        if not state.issues:
            layout.label(text="No issues found", icon="CHECKMARK")
            return

        for item in state.issues:
            box = layout.box()
            row = box.row(align=True)
            row.prop(item, "expanded", text="", emboss=False,
                     icon="TRIA_DOWN" if item.expanded else "TRIA_RIGHT")
            name = row.row(align=True)
            name.alert = item.severity == "ERROR"
            name.alignment = "LEFT"
            op = name.operator("om3d.scene_prep_toggle", text=item.label, emboss=False,
                               icon=SEVERITY_ICON.get(item.severity, "DOT"))
            op.key = item.key
            cnt = row.row(align=True)
            cnt.alignment = "RIGHT"
            cnt.label(text=_fmt(item.count if item.level == "ELEMENT"
                                else (item.object_count or item.count)))
            if item.level == "ELEMENT":
                row.prop(item, "show_in_viewport", text="", emboss=False,
                         icon="HIDE_OFF" if item.show_in_viewport else "HIDE_ON")
            _issue_buttons(row, item)
            op = row.operator("om3d.scene_prep_refresh_issue", text="", icon="FILE_REFRESH")
            op.key = item.key

            if not item.expanded:
                continue
            fix = operators.ISSUE_FIX.get(item.key)
            if fix:
                fr = box.row()
                op = fr.operator("om3d.scene_prep_fix_issue", text=fix[0], icon="MODIFIER")
                op.key = item.key
            col = box.column(align=True)
            for obj_name, n in issue_rows(item):
                r = col.row(align=True)
                if item.level == "FILE":
                    r.label(text=obj_name, icon="MATERIAL")
                    continue
                r.label(text=obj_name, icon="OBJECT_DATA")
                if n > 1 or item.level == "ELEMENT":
                    c = r.row(align=True)
                    c.alignment = "RIGHT"
                    c.label(text=_fmt(n))
                _issue_buttons(r, item, obj_name)


classes = (
    *export_ops.state_classes,
    *bake_ops.state_classes,
    OM3D_AP_Preferences,
    OM3D_AP_Issue,
    OM3D_AP_ScenePrepState,
    *operators.classes,
    *bake_ops.op_classes,
    *export_ops.op_classes,
    OM3D_PT_asset_prep,
    OM3D_PT_scene_prep,
    bake_ops.OM3D_PT_bake_prep,
    export_ops.OM3D_PT_batch_export,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.om3d_scene_prep = bpy.props.PointerProperty(type=OM3D_AP_ScenePrepState)
    bpy.types.Scene.om3d_bake_prep = bpy.props.PointerProperty(type=bake_ops.OM3D_AP_BakeState)
    bpy.types.Scene.om3d_export = bpy.props.PointerProperty(type=export_ops.OM3D_AP_ExportState)
    overlay.register()
    live.register()
    if bake_ops.clear_on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(bake_ops.clear_on_load)


def unregister():
    if bake_ops.clear_on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(bake_ops.clear_on_load)
    live.unregister()
    overlay.unregister()
    del bpy.types.Scene.om3d_export
    del bpy.types.Scene.om3d_bake_prep
    del bpy.types.Scene.om3d_scene_prep
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
