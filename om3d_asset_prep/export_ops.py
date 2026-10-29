# SPDX-License-Identifier: GPL-3.0-or-later
"""Batch Export: presets (stored in add-on preferences), state, operators and panel.
Core logic lives in export.py."""
import os
import time

import bpy

from . import export
from . import overlay
from . import scene_checks

SEP = "\n"

AXIS_ITEMS = [(a, a, "") for a in ("X", "Y", "Z", "-X", "-Y", "-Z")]


def _prefs(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences if addon else None


def _preset_class(key):
    """A preset PropertyGroup whose defaults come from export.PRESET_DEFAULTS[key]."""
    d = export.PRESET_DEFAULTS[key]
    ann = {
        "axis_forward": bpy.props.EnumProperty(name="Forward", items=AXIS_ITEMS,
                                               default=d["axis_forward"]),
        "axis_up": bpy.props.EnumProperty(name="Up", items=AXIS_ITEMS, default=d["axis_up"]),
        "global_scale": bpy.props.FloatProperty(name="Scale", default=d["global_scale"],
                                                min=0.0001, max=10000.0),
        "apply_unit_scale": bpy.props.BoolProperty(
            name="Apply Unit Scale", default=d["apply_unit_scale"],
            description="Bake the scene's unit scale into the file"),
        "smoothing": bpy.props.EnumProperty(
            name="Smoothing",
            items=[("FACE", "Face", "Face smoothing groups"), ("EDGE", "Edge", "Edge smoothing"),
                   ("OFF", "Normals Only", "Write normals only")],
            default=d["smoothing"]),
        "triangulate": bpy.props.BoolProperty(name="Triangulate", default=d["triangulate"]),
        "include_high": bpy.props.BoolProperty(
            name="Include Highs", default=d["include_high"],
            description="Also write SM_<Asset>_high.fbx with the high-poly objects"),
        "collision": bpy.props.BoolProperty(
            name="UCX_ Collision", default=d["collision"],
            description="Export UCX_SM_<Asset>_... collision meshes with their asset"),
        "lods": bpy.props.BoolProperty(
            name="LODs", default=d["lods"],
            description="Objects ending in _LOD1, _LOD2... go to SM_<Asset>_LOD1.fbx and so on"),
        "strip_low": bpy.props.BoolProperty(
            name="Drop _low From Names", default=d["strip_low"],
            description="SM_Crate_Lid_low is written as SM_Crate_Lid (game engine names)"),
    }
    return type(f"OM3D_AP_ExportPreset{key.title()}", (bpy.types.PropertyGroup,),
                {"__annotations__": ann})


PRESET_CLASSES = {key: _preset_class(key) for key in export.PRESET_KEYS}


def _load_defaults(preset, key):
    for k, v in export.PRESET_DEFAULTS[key].items():
        setattr(preset, k, v)


def settings_from(context, key):
    prefs = _prefs(context)
    st = context.scene.om3d_export
    s = export.Settings(preset=key)
    if prefs is not None:
        p = getattr(prefs, "preset_" + key.lower())
        for k in export.PRESET_DEFAULTS[key]:
            setattr(s, k, getattr(p, k))
    else:
        for k, v in export.PRESET_DEFAULTS[key].items():
            setattr(s, k, v)
    s.group_by = st.group_by
    s.pivot = st.pivot
    s.merge_highs = st.merge_highs
    s.root = bpy.path.abspath(st.root) if st.root else ""
    return s


def do_export(context, objects, s, dry_run, op=None):
    """Plan, then (unless dry_run) write. Returns (lines for the panel, ok)."""
    conv = scene_checks.options_from_prefs(context).convention
    if s.root and st_relative_unsaved(context):
        msg = "Save the .blend first, or use an absolute export folder"
        if op:
            op.report({"ERROR"}, msg)
        return [f"ERR {msg}"], False
    report = overlay.CACHE["report"] if overlay.CACHE["scene"] == context.scene.name else None
    p = export.plan(context, objects, conv, s, report)
    if p.errors:
        if op:
            op.report({"ERROR"}, p.errors[0])
        return [f"ERR {e}" for e in p.errors], False
    files = [f for j in p.assets for f in j.files]
    if not files:
        if op:
            op.report({"WARNING"}, "Nothing to export")
        lines = [f"SKIP {n}: {why}" for n, why in p.skipped[:40]]
        return ["ERR Nothing to export"] + lines, False
    results = [] if dry_run else export.run(context, p, s)
    res = {r[0]: r for r in results}
    lines = []
    for job in p.assets:
        lines.append(f"ASSET {job.asset}")
        for f in job.files:
            rel = os.path.relpath(f.path, s.root) if s.root else f.path
            n = len(f.items) + len(f.merge)
            if dry_run:
                lines.append(f"FILE {rel}|{n} mesh{'es' if n != 1 else ''}, {f.tris:,} tris")
            else:
                r = res.get(f.path)
                tag = "FILE" if r and r[1] else "FAIL"
                lines.append(f"{tag} {rel}|{r[2] if r else 'not written'}")
        for w in job.warnings:
            lines.append(f"WARN {w}")
    for n, why in p.skipped:
        lines.append(f"SKIP {n}: {why}")
    ok = True
    if not dry_run:
        failed = [r for r in results if not r[1]]
        log = export.write_log(s.root, p, results, s)
        if log:
            lines.append(f"LOG {log}")
        ok = not failed
        if op:
            if failed:
                op.report({"ERROR"}, f"{len(failed)} of {len(results)} files failed: "
                                     f"{failed[0][2]}")
            else:
                op.report({"INFO"}, f"Exported {len(results)} files to {s.root}")
    elif op:
        op.report({"INFO"}, f"Dry run: {len(files)} files would be written")
    return lines, ok


def st_relative_unsaved(context):
    return context.scene.om3d_export.root.startswith("//") and not bpy.data.filepath


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class OM3D_AP_ExportState(bpy.types.PropertyGroup):
    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[("SELECTED", "Selected", "Export the selected objects"),
               ("VISIBLE", "Visible", "Export objects visible in the view layer"),
               ("ALL", "All", "Export every object in the scene")],
        default="VISIBLE")
    preset: bpy.props.EnumProperty(
        name="Preset",
        items=[("UNREAL", "Unreal", "Lows as SM_<Asset>.fbx, -Y forward, Z up, with UCX_ "
                                    "collision and LOD files"),
               ("SUBSTANCE", "Substance", "SM_<Asset>_low.fbx (+ _high.fbx), Y forward, Z up, "
                                          "triangulated"),
               ("MARMOSET", "Marmoset", "SM_<Asset>_low.fbx and SM_<Asset>_high.fbx, "
                                        "Y forward, Z up")],
        default="UNREAL")
    group_by: bpy.props.EnumProperty(
        name="Group By",
        items=[("ASSET", "Asset Name", "One file per <Asset> in SM_<Asset>_<Part>_low"),
               ("COLLECTION", "Collection", "One file per collection, named after it")],
        default="ASSET")
    pivot: bpy.props.EnumProperty(
        name="Origin",
        items=[("BOTTOM", "Bottom Center", "Move each asset so the bottom center of its lows "
                                           "sits at the world origin"),
               ("CENTER", "Bounds Center", "Move each asset so its center sits at the origin"),
               ("KEEP", "Keep Position", "Export where it is in the scene")],
        default="BOTTOM",
        description="Each asset is moved for the export only; the scene doesn't change")
    root: bpy.props.StringProperty(
        name="Export Folder", subtype="DIR_PATH", default="//export/",
        description="Files go to <folder>/<Asset>/. // means next to the .blend")
    merge_highs: bpy.props.BoolProperty(
        name="Merge Extra Highs", default=True,
        description="In high files, join a low's highs (_high, _high_bolts...) into one "
                    "<name>_high mesh so name matching finds them")
    show_preset: bpy.props.BoolProperty(name="Preset Settings", default=False)
    lines: bpy.props.StringProperty()
    time: bpy.props.StringProperty()
    show_all: bpy.props.BoolProperty(name="Show All", default=False)


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class OM3D_OT_export_batch(bpy.types.Operator):
    """Export one FBX per asset with the chosen preset"""
    bl_idname = "om3d.export_batch"
    bl_label = "Export"
    bl_options = {"REGISTER"}

    dry_run: bpy.props.BoolProperty(name="Dry Run", default=False)

    @classmethod
    def description(cls, context, props):
        if props.dry_run:
            return "List what would be exported, and where, without writing anything"
        return ("Export one FBX per asset with the chosen preset. "
                "Your scene isn't changed")

    def execute(self, context):
        st = context.scene.om3d_export
        objs = scene_checks.gather_objects(context, st.scope)
        s = settings_from(context, st.preset)
        lines, ok = do_export(context, objs, s, self.dry_run, self)
        st.lines = SEP.join(lines)
        st.time = ("Dry run " if self.dry_run else "Exported ") + time.strftime("%H:%M:%S")
        return {"FINISHED"} if ok else {"CANCELLED"}


class OM3D_OT_export_open_folder(bpy.types.Operator):
    """Open the export folder"""
    bl_idname = "om3d.export_open_folder"
    bl_label = "Open Folder"

    def execute(self, context):
        root = bpy.path.abspath(context.scene.om3d_export.root)
        if not root or not os.path.isdir(root):
            self.report({"WARNING"}, "Folder doesn't exist yet")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=root)
        return {"FINISHED"}


class OM3D_OT_export_clear(bpy.types.Operator):
    """Clear the export results list"""
    bl_idname = "om3d.export_clear"
    bl_label = "Clear Results"

    def execute(self, context):
        st = context.scene.om3d_export
        st.lines = ""
        st.time = ""
        return {"FINISHED"}


class OM3D_OT_export_reset_preset(bpy.types.Operator):
    """Put this preset's settings back to the defaults"""
    bl_idname = "om3d.export_reset_preset"
    bl_label = "Reset Preset"

    def execute(self, context):
        prefs = _prefs(context)
        key = context.scene.om3d_export.preset
        _load_defaults(getattr(prefs, "preset_" + key.lower()), key)
        return {"FINISHED"}


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------

ICON = {"ASSET": "PACKAGE", "FILE": "FILE_3D", "FAIL": "CANCEL", "WARN": "ERROR",
        "SKIP": "BLANK1", "LOG": "TEXT", "ERR": "CANCEL"}


def draw_lines(layout, title, lines, limit=None, state=None):
    box = layout.box()
    box.label(text=title, icon="INFO")
    shown = lines if limit is None else lines[:limit]
    col = box.column(align=True)
    skipped = [l for l in lines if l.startswith("SKIP ")]
    for line in shown:
        tag, _, rest = line.partition(" ")
        if tag == "SKIP":
            continue
        if tag in ("FILE", "FAIL"):
            path, _, info = rest.partition("|")
            r = col.row(align=True)
            r.alert = tag == "FAIL"
            r.label(text=path, icon=ICON[tag])
            col.label(text="      " + info)
        elif tag == "ASSET":
            col.separator()
            col.label(text=rest, icon=ICON[tag])
        else:
            r = col.row()
            r.alert = tag in ("ERR", "FAIL")
            r.label(text=rest if tag != "LOG" else "Log: " + os.path.basename(rest),
                    icon=ICON.get(tag, "DOT"))
    if skipped:
        col.separator()
        col.label(text=f"Skipped {len(skipped)}", icon="INFO")
        for line in skipped[:8]:
            col.label(text="   " + line[5:])
        if len(skipped) > 8:
            col.label(text=f"   ...and {len(skipped) - 8} more (in the log)")
    if state is not None and limit is not None and len(lines) > limit:
        box.prop(state, "show_all", toggle=True,
                 text="Show fewer" if state.show_all else f"Show all {len(lines)}")


def draw_export(panel, context):
    layout = panel.layout
    st = context.scene.om3d_export
    prefs = _prefs(context)
    layout.row().prop(st, "scope", expand=True)
    layout.row().prop(st, "preset", expand=True)
    col = layout.column(align=True)
    col.prop(st, "group_by")
    col.prop(st, "pivot")
    r = layout.row(align=True)
    r.prop(st, "root", text="")
    r.operator("om3d.export_open_folder", text="", icon="FOLDER_REDIRECT")

    box = layout.box()
    box.prop(st, "show_preset", emboss=False,
             icon="TRIA_DOWN" if st.show_preset else "TRIA_RIGHT",
             text=f"{st.preset.title()} Settings")
    if st.show_preset and prefs is not None:
        p = getattr(prefs, "preset_" + st.preset.lower())
        c = box.column(align=True)
        c.use_property_split = True
        c.use_property_decorate = False
        c.prop(p, "axis_forward")
        c.prop(p, "axis_up")
        c.prop(p, "global_scale")
        c.prop(p, "apply_unit_scale")
        c.prop(p, "smoothing")
        c.prop(p, "triangulate")
        c.prop(p, "include_high")
        if p.include_high:
            c.prop(st, "merge_highs")
        if st.preset == "UNREAL":
            c.prop(p, "collision")
            c.prop(p, "lods")
            c.prop(p, "strip_low")
        box.operator("om3d.export_reset_preset", icon="LOOP_BACK")

    r = layout.row(align=True)
    r.scale_y = 1.4
    op = r.operator("om3d.export_batch", text="Dry Run", icon="HIDE_OFF")
    op.dry_run = True
    op = r.operator("om3d.export_batch", text="Export", icon="EXPORT")
    op.dry_run = False

    if st.lines:
        lines = [l for l in st.lines.split(SEP) if l]
        r = layout.row()
        r.alignment = "RIGHT"
        r.operator("om3d.export_clear", text="Clear", icon="X", emboss=False)
        draw_lines(layout, st.time, lines, None if st.show_all else 30, st)


class OM3D_PT_batch_export(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OM3D"
    bl_parent_id = "OM3D_PT_asset_prep"
    bl_label = "Batch Export"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        draw_export(self, context)


state_classes = (*PRESET_CLASSES.values(), OM3D_AP_ExportState)
op_classes = (OM3D_OT_export_batch, OM3D_OT_export_open_folder, OM3D_OT_export_reset_preset,
              OM3D_OT_export_clear)
