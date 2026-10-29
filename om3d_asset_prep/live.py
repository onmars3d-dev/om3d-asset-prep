# SPDX-License-Identifier: GPL-3.0-or-later
"""Live refresh (xView style): re-check objects you edit, after a short pause.

Only objects from the last check are watched. Paused while Topo Draw runs,
since reading an edit mesh from Python would disturb its held BMesh.
"""
import time

import bpy
from bpy.app.handlers import persistent

from . import overlay
from . import scene_checks

DELAY = 0.35

_dirty = set()
_last_change = [0.0]
_scheduled = [False]


def _watched():
    report = overlay.CACHE["report"]
    return set(report["checked"]) if report else set()


@persistent
def _on_depsgraph(scene, depsgraph):
    state = getattr(scene, "om3d_scene_prep", None)
    if (state is None or not state.has_run or not state.live_update
            or overlay.CACHE["report"] is None or overlay.CACHE["scene"] != scene.name):
        return
    watched = _watched()
    hit = False
    for u in depsgraph.updates:
        idb = u.id.original if u.id else None
        if isinstance(idb, bpy.types.Object):
            if idb.name in watched and (u.is_updated_geometry or u.is_updated_transform):
                _dirty.add(idb.name)
                hit = True
        elif isinstance(idb, bpy.types.Mesh) and u.is_updated_geometry:
            for name in watched:
                obj = scene.objects.get(name)
                if obj is not None and obj.data == idb:
                    _dirty.add(name)
                    hit = True
    if hit:
        _last_change[0] = time.monotonic()
        if not _scheduled[0]:
            _scheduled[0] = True
            bpy.app.timers.register(_refresh, first_interval=DELAY)


def _refresh():
    wait = DELAY - (time.monotonic() - _last_change[0])
    if wait > 0.02:
        return wait  # still editing, wait for a pause
    _scheduled[0] = False
    if scene_checks.topo_draw_running():
        _dirty.clear()
        return None
    context = bpy.context
    scene = context.scene
    state = getattr(scene, "om3d_scene_prep", None)
    report = overlay.CACHE["report"]
    if state is None or report is None or overlay.CACHE["scene"] != scene.name:
        _dirty.clear()
        return None
    objs = [scene.objects[n] for n in _dirty if n in scene.objects]
    _dirty.clear()
    if not objs:
        return None
    from .operators import store_report  # avoid import cycle at load
    opt = scene_checks.options_from_prefs(context)
    opt.skip_heavy_over = scene_checks.LIVE_HEAVY_LIMIT
    try:
        scene_checks.refresh_objects(report, objs, opt, overlay.CACHE["geometry"])
    except Exception as exc:
        print("OM3D Asset Prep: live refresh failed:", exc)
        return None
    store_report(state, report, keep_time=True)
    overlay.invalidate()
    for win in context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    return None


def register():
    if _on_depsgraph not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph)


def unregister():
    if _on_depsgraph in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph)
    if bpy.app.timers.is_registered(_refresh):
        bpy.app.timers.unregister(_refresh)
    _scheduled[0] = False
    _dirty.clear()
