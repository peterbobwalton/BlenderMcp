"""'Send to Claude': the user marks what they are looking at (selection + viewport + a note) from the
sidebar panel; Claude fetches it with get_user_context instead of the user describing it in words."""

import time

import bpy

from . import imaging
from .handlers import _p, _summary, _window_ctx, command

_state = {"context": None}


def capture(context, note):
    """Called by the panel operator (has a real UI context)."""
    vl = context.view_layer
    selected = [o for o in vl.objects if o.select_get()]
    ctx = None
    area = context.area if context.area and context.area.type == "VIEW_3D" else None
    if area is not None:
        region = next((r for r in area.regions if r.type == "WINDOW"), None)
        ctx = {"window": context.window, "screen": context.screen, "area": area, "region": region}
    else:
        try:
            ctx = _window_ctx(need_view3d=True)
        except Exception:
            ctx = None
    image = imaging.capture_viewport(ctx, 1280) if ctx else None
    _state["context"] = {
        "sent_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note,
        "mode": context.mode,
        "active": vl.objects.active.name if vl.objects.active else None,
        "selected": [_summary(o) for o in selected[:25]],
        "selected_count": len(selected),
        "file": bpy.data.filepath or None,
        "image": image,
    }
    return len(selected)


@command("get_user_context")
def get_user_context(p):
    ctx = _state["context"]
    if ctx is None:
        return {"available": False,
                "hint": "Nothing sent yet. In Blender: 3D Viewport > Sidebar (N) > MCP > 'Send to Claude'."}
    out = dict(ctx, available=True)
    if not _p(p, "include_image", True):
        out.pop("image", None)
    if _p(p, "clear", False):
        _state["context"] = None
    return out
