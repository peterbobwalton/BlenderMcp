"""Asset review helpers: turntable contact sheets and texel-density / UV-usage checks."""

import math

import bmesh
import bpy

from . import imaging
from .handlers import BridgeError, _eval_mesh_stats, _mesh_obj, _objs, _p, _window_ctx, command


@command("turntable")
def turntable(p):
    """N views around the object(s) at one elevation, tiled into a single contact-sheet image."""
    objs = _objs(_p(p, "objects", required=True))
    frames = max(2, min(24, int(_p(p, "frames", 8))))
    elev = max(-80.0, min(80.0, float(_p(p, "elevation_deg", 20))))
    size = max(96, min(768, int(_p(p, "size", 256))))
    cols = int(_p(p, "columns", 4))
    ctx = _window_ctx(need_view3d=True)
    bpy.context.view_layer.update()
    views = [(360.0 * k / frames, elev) for k in range(frames)]
    shots = imaging.render_rgba(ctx, objs, views, size, bool(_p(p, "isolate", True)), _p(p, "shading"),
                                bool(_p(p, "wireframe", False)))
    sheet = imaging.contact_sheet([rgba for _, rgba in shots], min(cols, frames))
    sheet.update({"frames": frames, "elevation_deg": elev,
                  "tris": sum(_eval_mesh_stats(o)["tris"] for o in objs if o.type == "MESH")})
    return sheet


def _uv_stats(ob, layer_name, tex):
    me = ob.data
    if not me.uv_layers:
        return None
    layer = me.uv_layers.get(layer_name) if layer_name else me.uv_layers.active
    if layer is None:
        raise BridgeError(f"{ob.name} has no UV layer '{layer_name}'")
    bm = bmesh.new()
    bm.from_mesh(me)
    try:
        bm.transform(ob.matrix_world)  # world-space areas, so object scale counts
        uv = bm.loops.layers.uv[layer.name]
        world_area = uv_area = 0.0
        densities = []
        for f in bm.faces:
            wa = f.calc_area()
            pts = [l[uv].uv for l in f.loops]
            ua = 0.0
            for i in range(1, len(pts) - 1):
                a, b, c = pts[0], pts[i], pts[i + 1]
                ua += abs((b.x - a.x) * (c.y - a.y) - (c.x - a.x) * (b.y - a.y)) / 2
            world_area += wa
            uv_area += ua
            if wa > 1e-9 and ua > 0:
                densities.append((math.sqrt(ua / wa) * tex, wa))
        if not densities:
            return {"object": ob.name, "uv_layer": layer.name, "error": "UVs are degenerate (all faces collapsed)"}
        densities.sort()
        total = sum(w for _, w in densities)
        acc, median = 0.0, densities[-1][0]
        for d, w in densities:
            acc += w
            if acc >= total / 2:
                median = d
                break
        return {
            "object": ob.name, "uv_layer": layer.name,
            "texel_density_px_per_m": round(math.sqrt(uv_area / world_area) * tex, 1) if world_area else None,
            "median_px_per_m": round(median, 1),
            "min_px_per_m": round(densities[0][0], 1), "max_px_per_m": round(densities[-1][0], 1),
            "spread": round(densities[-1][0] / max(densities[0][0], 1e-9), 2),
            "uv_space_used_pct": round(min(uv_area, 9.99) * 100, 1),
            "surface_m2": round(world_area, 3),
        }
    finally:
        bm.free()


@command("texel_density")
def texel_density(p):
    """Pixels per metre each mesh gets at a texture size, how even it is across faces, and how much of
    the 0-1 UV square is used. Flags assets far from 'target_px_per_m' or with very uneven density."""
    names = _p(p, "objects", required=True)
    tex = int(_p(p, "texture_size", 1024))
    target = _p(p, "target_px_per_m")
    rows, warnings = [], []
    for ob in _objs(names):
        if ob.type != "MESH":
            continue
        st = _uv_stats(ob, _p(p, "uv_layer"), tex)
        if st is None:
            warnings.append(f"{ob.name}: no UVs")
            continue
        rows.append(st)
        if "error" in st:
            warnings.append(f"{ob.name}: {st['error']}")
            continue
        if st["spread"] > 4:
            warnings.append(f"{ob.name}: density varies {st['spread']}x across faces (stretched/uneven islands)")
        if st["uv_space_used_pct"] < 40:
            warnings.append(f"{ob.name}: only {st['uv_space_used_pct']}% of UV space used (wasted texture)")
        if st["uv_space_used_pct"] > 101:
            warnings.append(f"{ob.name}: UV area exceeds 100% - overlapping or out-of-bounds islands")
        if target and abs(st["texel_density_px_per_m"] - float(target)) / float(target) > 0.25:
            warnings.append(f"{ob.name}: {st['texel_density_px_per_m']} px/m vs target {target}")
    return {"texture_size": tex, "objects": rows, "warnings": warnings}
