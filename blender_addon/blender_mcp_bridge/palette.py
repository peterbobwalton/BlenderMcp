"""Flat-colour texturing for low-poly art: a shared palette texture (one material, one texture, one draw
call for a whole prop set, no UV work) or per-face vertex colours."""

import json
import math
import os
import tempfile

import bmesh
import bpy

from .editmesh import select_faces
from .handlers import BridgeError, _ensure_object_mode, _mesh_obj, _p, _principled, command

_COLS = 8  # swatches per row


def _parse_color(c):
    if isinstance(c, str):
        h = c.lstrip("#")
        if len(h) != 6:
            raise BridgeError(f"Colour '{c}' must be #RRGGBB")
        return [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    if isinstance(c, (list, tuple)) and len(c) >= 3:
        return [float(v) for v in c[:3]]
    raise BridgeError(f"Colour must be '#RRGGBB' or [r,g,b] 0..1 (sRGB), got {c!r}")


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02X}" for v in rgb)


def _pow2(n):
    return 1 << max(3, math.ceil(math.log2(max(1, n))))


def _palette_image(name):
    img = bpy.data.images.get(f"T_{name}")
    if img is None or "mcp_palette" not in img:
        raise BridgeError(f"Palette '{name}' not found - call create_palette first")
    return img


def _remap_v(img, old_h, new_h):
    """Rows are laid out from the top, so a taller image moves every swatch in V: keep painted faces on
    their colour by remapping the UVs of faces whose material uses this palette."""
    k = old_h / new_h
    for me in bpy.data.meshes:
        uv = me.uv_layers.active
        if uv is None:
            continue
        slots = {i for i, m in enumerate(me.materials)
                 if m and m.node_tree and any(n.type == "TEX_IMAGE" and n.image == img for n in m.node_tree.nodes)}
        if not slots:
            continue
        for poly in me.polygons:
            if poly.material_index in slots:
                for li in poly.loop_indices:
                    d = uv.data[li].uv
                    d.y = 1.0 - (1.0 - d.y) * k


def _paint_image(img, colors, swatch):
    rows = max(1, math.ceil(len(colors) / _COLS))
    w = _pow2(_COLS * swatch)
    h = max(w, _pow2(rows * swatch))  # square until it overflows, so adding a colour rarely changes the height
    old_h = img.size[1] if "mcp_palette" in img else 0
    if old_h and old_h != h:
        _remap_v(img, old_h, h)
    # Repaint from scratch as a generated image: after a save the image is file-backed, and a moved or
    # deleted PNG would otherwise leave it without a pixel buffer ("failed to load image buffer").
    img.source = "GENERATED"
    img.generated_type = "BLANK"
    img.generated_width, img.generated_height = w, h
    px = [0.0] * (w * h * 4)
    for i, hexc in enumerate(colors):
        r, g, b = _parse_color(hexc)
        cx, cy = (i % _COLS) * swatch, h - (i // _COLS + 1) * swatch  # row 0 at the top
        for y in range(cy, cy + swatch):
            for x in range(cx, cx + swatch):
                k = (y * w + x) * 4
                px[k:k + 4] = (r, g, b, 1.0)
    img.pixels.foreach_set(px)
    img["mcp_palette"] = json.dumps(colors)
    img["mcp_swatch"] = swatch
    if img.filepath_raw:
        img.save()


def _swatch_uv(img, index):
    swatch = int(img["mcp_swatch"])
    w, h = img.size
    if not w or not h:  # file-backed palette whose PNG went missing: rebuild it from the stored colours
        _paint_image(img, json.loads(img["mcp_palette"]), swatch)
        w, h = img.size
    x = (index % _COLS) * swatch + swatch / 2
    y = h - (index // _COLS) * swatch - swatch / 2
    return (x / w, y / h)


@command("create_palette", undo=True)
def create_palette(p):
    """Palette texture T_<name> (8 swatches per row, nearest filtering) + material M_<name>."""
    name = _p(p, "name", "Palette")
    colors = [_hex(_parse_color(c)) for c in _p(p, "colors", required=True)]
    swatch = int(_p(p, "swatch", 8))
    folder = _p(p, "folder") or (os.path.join(os.path.dirname(bpy.data.filepath), "textures")
                                 if bpy.data.filepath else os.path.join(tempfile.gettempdir(), "BlenderMcp_palettes"))
    os.makedirs(folder, exist_ok=True)
    img = bpy.data.images.get(f"T_{name}") or bpy.data.images.new(f"T_{name}", 8, 8, alpha=False)
    if img.size[0] == 0 and img.source != "GENERATED":
        img.source = "GENERATED"
    img.colorspace_settings.name = "sRGB"
    img.filepath_raw = os.path.join(folder, f"T_{name}.png")
    img.file_format = "PNG"
    _paint_image(img, colors, swatch)

    mat = bpy.data.materials.get(f"M_{name}") or bpy.data.materials.new(f"M_{name}")
    bsdf = _principled(mat)
    nt = mat.node_tree
    tex = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image == img), None) or nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.interpolation = "Closest"  # crisp swatches, no bleeding between colours
    tex.location = (bsdf.location.x - 350, bsdf.location.y)
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = float(_p(p, "roughness", 0.8))
    bsdf.inputs["Metallic"].default_value = float(_p(p, "metallic", 0.0))
    return {"palette": name, "image": img.name, "path": img.filepath_raw, "material": mat.name,
            "size": list(img.size), "colors": {i: c for i, c in enumerate(colors)}}


def _resolve_index(img, p):
    colors = json.loads(img["mcp_palette"])
    if _p(p, "index") is not None:
        i = int(p["index"])
        if not 0 <= i < len(colors):
            raise BridgeError(f"index {i} out of range (palette has {len(colors)} colours)")
        return i, colors[i], False
    want = _hex(_parse_color(_p(p, "color", required=True)))
    if want in colors:
        return colors.index(want), want, False
    colors.append(want)  # grow the palette instead of silently picking a near colour
    _paint_image(img, colors, int(img["mcp_swatch"]))
    return len(colors) - 1, want, True


@command("paint_faces", undo=True)
def paint_faces(p):
    """Give faces a flat colour. mode=palette: UVs collapse onto a swatch of the shared palette texture
    (missing colours are added). mode=vertex: writes a 'Col' colour attribute."""
    ob = _mesh_obj(_p(p, "object", required=True))
    mode = _p(p, "mode", "palette").lower()
    _ensure_object_mode()
    if ob.data.users > 1:
        ob.data = ob.data.copy()
    me = ob.data

    if mode == "palette":
        pal = _p(p, "palette", "Palette")
        img = _palette_image(pal)
        index, hexc, added = _resolve_index(img, p)
        mat = bpy.data.materials.get(f"M_{pal}")
        u, v = _swatch_uv(img, index)
    elif mode == "vertex":
        hexc = _hex(_parse_color(_p(p, "color", required=True)))
        mat = bpy.data.materials.get("M_VertexColor") or _vertex_color_material()
    else:
        raise BridgeError("mode must be palette or vertex")

    slot = next((i for i, s in enumerate(ob.material_slots) if s.material == mat), None)
    if slot is None:
        me.materials.append(mat)
        slot = len(me.materials) - 1

    bm = bmesh.new()
    bm.from_mesh(me)
    try:
        faces = select_faces(ob, bm, _p(p, "selector"))
        for f in faces:
            f.material_index = slot
        if mode == "palette":
            uv = bm.loops.layers.uv.get(_p(p, "uv_layer") or "UVMap") or bm.loops.layers.uv.active \
                or bm.loops.layers.uv.new("UVMap")
            for f in faces:
                for loop in f.loops:
                    loop[uv].uv = (u, v)
        else:
            # bmesh "color" layers are byte colour attributes, which store sRGB values as given
            col = bm.loops.layers.color.get("Col") or bm.loops.layers.color.new("Col")
            rgba = _parse_color(hexc) + [1.0]
            for f in faces:
                for loop in f.loops:
                    loop[col] = rgba
        bm.to_mesh(me)
    finally:
        bm.free()
    me.update()
    out = {"object": ob.name, "faces": len(faces), "color": hexc, "mode": mode, "material": mat.name}
    if mode == "palette":
        out.update({"palette": pal, "index": index, "added_to_palette": added})
    return out


def _vertex_color_material():
    mat = bpy.data.materials.new("M_VertexColor")
    bsdf = _principled(mat)
    node = mat.node_tree.nodes.new("ShaderNodeVertexColor")
    node.layer_name = "Col"
    node.location = (bsdf.location.x - 300, bsdf.location.y)
    mat.node_tree.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.8
    return mat


@command("get_palette")
def get_palette(p):
    img = _palette_image(_p(p, "name", "Palette"))
    return {"palette": img.name[2:], "colors": dict(enumerate(json.loads(img["mcp_palette"]))),
            "size": list(img.size), "path": img.filepath_raw}
