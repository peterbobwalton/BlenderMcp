"""Image capture helpers: offscreen viewport renders, framing cameras, PNG encoding."""

import base64
import math
import os
import struct
import zlib

import bpy
import gpu
import numpy as np
from mathutils import Matrix, Vector

# Eye directions (from target towards the eye), Blender axes: -Y is "front".
VIEW_DIRS = {
    "front": (0, -1, 0), "back": (0, 1, 0),
    "right": (1, 0, 0), "left": (-1, 0, 0),
    "top": (0, 0, 1), "bottom": (0, 0, -1),
    "iso": (1, -1, 0.8), "iso_back": (-1, 1, 0.8),
    "iso_left": (-1, -1, 0.8), "iso_right_back": (1, 1, 0.8),
}

_FOV = math.radians(40.0)


def encode_png(rgba, width, height):
    """rgba: uint8 array (h, w, 4), top row first."""
    raw = np.empty((height, 1 + width * 4), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = rgba.reshape(height, width * 4)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b"")


def file_result(path, max_size=1024):
    """Load a PNG from disk, downscale if needed, return base64 payload."""
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if max(w, h) > max_size:
            s = max_size / max(w, h)
            w, h = max(1, int(w * s)), max(1, int(h * s))
            img.scale(w, h)
            px = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(px)
            rgba = (np.clip(px, 0, 1) * 255 + 0.5).astype(np.uint8).reshape(h, w, 4)[::-1]
            data = encode_png(rgba, w, h)
        else:
            with open(path, "rb") as f:
                data = f.read()
    finally:
        bpy.data.images.remove(img)
    try:
        os.remove(path)
    except OSError:
        pass
    return {"mime": "image/png", "width": w, "height": h, "base64": base64.b64encode(data).decode("ascii")}


def _bounds(objs):
    pts = [ob.matrix_world @ Vector(c) for ob in objs for c in ob.bound_box]
    if not pts:
        return Vector((0, 0, 0)), 1.0
    mn = Vector(map(min, *pts))
    mx = Vector(map(max, *pts))
    center = (mn + mx) / 2
    radius = max((mx - mn).length / 2, 0.01)
    return center, radius


def _dir_for(view):
    """A named view, or an (azimuth, elevation) pair in degrees (azimuth 0 = front, counter-clockwise)."""
    if isinstance(view, (tuple, list)):
        az, el = math.radians(view[0]), math.radians(view[1])
        return Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
    return Vector(VIEW_DIRS.get(view, VIEW_DIRS["iso"])).normalized()


def _eye_for(view, center, radius, fov=_FOV):
    d = _dir_for(view)
    dist = radius / math.sin(fov / 2) * 1.05
    return center + d * dist, dist


def _view_matrix(eye, target, view):
    if view == "top":  # looking down -Z, +Y up on screen
        rot = Matrix.Identity(3)
    elif view == "bottom":  # looking up +Z
        rot = Matrix.Rotation(math.pi, 3, "X")
    else:
        rot = (target - eye).to_track_quat("-Z", "Y").to_matrix()
    cam = Matrix.Translation(eye) @ rot.to_4x4()
    return cam.inverted()


def _perspective(fov, aspect, near, far):
    f = 1.0 / math.tan(fov / 2)
    return Matrix((
        (f / aspect, 0, 0, 0),
        (0, f, 0, 0),
        (0, 0, (far + near) / (near - far), 2 * far * near / (near - far)),
        (0, 0, -1, 0),
    ))


_WIRE_OVERLAY = {  # overlay settings for a clean "wireframe on shaded" look; everything else off
    "show_overlays": True, "show_wireframes": True, "wireframe_threshold": 1.0, "wireframe_opacity": 1.0,
    "show_floor": False, "show_ortho_grid": False,
    "show_axis_x": False, "show_axis_y": False, "show_axis_z": False, "show_cursor": False,
    "show_object_origins": False, "show_text": False, "show_extras": False, "show_relationship_lines": False,
    "show_outline_selected": False, "show_stats": False, "show_bones": False,
}


def render_rgba(ctx, objs, views, size, isolate, shading, wireframe=False, bounds=None, before_view=None):
    """Offscreen renders as [(view, rgba uint8 (h, w, 4) top-row-first)].
    bounds: fixed (center, radius) instead of framing objs; before_view(i): called before each view is drawn
    (e.g. to change the frame), with isolation already in place."""
    area, region, win = ctx["area"], ctx["region"], ctx["window"]
    space = area.spaces.active
    ov = space.overlay
    scene, view_layer = win.scene, win.view_layer
    center, radius = bounds if bounds is not None else _bounds(objs)

    hidden = []
    saved_shading = space.shading.type
    saved_overlay = {k: getattr(ov, k) for k in _WIRE_OVERLAY if hasattr(ov, k)}
    w, h = size, size
    out = []
    off = None
    try:
        # everything that changes user-visible state happens inside try so finally always restores it
        if isolate:
            keep = set(objs)
            for o in view_layer.objects:
                if o not in keep and o.visible_get() and o.type not in ("LIGHT", "CAMERA"):
                    o.hide_set(True)
                    hidden.append(o)
            view_layer.update()
        if shading:
            space.shading.type = shading.upper()
        if wireframe:
            for k, v in _WIRE_OVERLAY.items():
                if hasattr(ov, k):
                    setattr(ov, k, v)
        else:
            ov.show_overlays = False
        off = gpu.types.GPUOffScreen(w, h)
        for i, view in enumerate(views):
            if before_view is not None:
                before_view(i)
            eye, dist = _eye_for(view, center, radius)
            vm = _view_matrix(eye, center, view)
            pm = _perspective(_FOV, w / h, max(0.001, dist - radius * 2), dist + radius * 2)
            off.draw_view3d(scene, view_layer, space, region, vm, pm, do_color_management=True)
            with off.bind():
                fb = gpu.state.active_framebuffer_get()
                buf = fb.read_color(0, 0, w, h, 4, 0, "UBYTE")
            buf.dimensions = w * h * 4
            rgba = np.ascontiguousarray(np.asarray(buf, dtype=np.uint8).reshape(h, w, 4)[::-1])
            rgba[:, :, 3] = 255
            out.append((view, rgba))
    finally:
        if off is not None:
            off.free()
        space.shading.type = saved_shading
        for k, v in saved_overlay.items():
            setattr(ov, k, v)
        for o in hidden:
            o.hide_set(False)
        if hidden:
            view_layer.update()
    return out


def render_views(ctx, objs, views, size, isolate, shading, wireframe=False):
    out = []
    for view, rgba in render_rgba(ctx, objs, views, size, isolate, shading, wireframe):
        h, w = rgba.shape[:2]
        out.append({"view": view if isinstance(view, str) else f"az{view[0]:g}_el{view[1]:g}",
                    "mime": "image/png", "width": w, "height": h,
                    "base64": base64.b64encode(encode_png(rgba, w, h)).decode("ascii")})
    return out


def contact_sheet(images, columns):
    """Tile equally sized rgba arrays into one image (dark gutters)."""
    h, w = images[0].shape[:2]
    rows = math.ceil(len(images) / columns)
    gap = 4
    sheet = np.full((rows * h + (rows - 1) * gap, columns * w + (columns - 1) * gap, 4), 30, dtype=np.uint8)
    sheet[:, :, 3] = 255
    for k, img in enumerate(images):
        r, c = divmod(k, columns)
        sheet[r * (h + gap):r * (h + gap) + h, c * (w + gap):c * (w + gap) + w] = img
    sh, sw = sheet.shape[:2]
    return {"mime": "image/png", "width": sw, "height": sh,
            "base64": base64.b64encode(encode_png(np.ascontiguousarray(sheet), sw, sh)).decode("ascii")}


def capture_viewport(ctx, max_size):
    area, region, win = ctx["area"], ctx["region"], ctx["window"]
    space = area.spaces.active
    r3d = space.region_3d
    w, h = region.width, region.height
    s = min(1.0, max_size / max(w, h))
    w, h = max(1, int(w * s)), max(1, int(h * s))
    off = gpu.types.GPUOffScreen(w, h)
    try:
        off.draw_view3d(win.scene, win.view_layer, space, region, r3d.view_matrix, r3d.window_matrix,
                        do_color_management=True)
        with off.bind():
            buf = gpu.state.active_framebuffer_get().read_color(0, 0, w, h, 4, 0, "UBYTE")
    finally:
        off.free()
    buf.dimensions = w * h * 4
    rgba = np.ascontiguousarray(np.asarray(buf, dtype=np.uint8).reshape(h, w, 4)[::-1])
    rgba[:, :, 3] = 255
    png = encode_png(rgba, w, h)
    return {"mime": "image/png", "width": w, "height": h,
            "view_perspective": r3d.view_perspective, "shading": space.shading.type,
            "base64": base64.b64encode(png).decode("ascii")}


def make_framing_camera(scene, objs, view, aspect):
    center, radius = _bounds(objs)
    cd = bpy.data.cameras.new("_mcp_tmp_cam")
    fov = _FOV
    cd.angle = fov
    cd.sensor_fit = "AUTO"
    # cd.angle applies to the wider image axis; fit the sphere to the narrower one.
    fit = 2 * math.atan(math.tan(fov / 2) / aspect) if aspect >= 1 else 2 * math.atan(math.tan(fov / 2) * aspect)
    eye, dist = _eye_for(view, center, radius, fit)
    cd.clip_start = max(0.001, dist - radius * 3)
    cd.clip_end = dist + radius * 3
    cam = bpy.data.objects.new("_mcp_tmp_cam", cd)
    scene.collection.objects.link(cam)
    cam.location = eye
    cam.matrix_world = _view_matrix(eye, center, view).inverted()
    return cam
