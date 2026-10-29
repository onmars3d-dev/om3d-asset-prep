# SPDX-License-Identifier: GPL-3.0-or-later
"""Viewport highlight (xView style): problem elements drawn in light green.

The last report and its local-space geometry live in CACHE (memory only).
Batches are rebuilt lazily when invalidate() is called: after a check, a live
refresh, or an eye toggle in the panel.
"""
import bpy
import gpu
from gpu_extras.batch import batch_for_shader
from mathutils import Vector

CACHE = {"scene": None, "report": None, "geometry": {}, "batches": {}}

# Issues that also fill their faces in Vertices style while their row is open
# (a dot alone doesn't show which face it is; in the mixed view they stay dots).
FACE_FILL = {"uv_overlap"}

_handle = None
_shader = {}

VERT_SRC = """
void main()
{
    vec3 p = pos;
    if (persp > 0.5) {
        p += (eye - p) * lift;      /* slide toward the eye along the view ray */
    }
    gl_Position = viewProj * vec4(p, 1.0);
    if (persp < 0.5) {
        gl_Position.z -= lift * 0.05 * gl_Position.w;
    }
    gl_PointSize = psize;
}
"""

FRAG_SRC = """
void main()
{
    fragColor = color;
}
"""


def _get_shader():
    if "s" not in _shader:
        try:
            info = gpu.types.GPUShaderCreateInfo()
            info.push_constant("MAT4", "viewProj")
            info.push_constant("VEC4", "color")
            info.push_constant("VEC3", "eye")
            info.push_constant("FLOAT", "lift")
            info.push_constant("FLOAT", "persp")
            info.push_constant("FLOAT", "psize")
            info.vertex_in(0, "VEC3", "pos")
            info.fragment_out(0, "VEC4", "fragColor")
            info.vertex_source(VERT_SRC)
            info.fragment_source(FRAG_SRC)
            _shader["s"] = (gpu.shader.create_from_info(info), True)
        except Exception as exc:  # fall back to the builtin flat shader
            print("OM3D Asset Prep: custom highlight shader failed, using fallback:", exc)
            _shader["s"] = (gpu.shader.from_builtin("UNIFORM_COLOR"), False)
    return _shader["s"]


def invalidate():
    CACHE["batches"] = {}
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def set_report(scene, report, geometry):
    CACHE["scene"] = scene.name
    CACHE["report"] = report
    CACHE["geometry"] = geometry
    invalidate()


def clear():
    CACHE.update(scene=None, report=None, geometry={}, batches={})
    invalidate()


def has_geometry():
    return bool(CACHE["geometry"])


def _style(context):
    addon = context.preferences.addons.get(__package__)
    return addon.preferences.highlight_style if addon else "VERTS"


def shown_keys(state):
    """Issues to highlight: the opened (expanded) ones if any are open, else every eye that's on."""
    on = [i for i in state.issues if i.level == "ELEMENT" and i.show_in_viewport]
    opened = {i.key for i in on if i.expanded}
    return opened or {i.key for i in on}


def _visible_names(context):
    """Objects with highlight geometry that this viewport shows (respects local view and hiding)."""
    space = context.space_data
    scene = context.scene
    out = set()
    for name in CACHE["geometry"]:
        obj = scene.objects.get(name)
        if obj is None:
            continue
        try:
            vis = obj.visible_get(viewport=space)
        except TypeError:
            vis = obj.visible_get()
        if vis:
            out.add(name)
    return frozenset(out)


def _build(context, state, visible):
    shown = shown_keys(state)
    opened = {i.key for i in state.issues if i.expanded}
    verts_only = _style(context) == "VERTS"
    tris, lines, points = [], [], []
    scene = context.scene
    for name, per_key in CACHE["geometry"].items():
        if name not in visible:
            continue
        obj = scene.objects.get(name)
        mw = obj.matrix_world
        for key, g in per_key.items():
            if key not in shown:
                continue
            if verts_only:
                if key in FACE_FILL and key in opened:  # fill only while its row is open
                    tris += [mw @ Vector(c) for c in g["tris"]]
                points += [mw @ Vector(c) for c in g.get("verts", g["points"])]
                continue
            tris += [mw @ Vector(c) for c in g["tris"]]
            lines += [mw @ Vector(c) for c in g["lines"]]
            points += [mw @ Vector(c) for c in g["points"]]
    shader, _custom = _get_shader()
    b = {}
    if tris:
        b["tris"] = batch_for_shader(shader, "TRIS", {"pos": tris})
    if lines:
        b["lines"] = batch_for_shader(shader, "LINES", {"pos": lines})
    if points:
        b["points"] = batch_for_shader(shader, "POINTS", {"pos": points})
    return b


def _draw():
    context = bpy.context
    scene = context.scene
    state = getattr(scene, "om3d_scene_prep", None)
    if (state is None or not state.show_overlay or not CACHE["geometry"]
            or CACHE["scene"] != scene.name):
        return
    # one cache entry per viewport, rebuilt when what that viewport shows changes
    visible = _visible_names(context)
    key = context.space_data.as_pointer()
    entry = CACHE["batches"].get(key)
    if entry is None or entry[0] != visible:
        entry = (visible, _build(context, state, visible))
        CACHE["batches"][key] = entry
    batches = entry[1]
    if not batches:
        return
    addon = context.preferences.addons.get(__package__)
    prefs = addon.preferences if addon else None
    rgb = tuple(prefs.highlight_color) if prefs else (0.55, 1.0, 0.55)
    xray = prefs.highlight_xray if prefs else False
    rv3d = context.region_data
    shader, custom = _get_shader()

    gpu.state.blend_set("ALPHA")
    gpu.state.depth_mask_set(False)
    gpu.state.depth_test_set("NONE" if xray else "LESS_EQUAL")
    gpu.state.face_culling_set("NONE")
    shader.bind()
    if custom:
        shader.uniform_float("viewProj", rv3d.perspective_matrix)
        shader.uniform_float("eye", rv3d.view_matrix.inverted().translation)
        shader.uniform_float("persp", 1.0 if rv3d.is_perspective else 0.0)
        gpu.state.program_point_size_set(True)

    def go(kind, alpha, lift, psize=1.0):
        if kind not in batches:
            return
        shader.uniform_float("color", (*rgb, alpha))
        if custom:
            shader.uniform_float("lift", lift)
            shader.uniform_float("psize", psize)
        batches[kind].draw(shader)

    go("tris", 0.35, 0.0015)
    gpu.state.line_width_set(2.0)
    go("lines", 1.0, 0.0025)
    psize = 6.0 if _style(context) == "VERTS" else 7.0
    if not custom:
        gpu.state.point_size_set(psize)
    go("points", 1.0, 0.003, psize)

    gpu.state.line_width_set(1.0)
    if custom:
        gpu.state.program_point_size_set(False)
    else:
        gpu.state.point_size_set(1.0)
    gpu.state.depth_test_set("NONE")
    gpu.state.depth_mask_set(True)
    gpu.state.blend_set("NONE")


def register():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), "WINDOW", "POST_VIEW")


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    _shader.clear()
    CACHE.update(scene=None, report=None, geometry={}, batches={})
