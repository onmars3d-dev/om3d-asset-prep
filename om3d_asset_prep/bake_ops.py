# SPDX-License-Identifier: GPL-3.0-or-later
"""Bake Prep: state, operators and panel. Core logic lives in bake.py."""
import time

import bpy
from bpy.app.handlers import persistent

from . import bake

from . import export_ops
from . import scene_checks

SEP = "\n"


def _conv(context):
    return scene_checks.options_from_prefs(context).convention


def _split(s):
    return [n for n in s.split(SEP) if n] if s else []


def _objs(names):
    return [bpy.data.objects[n] for n in names if n in bpy.data.objects]


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class OM3D_AP_BakePair(bpy.types.PropertyGroup):
    base: bpy.props.StringProperty()
    asset: bpy.props.StringProperty()
    lows: bpy.props.StringProperty()
    highs: bpy.props.StringProperty()
    problems: bpy.props.StringProperty()
    expanded: bpy.props.BoolProperty(name="Show", default=False)


class OM3D_AP_BakeState(bpy.types.PropertyGroup):
    scope: bpy.props.EnumProperty(
        name="Scope",
        items=[("SELECTED", "Selected", "Pair the selected objects"),
               ("VISIBLE", "Visible", "Pair objects visible in the view layer"),
               ("ALL", "All", "Pair every object in the scene")],
        default="VISIBLE")
    pairs: bpy.props.CollectionProperty(type=OM3D_AP_BakePair)
    lone_lows: bpy.props.StringProperty()
    lone_highs: bpy.props.StringProperty()
    off_convention: bpy.props.StringProperty()
    has_run: bpy.props.BoolProperty(default=False)
    last_run: bpy.props.StringProperty()
    show_unmatched: bpy.props.BoolProperty(name="Unmatched", default=True)
    show_off: bpy.props.BoolProperty(name="Off convention", default=False)
    explode_axis: bpy.props.EnumProperty(
        name="Axis", items=[("X", "X", ""), ("Y", "Y", ""), ("Z", "Z", "")], default="X",
        description="Direction to spread the pairs out in")
    explode_gap: bpy.props.FloatProperty(
        name="Gap", default=0.0, min=0.0, unit="LENGTH",
        description="Space between pairs. 0 picks a quarter of the biggest pair")
    groups_move: bpy.props.BoolProperty(
        name="Move Into Groups", default=False,
        description="Move objects into the bake group collections instead of linking them "
                    "(linking keeps your own collections as they are)")
    triangulate: bpy.props.BoolProperty(
        name="Triangulate Low", default=True,
        description="Triangulate the low in the bake file, so the baker and the game engine "
                    "see the same triangles")
    merge_highs: bpy.props.BoolProperty(
        name="Merge Extra Highs", default=True,
        description="Join a low's highs (_high, _high_bolts...) into one <name>_high mesh in "
                    "the bake file, so name matching always finds them")
    export_lines: bpy.props.StringProperty()
    export_time: bpy.props.StringProperty()
    # Name Pairs
    show_naming: bpy.props.BoolProperty(name="Name Pairs", default=False)
    name_low: bpy.props.PointerProperty(
        type=bpy.types.Collection, name="Lows",
        description="Collection holding the low-poly objects")
    name_high: bpy.props.PointerProperty(
        type=bpy.types.Collection, name="Highs",
        description="Collection holding the high-poly objects")
    name_asset: bpy.props.StringProperty(
        name="Asset", default="",
        description="Asset name for SM_<Asset>_<Part>. Blank uses the low collection's name "
                    "without 'low'")
    name_keep: bpy.props.BoolProperty(
        name="Keep Meaningful Names", default=True,
        description="Use the old name as the part when it means something "
                    "(Scale_Viewfinder -> ScaleViewfinder). Generic names (Cube.001) become "
                    "Part01, Part02...")
    name_lines: bpy.props.StringProperty()
    name_time: bpy.props.StringProperty()


def store_pairs(state, res):
    open_ = {p.base for p in state.pairs if p.expanded}
    state.pairs.clear()
    for p in res.pairs:
        item = state.pairs.add()
        item.base = p.base
        item.asset = p.asset
        item.lows = SEP.join(o.name for o in p.lows)
        item.highs = SEP.join(o.name for o in p.highs)
        item.problems = SEP.join(p.problems)
        item.expanded = p.base in open_
    state.lone_lows = SEP.join(o.name for p in res.lone_lows for o in p.lows)
    state.lone_highs = SEP.join(o.name for p in res.lone_highs for o in p.highs)
    state.off_convention = SEP.join(o.name for o in res.off_convention)
    state.has_run = True
    state.last_run = time.strftime("%H:%M:%S")


def run_pairing(context):
    state = context.scene.om3d_bake_prep
    objs = scene_checks.gather_objects(context, state.scope)
    res = bake.find_pairs(objs, _conv(context))
    store_pairs(state, res)
    return res


def _paired_objects(state):
    out = []
    for p in state.pairs:
        out += _objs(_split(p.lows)) + _objs(_split(p.highs))
    return out


def clear_results(state, settings=False):
    """Empty the Bake Prep lists. settings=True also clears the Name Pairs pickers."""
    state.pairs.clear()
    state.lone_lows = state.lone_highs = state.off_convention = ""
    state.has_run = False
    state.last_run = ""
    state.name_lines = state.name_time = ""
    state.export_lines = state.export_time = ""
    if settings:
        state.name_low = None
        state.name_high = None
        state.name_asset = ""
        state.show_naming = False


def has_results(state):
    return bool(state.has_run or state.name_lines or state.export_lines)


@persistent
def clear_on_load(_dummy=None):
    """Opening a file starts Scene Prep, Bake Prep and Batch Export with empty result lists
    (settings like scope, fix options, collections, presets and the export folder are kept)."""
    from . import operators, overlay
    overlay.clear()  # the highlight is memory-only; never carry it into another file
    for scene in bpy.data.scenes:
        sp = getattr(scene, "om3d_scene_prep", None)
        if sp is not None and (sp.has_run or sp.fix_has_run):
            operators.clear_state(sp, full=True)
        st = getattr(scene, "om3d_bake_prep", None)
        if st is not None and has_results(st):
            clear_results(st)
        ex = getattr(scene, "om3d_export", None)
        if ex is not None and ex.lines:
            ex.lines = ""
            ex.time = ""


# --------------------------------------------------------------------------
# Name Pairs helpers
# --------------------------------------------------------------------------

def _asset_name(state):
    from .fixes import _token
    if state.name_asset.strip():
        return _token(state.name_asset) or "Asset"
    if state.name_low is not None:
        return _token(state.name_low.name) or "Asset"
    return "Asset"


def _name_plan(context):
    state = context.scene.om3d_bake_prep
    lows = list(state.name_low.all_objects)
    highs = list(state.name_high.all_objects)
    return bake.plan_names(lows, highs, _conv(context), _asset_name(state), state.name_keep)


def _plan_lines(plan):
    lines = []
    for e in plan.entries:
        lines.append(f"L|{e.low.name}|{e.new_low}|{len(e.highs)}")
        for m, n in e.highs:
            note = m.runner_up.name if (m.close_call and m.runner_up) else ""
            lines.append(f"H|{m.high.name}|{n}|{note}")
    for m in plan.unmatched:
        lines.append(f"U|{m.high.name}|{m.reason}")
    return lines


def _check_name_cols(state, op):
    if state.name_low is None or state.name_high is None:
        op.report({"WARNING"}, "Pick the low and high collections first")
        return False
    if state.name_low == state.name_high:
        op.report({"WARNING"}, "Lows and highs need different collections")
        return False
    return True


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

class OM3D_OT_bake_clear(bpy.types.Operator):
    """Clear Bake Prep: pairs, unmatched lists, the Name Pairs preview and export results,
    plus the low/high collection pickers. Nothing in the scene changes"""
    bl_idname = "om3d.bake_clear"
    bl_label = "Reset Bake Prep"
    bl_options = {"REGISTER"}

    def execute(self, context):
        clear_results(context.scene.om3d_bake_prep, settings=True)
        return {"FINISHED"}


class OM3D_OT_bake_name_preview(bpy.types.Operator):
    """Match each high to the low it sits on and list the new names. Nothing is renamed"""
    bl_idname = "om3d.bake_name_preview"
    bl_label = "Preview Names"
    bl_options = {"REGISTER"}

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        if not _check_name_cols(state, self):
            return {"CANCELLED"}
        t0 = time.perf_counter()
        plan = _name_plan(context)
        state.name_lines = SEP.join(_plan_lines(plan))
        state.name_time = "Preview " + time.strftime("%H:%M:%S")
        close = sum(1 for e in plan.entries for m, _n in e.highs if m.close_call)
        self.report({"INFO"}, f"{len(plan.entries)} lows, "
                              f"{sum(len(e.highs) for e in plan.entries)} highs matched, "
                              f"{len(plan.unmatched)} unmatched, {close} close calls "
                              f"({time.perf_counter() - t0:.1f} s)")
        return {"FINISHED"}


class OM3D_OT_bake_name_apply(bpy.types.Operator):
    """Rename lows and highs to SM_<Asset>_<Part>_low / _high, matching each high to the
    low it sits on. One undo step"""
    bl_idname = "om3d.bake_name_apply"
    bl_label = "Rename Pairs"
    bl_options = {"REGISTER", "UNDO"}

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Rename Pairs", confirm_text="Rename",
            message="Rename the lows and highs to the convention? Ctrl+Z undoes it")

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        if not _check_name_cols(state, self):
            return {"CANCELLED"}
        if context.mode != "OBJECT" and context.active_object is not None:
            bpy.ops.object.mode_set(mode="OBJECT")
        plan = _name_plan(context)
        lines = _plan_lines(plan)
        n = bake.apply_names(plan)
        # show the result with the new names
        state.name_lines = SEP.join(lines)
        state.name_time = "Renamed " + time.strftime("%H:%M:%S")
        # pair what was just named, even if the highs' collection is hidden
        objs = list(state.name_low.all_objects) + list(state.name_high.all_objects)
        store_pairs(state, bake.find_pairs(objs, _conv(context)))
        self.report({"INFO"}, f"Renamed {n} objects. {len(plan.unmatched)} highs unmatched")
        return {"FINISHED"}


class OM3D_OT_bake_pair(bpy.types.Operator):
    """Match _low and _high objects by name and check each pair"""
    bl_idname = "om3d.bake_pair"
    bl_label = "Find Pairs"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if scene_checks.topo_draw_running():
            self.report({"WARNING"}, "Finish Topo Draw first")
            return {"CANCELLED"}
        res = run_pairing(context)
        bad = sum(1 for p in res.pairs if p.problems)
        self.report({"INFO"}, f"{len(res.pairs)} pairs ({bad} with problems), "
                              f"{sum(len(p.lows) for p in res.lone_lows)} lows and "
                              f"{sum(len(p.highs) for p in res.lone_highs)} highs unmatched")
        return {"FINISHED"}


class OM3D_OT_bake_select(bpy.types.Operator):
    """Select these objects"""
    bl_idname = "om3d.bake_select"
    bl_label = "Select"
    bl_options = {"REGISTER", "UNDO"}

    what: bpy.props.EnumProperty(items=[
        ("PAIR", "Pair", ""), ("LOWS", "Lows", ""), ("HIGHS", "Highs", ""),
        ("LONE_LOWS", "Lows without a high", ""), ("LONE_HIGHS", "Highs without a low", ""),
        ("OFF", "Off convention", ""), ("ALL_PAIRS", "All pairs", ""), ("NAME", "One", "")])
    base: bpy.props.StringProperty()
    name: bpy.props.StringProperty()
    extend: bpy.props.BoolProperty(default=False, options={"SKIP_SAVE"})

    @classmethod
    def description(cls, context, props):
        return {"PAIR": "Select this low and its highs", "LOWS": "Select the lows",
                "HIGHS": "Select the highs", "LONE_LOWS": "Select every low that has no high",
                "LONE_HIGHS": "Select every high that has no low",
                "OFF": "Select meshes whose names don't fit the convention",
                "ALL_PAIRS": "Select every paired object",
                "NAME": "Select this object"}.get(props.what, "Select")

    def invoke(self, context, event):
        self.extend = event.shift
        return self.execute(context)

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        names = []
        if self.what == "NAME":
            names = [self.name]
        elif self.what == "LONE_LOWS":
            names = _split(state.lone_lows)
        elif self.what == "LONE_HIGHS":
            names = _split(state.lone_highs)
        elif self.what == "OFF":
            names = _split(state.off_convention)
        elif self.what == "ALL_PAIRS":
            names = [o.name for o in _paired_objects(state)]
        else:
            for p in state.pairs:
                if p.base == self.base:
                    if self.what in ("PAIR", "LOWS"):
                        names += _split(p.lows)
                    if self.what in ("PAIR", "HIGHS"):
                        names += _split(p.highs)
        objs = [o for o in _objs(names) if o.name in context.view_layer.objects]
        if not objs:
            self.report({"WARNING"}, "Nothing to select (renamed, deleted or hidden?)")
            return {"CANCELLED"}
        if context.mode != "OBJECT" and context.active_object is not None:
            bpy.ops.object.mode_set(mode="OBJECT")
        if not self.extend:
            for o in context.view_layer.objects:
                o.select_set(False)
        for o in objs:
            if o.hide_get():
                o.hide_set(False)
            o.select_set(True)
        context.view_layer.objects.active = objs[0]
        return {"FINISHED"}


class OM3D_OT_bake_explode(bpy.types.Operator):
    """Spread the pairs out along an axis so their bakes can't bleed into each other.
    Each low moves together with its highs. Reset puts them back"""
    bl_idname = "om3d.bake_explode"
    bl_label = "Explode"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        res = run_pairing(context)
        if not res.pairs:
            self.report({"WARNING"}, "No pairs to explode")
            return {"CANCELLED"}
        objs = [o for p in res.pairs for o in p.lows + p.highs]
        if bake.exploded(objs):
            bake.reset_explode(objs)
            context.view_layer.update()
        n = bake.explode(res.pairs, state.explode_axis, state.explode_gap or None)
        context.view_layer.update()
        run_pairing(context)
        self.report({"INFO"}, f"Exploded {n} pairs along {state.explode_axis}")
        return {"FINISHED"}


class OM3D_OT_bake_reset(bpy.types.Operator):
    """Move exploded pairs back to where they were"""
    bl_idname = "om3d.bake_reset"
    bl_label = "Reset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        objs = [o for o in context.scene.objects if bake.EXPLODE_PROP in o]
        n = bake.reset_explode(objs)
        context.view_layer.update()
        if context.scene.om3d_bake_prep.has_run:
            run_pairing(context)
        self.report({"INFO"}, f"Moved {n} objects back")
        return {"FINISHED"}


class OM3D_OT_bake_groups(bpy.types.Operator):
    """Put each pair in its own collection, named for its Marmoset bake group,
    under a 'Bake Groups' collection"""
    bl_idname = "om3d.bake_groups"
    bl_label = "Make Bake Groups"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        res = run_pairing(context)
        if not res.pairs:
            self.report({"WARNING"}, "No pairs found")
            return {"CANCELLED"}
        _root, n = bake.write_bake_groups(context.scene, res.pairs, move=state.groups_move)
        self.report({"INFO"}, f"{n} bake group collections in '{bake.ROOT_NAME}'")
        return {"FINISHED"}


class OM3D_OT_bake_mark_stacked(bpy.types.Operator):
    """Mark this low's overlapping UVs as on purpose (or not), so the pair check
    stops flagging them"""
    bl_idname = "om3d.bake_mark_stacked"
    bl_label = "Stacked on Purpose"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty()

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        if obj.get(bake.STACKED_PROP):
            del obj[bake.STACKED_PROP]
        else:
            obj[bake.STACKED_PROP] = True
        run_pairing(context)
        return {"FINISHED"}


class OM3D_OT_bake_export(bpy.types.Operator):
    """Export each asset's paired lows and highs for baking"""
    bl_idname = "om3d.bake_export"
    bl_label = "Export for Baking"
    bl_options = {"REGISTER"}

    target: bpy.props.EnumProperty(items=[
        ("MARMOSET", "Marmoset Toolbag", "One low and one high FBX per asset, names kept "
                                         "for Toolbag's suffix pairing"),
        ("SUBSTANCE", "Substance Painter", "Low FBX per asset plus a high FBX with matching "
                                           "names, for Match by Mesh Name")])
    dry_run: bpy.props.BoolProperty(default=False)

    @classmethod
    def description(cls, context, props):
        what = "List what would be written" if props.dry_run else "Write"
        return (f"{what}: " + {"MARMOSET": "one low and one high FBX per asset for Marmoset "
                               "Toolbag", "SUBSTANCE": "a low FBX and a high FBX per asset "
                               "for Substance Painter (Match by Mesh Name)"}[props.target])

    def execute(self, context):
        state = context.scene.om3d_bake_prep
        res = run_pairing(context)
        if not res.pairs:
            self.report({"WARNING"}, "No pairs to export")
            return {"CANCELLED"}
        s = export_ops.settings_from(context, self.target)
        s.include_high = True
        s.triangulate = state.triangulate
        s.merge_highs = state.merge_highs
        s.group_by = "ASSET"
        s.collision = False
        s.lods = False
        s.strip_low = False
        objs = [o for p in res.pairs for o in p.lows + p.highs]
        lines, ok = export_ops.do_export(context, objs, s, self.dry_run, self)
        state.export_lines = SEP.join(lines)
        state.export_time = (("Dry run " if self.dry_run else "Exported ")
                             + time.strftime("%H:%M:%S"))
        return {"FINISHED"} if ok else {"CANCELLED"}


# --------------------------------------------------------------------------
# Panel
# --------------------------------------------------------------------------

def _name_row(col, name, icon):
    r = col.row(align=True)
    r.label(text=name, icon=icon)
    op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
    op.what = "NAME"
    op.name = name


def draw_naming(layout, state):
    box = layout.box()
    box.prop(state, "show_naming", emboss=False,
             icon="TRIA_DOWN" if state.show_naming else "TRIA_RIGHT",
             text="Name Pairs (match by position)")
    if not state.show_naming:
        return
    col = box.column(align=True)
    col.prop(state, "name_low", icon="OUTLINER_COLLECTION")
    col.prop(state, "name_high", icon="OUTLINER_COLLECTION")
    col.prop(state, "name_asset", placeholder=_asset_name(state))
    box.prop(state, "name_keep")
    r = box.row(align=True)
    r.operator("om3d.bake_name_preview", icon="HIDE_OFF")
    r.operator("om3d.bake_name_apply", icon="SORTALPHA")
    lines = _split(state.name_lines)
    if not lines:
        return
    renamed = state.name_time.startswith("Renamed")
    low_n = sum(1 for l in lines if l.startswith("L|"))
    high_n = sum(1 for l in lines if l.startswith("H|"))
    un = [l for l in lines if l.startswith("U|")]
    close = [l for l in lines if l.startswith("H|") and l.split("|")[3]]
    box.label(text=state.name_time, icon="INFO")
    box.label(text=f"{low_n} lows, {high_n} highs, {len(un)} unmatched")
    if close:
        box.label(text=f"{len(close)} match{'es' if len(close) > 1 else ''} worth a look "
                       f"(still renamed)", icon="QUESTION")
    col = box.column(align=True)
    for line in lines:
        bits = line.split("|")
        if bits[0] == "L":
            col.separator()
            r = col.row(align=True)
            r.label(text=bits[2], icon="MESH_PLANE")
            if not renamed:
                col.label(text=f"      was {bits[1]}")
            if bits[3] == "0":
                col.label(text="      no high matched", icon="UNLINKED")
        elif bits[0] == "H":
            r = col.row(align=True)
            r.label(text=f"   {bits[2]}", icon="QUESTION" if bits[3] else "MESH_ICOSPHERE")
            sel = bits[2] if renamed else bits[1]
            op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
            op.what = "NAME"
            op.name = sel
            if not renamed:
                col.label(text=f"         was {bits[1]}")
            if bits[3]:
                col.label(text=f"         matched, but also close to {bits[3]}: worth a look")
    if un:
        col.separator()
        col.label(text="Highs with no low nearby (left as they are)", icon="UNLINKED")
        for line in un:
            _o, name, why = line.split("|")
            r = col.row(align=True)
            r.label(text=f"   {name}: {why}")
            op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF", emboss=False)
            op.what = "NAME"
            op.name = name


def draw_bake(panel, context):
    layout = panel.layout
    state = context.scene.om3d_bake_prep
    estate = context.scene.om3d_export

    r = layout.row()
    r.alignment = "RIGHT"
    r.operator("om3d.bake_clear", text="Reset", icon="LOOP_BACK", emboss=False)

    draw_naming(layout, state)

    layout.row().prop(state, "scope", expand=True)
    row = layout.row()
    row.scale_y = 1.4
    row.operator("om3d.bake_pair", icon="LINKED")
    if not state.has_run:
        layout.label(text="Pairs _low with _high by name", icon="INFO")
        return

    bad = sum(1 for p in state.pairs if p.problems)
    lone_l, lone_h = _split(state.lone_lows), _split(state.lone_highs)
    off = _split(state.off_convention)
    head = layout.row()
    head.label(text=f"{len(state.pairs)} pairs, {bad} with problems  ({state.last_run})")
    if state.pairs:
        op = head.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF")
        op.what = "ALL_PAIRS"

    # pairs
    for p in state.pairs:
        probs = _split(p.problems)
        highs = _split(p.highs)
        box = layout.box()
        r = box.row(align=True)
        r.prop(p, "expanded", text="", emboss=False,
               icon="TRIA_DOWN" if p.expanded else "TRIA_RIGHT")
        name = r.row(align=True)
        name.alignment = "LEFT"
        name.alert = bool(probs)
        name.label(text=p.base, icon="ERROR" if probs else "CHECKMARK")
        c = r.row(align=True)
        c.alignment = "RIGHT"
        c.label(text=f"{len(highs)} high" + ("s" if len(highs) != 1 else ""))
        op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF")
        op.what = "PAIR"
        op.base = p.base
        if not p.expanded:
            continue
        col = box.column(align=True)
        for n in _split(p.lows):
            rr = col.row(align=True)
            rr.label(text=n, icon="MESH_PLANE")
            obj = bpy.data.objects.get(n)
            stacked = bool(obj and obj.get(bake.STACKED_PROP))
            op = rr.operator("om3d.bake_mark_stacked", text="", emboss=False,
                             icon="UV_SYNC_SELECT" if stacked else "UV")
            op.name = n
            op = rr.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF",
                             emboss=False)
            op.what = "NAME"
            op.name = n
        for n in highs:
            _name_row(col, n, "MESH_ICOSPHERE")
        if probs:
            pc = box.column(align=True)
            for t in probs:
                pc.label(text=t, icon="ERROR")

    # unmatched
    if lone_l or lone_h:
        box = layout.box()
        box.prop(state, "show_unmatched", emboss=False,
                 icon="TRIA_DOWN" if state.show_unmatched else "TRIA_RIGHT",
                 text=f"Unmatched: {len(lone_l)} low, {len(lone_h)} high")
        if state.show_unmatched:
            for label, names, what in (("Lows with no high", lone_l, "LONE_LOWS"),
                                       ("Highs with no low", lone_h, "LONE_HIGHS")):
                if not names:
                    continue
                r = box.row(align=True)
                r.label(text=label, icon="UNLINKED")
                op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF")
                op.what = what
                col = box.column(align=True)
                for n in names:
                    _name_row(col, n, "OBJECT_DATA")
    if off:
        box = layout.box()
        r = box.row(align=True)
        r.prop(state, "show_off", emboss=False,
               icon="TRIA_DOWN" if state.show_off else "TRIA_RIGHT",
               text=f"Not named for baking: {len(off)}")
        op = r.operator("om3d.bake_select", text="", icon="RESTRICT_SELECT_OFF")
        op.what = "OFF"
        if state.show_off:
            col = box.column(align=True)
            for n in off:
                _name_row(col, n, "OBJECT_DATA")

    # explode
    box = layout.box()
    box.label(text="Explode", icon="FULLSCREEN_ENTER")
    r = box.row(align=True)
    r.prop(state, "explode_axis", expand=True)
    box.prop(state, "explode_gap")
    r = box.row(align=True)
    r.operator("om3d.bake_explode", icon="FULLSCREEN_ENTER")
    r.operator("om3d.bake_reset", icon="LOOP_BACK")

    # bake groups
    box = layout.box()
    box.label(text="Bake Groups", icon="OUTLINER_COLLECTION")
    box.prop(state, "groups_move")
    box.operator("om3d.bake_groups", icon="COLLECTION_NEW")

    # export
    box = layout.box()
    box.label(text="Export for Baking", icon="EXPORT")
    box.prop(estate, "root", text="")
    box.prop(state, "triangulate")
    box.prop(state, "merge_highs")
    r = box.row(align=True)
    for target, text in (("MARMOSET", "Marmoset"), ("SUBSTANCE", "Substance")):
        op = r.operator("om3d.bake_export", text=text, icon="EXPORT")
        op.target = target
        op.dry_run = False
    r = box.row(align=True)
    for target, text in (("MARMOSET", "Dry Run Marmoset"), ("SUBSTANCE", "Dry Run Substance")):
        op = r.operator("om3d.bake_export", text=text, icon="HIDE_OFF")
        op.target = target
        op.dry_run = True
    if state.export_lines:
        export_ops.draw_lines(box, state.export_time, _split(state.export_lines))


class OM3D_PT_bake_prep(bpy.types.Panel):
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "OM3D"
    bl_parent_id = "OM3D_PT_asset_prep"
    bl_label = "Bake Prep"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        draw_bake(self, context)


state_classes = (OM3D_AP_BakePair, OM3D_AP_BakeState)
op_classes = (OM3D_OT_bake_pair, OM3D_OT_bake_select, OM3D_OT_bake_explode,
              OM3D_OT_bake_reset, OM3D_OT_bake_groups, OM3D_OT_bake_mark_stacked,
              OM3D_OT_bake_export, OM3D_OT_bake_name_preview, OM3D_OT_bake_name_apply,
              OM3D_OT_bake_clear)
