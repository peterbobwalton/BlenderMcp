"""Command handlers. Every function here runs on Blender's main thread.

Handlers take a params dict and return something JSON-serialisable.
Raise BridgeError for user-facing failures (bad names, bad arguments).
Mutating commands are registered with undo=True so each call becomes one
undo step in Blender (Ctrl+Z works as the user expects).
"""

import base64
import contextlib
import io
import math
import os
import tempfile
import time

import bmesh
import bpy
from mathutils import Euler, Matrix, Vector

from . import imaging


class BridgeError(Exception):
    pass


_COMMANDS = {}


def command(name, undo=False):
    def deco(fn):
        _COMMANDS[name] = (fn, undo)
        return fn
    return deco


def dispatch(cmd, params):
    entry = _COMMANDS.get(cmd)
    if entry is None:
        raise BridgeError(f"Unknown command '{cmd}'. Available: {', '.join(sorted(_COMMANDS))}")
    fn, undo = entry
    try:
        return fn(params)
    finally:
        if undo:
            _undo_push(f"MCP: {cmd}")


# ---------------------------------------------------------------- helpers

def _p(params, key, default=None, required=False):
    if key in params and params[key] is not None:
        return params[key]
    if required:
        raise BridgeError(f"Missing required parameter '{key}'")
    return default


def _obj(name):
    if not isinstance(name, str):
        raise BridgeError(f"Object name must be a string, got {name!r}")
    ob = bpy.data.objects.get(name)
    if ob is None:
        close = [o.name for o in bpy.data.objects if name.lower() in o.name.lower()][:10]
        hint = f" Did you mean: {', '.join(close)}?" if close else ""
        raise BridgeError(f"Object '{name}' not found.{hint}")
    return ob


def _objs(names):
    if isinstance(names, str):
        names = [names]
    if not isinstance(names, (list, tuple)):
        raise BridgeError(f"Expected a list of object names, got {names!r}")
    return [_obj(n) for n in dict.fromkeys(names)]  # de-duplicated, order kept


def _mesh_obj(name):
    ob = _obj(name)
    if ob.type != "MESH":
        raise BridgeError(f"Object '{name}' is a {ob.type}, not a MESH")
    return ob


def _vec(v, n=3, name="vector"):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return [float(v)] * n
    if not isinstance(v, (list, tuple)):
        raise BridgeError(f"'{name}' must be a list of {n} numbers")
    if len(v) != n:
        raise BridgeError(f"'{name}' must have {n} numbers, got {len(v)}")
    return [float(x) for x in v]


def _r(v, nd=4):
    return [round(float(x), nd) for x in v]


def _scene():
    return bpy.context.scene


def _collection(name, create=False):
    if not name:
        return bpy.context.view_layer.active_layer_collection.collection
    col = bpy.data.collections.get(name)
    if col is None:
        if not create:
            raise BridgeError(f"Collection '{name}' not found. Existing: {[c.name for c in bpy.data.collections][:30]}")
        col = bpy.data.collections.new(name)
    if create and col not in _scene().collection.children_recursive:
        # exists in the file but not in this scene: link it, or new objects would be invisible
        _scene().collection.children.link(col)
    return col


def _window_ctx(need_view3d=False):
    wm = bpy.context.window_manager
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                region = next((r for r in area.regions if r.type == "WINDOW"), None)
                return {"window": win, "screen": win.screen, "area": area, "region": region}
    if need_view3d:
        raise BridgeError("No 3D Viewport is open in Blender")
    if wm.windows:
        return {"window": wm.windows[0], "screen": wm.windows[0].screen}
    return {}


def _undo_push(msg):
    try:
        with bpy.context.temp_override(**_window_ctx()):
            bpy.ops.ed.undo_push(message=msg)
    except Exception:
        pass


def _ensure_object_mode():
    ob = bpy.context.view_layer.objects.active
    if ob is not None and ob.mode != "OBJECT":
        with bpy.context.temp_override(**_window_ctx(), active_object=ob, object=ob):
            bpy.ops.object.mode_set(mode="OBJECT")


@contextlib.contextmanager
def _edit_mode(ob):
    """Enter edit mode on a single object with a valid context, restore after."""
    vl = bpy.context.view_layer
    prev_active = vl.objects.active
    prev_mode = prev_active.mode if prev_active is not None else "OBJECT"
    _ensure_object_mode()
    # select_all below replaces the user's element selection: keep it to put back afterwards
    me = ob.data
    saved_sel = []
    for attr in ("vertices", "edges", "polygons"):  # by name: edit mode reallocates the arrays
        elems = getattr(me, attr)
        flags = [False] * len(elems)
        elems.foreach_get("select", flags)
        saved_sel.append((attr, flags))
    prev_sel = [o for o in vl.objects if o.select_get()]
    for o in prev_sel:
        o.select_set(False)
    ob.select_set(True)
    vl.objects.active = ob
    ctx = dict(_window_ctx(), active_object=ob, object=ob, selected_objects=[ob], selected_editable_objects=[ob])
    try:
        with bpy.context.temp_override(**ctx):
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            yield
            bpy.ops.object.mode_set(mode="OBJECT")
    finally:
        if ob.mode != "OBJECT":
            with bpy.context.temp_override(**ctx):
                bpy.ops.object.mode_set(mode="OBJECT")
        ob.select_set(False)
        for o in prev_sel:
            if o.name in vl.objects:
                o.select_set(True)
        vl.objects.active = prev_active
        if ob.data == me:  # same topology count unless the operator changed it (separate, etc.)
            for attr, flags in saved_sel:
                elems = getattr(me, attr)
                if len(elems) == len(flags):
                    elems.foreach_set("select", flags)
        if prev_active is not None and prev_mode != "OBJECT":
            try:
                with bpy.context.temp_override(**_window_ctx(), active_object=prev_active, object=prev_active):
                    bpy.ops.object.mode_set(mode=prev_mode)
            except Exception:
                pass  # e.g. the object was removed or can't enter that mode any more


def _tri_count(mesh):
    return sum(len(p.vertices) - 2 for p in mesh.polygons)


def _eval_mesh_stats(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        return {"verts": len(me.vertices), "faces": len(me.polygons), "tris": _tri_count(me)}
    finally:
        ev.to_mesh_clear()


def _world_bounds(objs):
    mins = Vector((math.inf,) * 3)
    maxs = Vector((-math.inf,) * 3)
    for ob in objs:
        for c in ob.bound_box:
            w = ob.matrix_world @ Vector(c)
            mins = Vector(map(min, mins, w))
            maxs = Vector(map(max, maxs, w))
    if mins.x == math.inf:
        return Vector((0, 0, 0)), Vector((0, 0, 0))
    return mins, maxs


def _summary(ob, detail=False):
    bpy.context.view_layer.update()  # dimensions / matrix_world are stale until the depsgraph runs
    d = {
        "name": ob.name,
        "type": ob.type,
        "location": _r(ob.location),
        "rotation_deg": _r([math.degrees(a) for a in ob.rotation_euler], 2),
        "scale": _r(ob.scale),
        "dimensions": _r(ob.dimensions),
        "parent": ob.parent.name if ob.parent else None,
        "collections": [c.name for c in ob.users_collection],
        "visible": ob.visible_get(),
        "selected": ob.select_get(),
    }
    if ob.type == "MESH":
        me = ob.data
        d["mesh"] = {"verts": len(me.vertices), "faces": len(me.polygons), "tris": _tri_count(me)}
        d["materials"] = [s.material.name if s.material else None for s in ob.material_slots]
    if ob.modifiers:
        d["modifiers"] = [m.name + ":" + m.type for m in ob.modifiers]
    if detail:
        d["children"] = [c.name for c in ob.children]
        d["data_name"] = ob.data.name if ob.data else None
        d["hide_render"] = ob.hide_render
        d["display_type"] = ob.display_type
        mn, mx = _world_bounds([ob])
        d["world_bounds"] = {"min": _r(mn), "max": _r(mx)}
        d["custom_props"] = {k: _jsonable(ob[k]) for k in ob.keys() if not k.startswith("_")}
        if ob.type == "MESH":
            me = ob.data
            d["mesh"]["evaluated"] = _eval_mesh_stats(ob)
            d["mesh"]["uv_layers"] = [l.name for l in me.uv_layers]
            d["mesh"]["color_attributes"] = [a.name for a in me.color_attributes]
            d["mesh"]["vertex_groups"] = [g.name for g in ob.vertex_groups]
            d["mesh"]["shape_keys"] = [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else []
            d["modifiers_detail"] = [_modifier_info(m) for m in ob.modifiers]
        elif ob.type == "LIGHT":
            L = ob.data
            d["light"] = {"type": L.type, "energy": L.energy, "color": _r(L.color)}
        elif ob.type == "CAMERA":
            C = ob.data
            d["camera"] = {"lens": C.lens, "type": C.type, "clip": [C.clip_start, C.clip_end]}
    return d


def _jsonable(v):
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    try:
        return list(v)
    except TypeError:
        return str(v)


def _modifier_info(m):
    props = {}
    for p in m.bl_rna.properties:
        if p.identifier in ("rna_type", "name", "type") or p.type == "COLLECTION":
            continue
        try:
            val = getattr(m, p.identifier)
        except Exception:
            continue
        if p.type == "POINTER":
            val = getattr(val, "name", None) if val is not None else None
        props[p.identifier] = _jsonable(val)
    return {"name": m.name, "type": m.type, "show_viewport": m.show_viewport, "props": props}


def _set_rna_props(target, props, what):
    """Assign a dict of properties on any RNA struct, resolving object/material/collection names."""
    errors = []
    for key, val in (props or {}).items():
        rp = target.bl_rna.properties.get(key)
        if rp is None:
            errors.append(f"{what} has no property '{key}'")
            continue
        try:
            if rp.type == "POINTER" and isinstance(val, str):
                fixed = rp.fixed_type.identifier
                lookup = {"Object": bpy.data.objects, "Material": bpy.data.materials,
                          "Collection": bpy.data.collections, "Image": bpy.data.images,
                          "Texture": bpy.data.textures, "NodeTree": bpy.data.node_groups}.get(fixed)
                if lookup is None or val not in lookup:
                    raise BridgeError(f"{fixed} '{val}' not found")
                val = lookup[val]
            elif rp.type == "ENUM":
                ids = {e.identifier for e in rp.enum_items}

                def fix(v):
                    return v if (v in ids or not ids) else v.upper()
                if rp.is_enum_flag:
                    val = {fix(v) for v in ([val] if isinstance(val, str) else val)}
                elif isinstance(val, str):
                    val = fix(val)
            elif rp.type == "FLOAT" and rp.subtype in ("ANGLE", "EULER"):
                # angles are given in degrees (scalars and arrays such as rotation_euler)
                val = [math.radians(v) for v in val] if isinstance(val, (list, tuple)) else math.radians(val)
            setattr(target, key, val)
        except Exception as ex:
            errors.append(f"{key}: {ex}")
    return errors


# ----------------------------------------------------------- info / scene

@command("ping")
def ping(p):
    return {"pong": True, "blender": bpy.app.version_string, "time": time.time()}


@command("get_scene_info")
def get_scene_info(p):
    sc = _scene()
    counts = {}
    total_tris = 0
    for ob in sc.objects:
        counts[ob.type] = counts.get(ob.type, 0) + 1
        if ob.type == "MESH":
            total_tris += _tri_count(ob.data)

    def tree(col):
        return {"name": col.name, "objects": len(col.objects), "children": [tree(c) for c in col.children]}

    act = bpy.context.view_layer.objects.active
    return {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or None,
        "dirty": bpy.data.is_dirty,
        "scene": sc.name,
        "frame": {"current": sc.frame_current, "start": sc.frame_start, "end": sc.frame_end, "fps": sc.render.fps},
        "units": {"system": sc.unit_settings.system, "scale_length": sc.unit_settings.scale_length,
                  "length_unit": sc.unit_settings.length_unit},
        "render": {"engine": sc.render.engine, "resolution": [sc.render.resolution_x, sc.render.resolution_y]},
        "camera": sc.camera.name if sc.camera else None,
        "object_counts": counts,
        "total_base_tris": total_tris,
        "active_object": act.name if act else None,
        "selected": [o.name for o in bpy.context.view_layer.objects if o.select_get()],
        "collections": tree(sc.collection),
        "materials": len(bpy.data.materials),
        "images": len(bpy.data.images),
    }


@command("list_objects")
def list_objects(p):
    typ = (_p(p, "type") or "").upper()
    col = _p(p, "collection")
    contains = (_p(p, "name_contains") or "").lower()
    limit = int(_p(p, "limit", 200))
    source = _collection(col).all_objects if col else _scene().objects
    out = []
    truncated = False
    for ob in source:
        if typ and ob.type != typ:
            continue
        if contains and contains not in ob.name.lower():
            continue
        item = {"name": ob.name, "type": ob.type, "location": _r(ob.location, 3), "dimensions": _r(ob.dimensions, 3)}
        if ob.type == "MESH":
            item["tris"] = _tri_count(ob.data)
        if ob.parent:
            item["parent"] = ob.parent.name
        if len(out) >= limit:
            truncated = True
            break
        out.append(item)
    return {"count": len(out), "truncated": truncated, "objects": out}


@command("get_object")
def get_object(p):
    return _summary(_obj(_p(p, "name", required=True)), detail=True)


@command("get_selection")
def get_selection(p):
    vl = bpy.context.view_layer
    return {"active": vl.objects.active.name if vl.objects.active else None,
            "selected": [o.name for o in vl.objects if o.select_get()]}


@command("select_objects", undo=True)
def select_objects(p):
    names = _p(p, "names", [])
    vl = bpy.context.view_layer
    if not _p(p, "extend", False):
        for o in vl.objects:
            o.select_set(False)
    objs = _objs(names)
    for o in objs:
        o.select_set(True)
    active = _p(p, "active") or (objs[0].name if objs else None)
    if active:
        vl.objects.active = _obj(active)
    return get_selection({})


# ------------------------------------------------------------ create / edit

def _bm_torus(bm, major, minor, maj_seg, min_seg, uv_layer):
    rings = []
    for i in range(maj_seg):
        a = 2 * math.pi * i / maj_seg
        ring = []
        for j in range(min_seg):
            b = 2 * math.pi * j / min_seg
            r = major + minor * math.cos(b)
            ring.append(bm.verts.new((r * math.cos(a), r * math.sin(a), minor * math.sin(b))))
        rings.append(ring)
    for i in range(maj_seg):
        for j in range(min_seg):
            a, b = rings[i], rings[(i + 1) % maj_seg]
            f = bm.faces.new((a[j], b[j], b[(j + 1) % min_seg], a[(j + 1) % min_seg]))
            # unwrapped grid: u around the ring, v around the tube (no wrap-around seams)
            for loop, (du, dv) in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
                loop[uv_layer].uv = ((i + du) / maj_seg, (j + dv) / min_seg)


@command("create_primitive", undo=True)
def create_primitive(p):
    kind = _p(p, "kind", "cube").lower()
    name = _p(p, "name") or kind.capitalize()
    size = float(_p(p, "size", 2.0))
    seg = int(_p(p, "segments", 32))
    rings = int(_p(p, "rings", 16))
    depth = float(_p(p, "depth", size))
    radius = float(_p(p, "radius", size / 2))

    bm = bmesh.new()
    uv = bm.loops.layers.uv.new("UVMap")
    if kind == "cube":
        bmesh.ops.create_cube(bm, size=size, calc_uvs=True)
    elif kind in ("uv_sphere", "sphere"):
        bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=rings, radius=radius, calc_uvs=True)
    elif kind == "ico_sphere":
        bmesh.ops.create_icosphere(bm, subdivisions=int(_p(p, "subdivisions", 2)), radius=radius, calc_uvs=True)
    elif kind == "cylinder":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=radius, radius2=radius, depth=depth, calc_uvs=True)
    elif kind == "cone":
        bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=radius,
                              radius2=float(_p(p, "radius_top", 0.0)), depth=depth, calc_uvs=True)
    elif kind == "plane":
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=size / 2, calc_uvs=True)
    elif kind == "grid":
        bmesh.ops.create_grid(bm, x_segments=int(_p(p, "x_segments", 10)), y_segments=int(_p(p, "y_segments", 10)),
                              size=size / 2, calc_uvs=True)
    elif kind == "circle":
        bmesh.ops.create_circle(bm, cap_ends=bool(_p(p, "fill", True)), segments=seg, radius=radius, calc_uvs=True)
    elif kind in ("monkey", "suzanne"):
        bmesh.ops.create_monkey(bm, calc_uvs=True)
    elif kind == "torus":
        _bm_torus(bm, float(_p(p, "major_radius", 1.0)), float(_p(p, "minor_radius", 0.25)),
                  int(_p(p, "segments", 48)), int(_p(p, "rings", 12)), uv)
    else:
        bm.free()
        raise BridgeError("kind must be one of cube, uv_sphere, ico_sphere, cylinder, cone, plane, grid, circle, monkey, torus")

    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    _collection(_p(p, "collection"), create=True).objects.link(ob)
    _apply_transform_params(ob, p)
    if _p(p, "smooth", False):
        me.shade_smooth()
    if _p(p, "material"):
        _assign_material(ob, _p(p, "material"))
    return _summary(ob)


def _apply_transform_params(ob, p):
    loc = _vec(_p(p, "location"), name="location")
    rot = _vec(_p(p, "rotation"), name="rotation")
    scl = _vec(_p(p, "scale"), name="scale")
    if loc:
        ob.location = loc
    if rot:
        ob.rotation_euler = Euler([math.radians(a) for a in rot], ob.rotation_euler.order)
    if scl:
        ob.scale = scl


@command("create_empty", undo=True)
def create_empty(p):
    ob = bpy.data.objects.new(_p(p, "name", "Empty"), None)
    ob.empty_display_type = _p(p, "display", "PLAIN_AXES").upper()
    ob.empty_display_size = float(_p(p, "size", 1.0))
    _collection(_p(p, "collection"), create=True).objects.link(ob)
    _apply_transform_params(ob, p)
    return _summary(ob)


@command("create_light", undo=True)
def create_light(p):
    ltype = _p(p, "type", "POINT").upper()
    name = _p(p, "name", ltype.capitalize() + "Light")
    ld = bpy.data.lights.new(name, ltype)
    ld.energy = float(_p(p, "energy", 1000.0 if ltype != "SUN" else 3.0))
    if _p(p, "color"):
        ld.color = _vec(_p(p, "color"), name="color")
    ob = bpy.data.objects.new(name, ld)
    _collection(_p(p, "collection"), create=True).objects.link(ob)
    _apply_transform_params(ob, p)
    if _p(p, "look_at"):
        _look_at(ob, _vec(_p(p, "look_at")))
    return _summary(ob)


@command("create_camera", undo=True)
def create_camera(p):
    name = _p(p, "name", "Camera")
    cd = bpy.data.cameras.new(name)
    cd.lens = float(_p(p, "lens", 50.0))
    ob = bpy.data.objects.new(name, cd)
    _collection(_p(p, "collection"), create=True).objects.link(ob)
    _apply_transform_params(ob, p)
    if _p(p, "look_at"):
        _look_at(ob, _vec(_p(p, "look_at")))
    if _p(p, "set_active", True):
        _scene().camera = ob
    return _summary(ob)


def _look_at(ob, target):
    d = Vector(target) - ob.location
    if d.length > 1e-9:
        ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


@command("transform_object", undo=True)
def transform_object(p):
    ob = _obj(_p(p, "name", required=True))
    mode = _p(p, "mode", "set")
    loc = _vec(_p(p, "location"), name="location")
    rot = _vec(_p(p, "rotation"), name="rotation")
    scl = _vec(_p(p, "scale"), name="scale")
    if mode == "delta":
        if loc:
            ob.location = ob.location + Vector(loc)
        if rot:
            ob.rotation_euler = Euler([a + math.radians(b) for a, b in zip(ob.rotation_euler, rot)], ob.rotation_euler.order)
        if scl:
            ob.scale = Vector([a * b for a, b in zip(ob.scale, scl)])
    else:
        _apply_transform_params(ob, p)
    if _p(p, "dimensions"):
        ob.dimensions = _vec(_p(p, "dimensions"), name="dimensions")
    if _p(p, "look_at"):
        _look_at(ob, _vec(_p(p, "look_at")))
    return _summary(ob)


@command("set_object_properties", undo=True)
def set_object_properties(p):
    ob = _obj(_p(p, "name", required=True))
    if _p(p, "new_name"):
        ob.name = _p(p, "new_name")
        if ob.data is not None and _p(p, "rename_data", True) and ob.data.users == 1:
            ob.data.name = ob.name
    if "parent" in p:
        par = p["parent"]
        mw = ob.matrix_world.copy()
        ob.parent = _obj(par) if par else None
        if par:
            ob.matrix_parent_inverse = ob.parent.matrix_world.inverted()
        ob.matrix_world = mw
    for key in ("hide_viewport", "hide_render", "hide_select"):
        if key in p:
            setattr(ob, key, bool(p[key]))
    if "hide" in p:
        ob.hide_set(bool(p["hide"]))
    if _p(p, "display_type"):
        ob.display_type = p["display_type"].upper()
    if _p(p, "collection"):
        target = _collection(p["collection"], create=True)
        for c in list(ob.users_collection):
            c.objects.unlink(ob)
        target.objects.link(ob)
    if _p(p, "custom_props"):
        for k, v in p["custom_props"].items():
            ob[k] = v
    errs = _set_rna_props(ob, _p(p, "props"), "Object")
    out = _summary(ob)
    if errs:
        out["warnings"] = errs
    return out


@command("set_property", undo=True)
def set_property(p):
    """Generic setter: object + RNA data path, e.g. 'modifiers["Bevel"].width' or 'data.lens'."""
    ob = _obj(_p(p, "object", required=True))
    path = _p(p, "path", required=True)
    value = p.get("value")
    if "." in path:
        owner_path, attr = path.rsplit(".", 1)
        owner = ob.path_resolve(owner_path)
    else:
        owner, attr = ob, path
    errs = _set_rna_props(owner, {attr: value}, path)
    if errs:
        raise BridgeError("; ".join(errs))
    return {"object": ob.name, "path": path, "value": _jsonable(getattr(owner, attr))}


@command("delete_objects", undo=True)
def delete_objects(p):
    names = _p(p, "names", required=True)
    removed = []
    for ob in _objs(names):
        data = ob.data
        removed.append(ob.name)
        bpy.data.objects.remove(ob, do_unlink=True)
        if data is not None and _p(p, "delete_data", True) and data.users == 0:
            pool = {bpy.types.Mesh: bpy.data.meshes, bpy.types.Curve: bpy.data.curves,
                    bpy.types.Light: bpy.data.lights, bpy.types.Camera: bpy.data.cameras}
            for t, coll in pool.items():
                if isinstance(data, t):
                    coll.remove(data)
                    break
    return {"removed": removed}


@command("duplicate_object", undo=True)
def duplicate_object(p):
    src = _obj(_p(p, "name", required=True))
    count = int(_p(p, "count", 1))
    linked = bool(_p(p, "linked", False))
    offset = Vector(_vec(_p(p, "offset", [0, 0, 0])))
    out = []
    for i in range(count):
        ob = src.copy()
        if src.data is not None and not linked:
            ob.data = src.data.copy()
        ob.name = _p(p, "new_name") if (count == 1 and _p(p, "new_name")) else ob.name
        ob.location = src.location + offset * (i + 1)
        for c in src.users_collection:
            c.objects.link(ob)
        out.append(ob.name)
    return {"created": out}


@command("create_collection", undo=True)
def create_collection(p):
    name = _p(p, "name", required=True)
    parent = _p(p, "parent")
    par = _collection(parent) if parent else _scene().collection
    col = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    if col == par:
        raise BridgeError("A collection cannot be its own parent")
    if col.name not in par.children:
        par.children.link(col)
    return {"name": col.name, "parent": par.name}


# ------------------------------------------------------------- modifiers

@command("add_modifier", undo=True)
def add_modifier(p):
    ob = _obj(_p(p, "object", required=True))
    mtype = _p(p, "type", required=True).upper()
    valid = [e.identifier for e in bpy.types.Modifier.bl_rna.properties["type"].enum_items
             if not e.identifier.startswith("GREASE_PENCIL")]
    if mtype not in valid:
        raise BridgeError(f"Unknown modifier type '{mtype}'. Valid: {', '.join(valid)}")
    mod = ob.modifiers.new(_p(p, "name", mtype.title()), mtype)
    if mod is None:
        raise BridgeError(f"Could not add modifier {mtype} to {ob.type}")
    errs = _set_rna_props(mod, _p(p, "props"), f"{mtype} modifier")
    out = _modifier_info(mod)
    if errs:
        out["warnings"] = errs
    return out


@command("set_modifier", undo=True)
def set_modifier(p):
    ob = _obj(_p(p, "object", required=True))
    mod = ob.modifiers.get(_p(p, "name", required=True))
    if mod is None:
        raise BridgeError(f"No modifier '{p['name']}' on {ob.name}. Has: {[m.name for m in ob.modifiers]}")
    errs = _set_rna_props(mod, _p(p, "props"), "modifier")
    out = _modifier_info(mod)
    if errs:
        out["warnings"] = errs
    return out


@command("remove_modifier", undo=True)
def remove_modifier(p):
    ob = _obj(_p(p, "object", required=True))
    mod = ob.modifiers.get(_p(p, "name", required=True))
    if mod is None:
        raise BridgeError(f"No modifier '{p['name']}' on {ob.name}")
    ob.modifiers.remove(mod)
    return {"object": ob.name, "modifiers": [m.name for m in ob.modifiers]}


@command("apply_modifiers", undo=True)
def apply_modifiers(p):
    """Apply one named modifier, or all of them (stack order) when name is omitted."""
    ob = _mesh_obj(_p(p, "object", required=True))
    name = _p(p, "name")
    before = _tri_count(ob.data)
    if name:
        if ob.modifiers.get(name) is None:
            raise BridgeError(f"No modifier '{name}' on {ob.name}")
        _ensure_object_mode()
        if ob.data.users > 1:
            ob.data = ob.data.copy()
        with bpy.context.temp_override(**_window_ctx(), object=ob, active_object=ob,
                                       selected_objects=[ob], selected_editable_objects=[ob]):
            bpy.ops.object.modifier_apply(modifier=name)
    else:
        _bake_evaluated(ob)
    return {"object": ob.name, "tris_before": before, "tris_after": _tri_count(ob.data),
            "modifiers": [m.name for m in ob.modifiers]}


def _bake_evaluated(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    new_me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    old = ob.data
    name = old.name
    ob.modifiers.clear()
    ob.data = new_me
    if old.users == 0:
        bpy.data.meshes.remove(old)
    new_me.name = name  # after removing the old mesh, so it doesn't become "name.001"


# ------------------------------------------------------------- materials

def _principled(mat):
    if hasattr(mat, "use_nodes") and not mat.use_nodes:
        try:
            mat.use_nodes = True
        except Exception:
            pass
    nt = mat.node_tree
    node = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
    if node is None:
        node = nt.nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None) or nt.nodes.new("ShaderNodeOutputMaterial")
        nt.links.new(node.outputs["BSDF"], out.inputs["Surface"])
    return node


_INPUT_ALIASES = {
    "base_color": "Base Color", "color": "Base Color", "metallic": "Metallic", "roughness": "Roughness",
    "alpha": "Alpha", "ior": "IOR", "emission_color": "Emission Color", "emission": "Emission Color",
    "emission_strength": "Emission Strength", "specular": "Specular IOR Level",
    "transmission": "Transmission Weight", "coat": "Coat Weight", "subsurface": "Subsurface Weight",
    "normal": "Normal",
}


def _set_principled_values(mat, values):
    node = _principled(mat)
    warnings = []
    for k, v in (values or {}).items():
        sock_name = _INPUT_ALIASES.get(k, k)
        sock = node.inputs.get(sock_name)
        if sock is None:
            warnings.append(f"Principled BSDF has no input '{sock_name}'")
            continue
        if isinstance(v, list) and len(v) == 3 and sock.type == "RGBA":
            v = v + [1.0]
        try:
            sock.default_value = v
        except Exception as ex:
            warnings.append(f"{sock_name}: {ex}")
    vals = values or {}
    if any(k in vals for k in ("emission_color", "emission", "Emission Color")) and \
            not any(k in vals for k in ("emission_strength", "Emission Strength")) and \
            node.inputs["Emission Strength"].default_value == 0.0:
        node.inputs["Emission Strength"].default_value = 1.0  # otherwise the colour has no effect
    if "alpha" in (values or {}) and float(values["alpha"]) < 1.0:
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"
    if "base_color" in (values or {}) or "color" in (values or {}):
        c = values.get("base_color", values.get("color"))
        mat.diffuse_color = (list(c) + [1.0])[:4]  # viewport solid colour
    return warnings


@command("create_material", undo=True)
def create_material(p):
    name = _p(p, "name", required=True)
    mat = bpy.data.materials.get(name)
    if mat is None or not _p(p, "reuse", True):
        mat = bpy.data.materials.new(name)
    vals = {k: p[k] for k in _INPUT_ALIASES if k in p and k != "normal"}
    vals.update(_p(p, "inputs", {}) or {})
    warnings = _set_principled_values(mat, vals)
    if _p(p, "assign_to"):
        for o in _objs(p["assign_to"]):
            _assign_material(o, mat.name)
    return {"material": mat.name, "warnings": warnings} if warnings else {"material": mat.name}


@command("set_material", undo=True)
def set_material(p):
    mat = bpy.data.materials.get(_p(p, "name", required=True))
    if mat is None:
        raise BridgeError(f"Material '{p['name']}' not found")
    vals = {k: p[k] for k in _INPUT_ALIASES if k in p and k != "normal"}
    vals.update(_p(p, "inputs", {}) or {})
    w = _set_principled_values(mat, vals)
    return {"material": mat.name, "warnings": w}


def _assign_material(ob, mat_name, slot=None):
    mat = bpy.data.materials.get(mat_name)
    if mat is None:
        raise BridgeError(f"Material '{mat_name}' not found")
    if ob.data is None or not hasattr(ob.data, "materials"):
        raise BridgeError(f"{ob.name} cannot hold materials")
    if slot is None:
        if len(ob.data.materials) == 0:
            ob.data.materials.append(mat)
        else:
            ob.data.materials[0] = mat
    else:
        while len(ob.data.materials) <= slot:
            ob.data.materials.append(None)
        ob.data.materials[slot] = mat


@command("assign_material", undo=True)
def assign_material(p):
    names = _p(p, "objects", required=True)
    mat = _p(p, "material", required=True)
    slot = _p(p, "slot")
    faces = _p(p, "face_indices")
    if faces is not None and slot is None:
        raise BridgeError("face_indices requires 'slot' (the material slot to assign those faces to)")
    if faces is not None:
        _ensure_object_mode()  # polygon edits made in edit mode are overwritten on exit
    done = []
    for ob in _objs(names):
        _assign_material(ob, mat, slot)
        if faces is not None and slot is not None and ob.type == "MESH":
            polys = ob.data.polygons
            for i in faces:
                if 0 <= i < len(polys):
                    polys[i].material_index = slot
        done.append(ob.name)
    return {"assigned": done, "material": mat}


_TEX_TARGETS = {
    "base_color": ("Base Color", "sRGB"), "roughness": ("Roughness", "Non-Color"),
    "metallic": ("Metallic", "Non-Color"), "normal": ("Normal", "Non-Color"),
    "emission": ("Emission Color", "sRGB"), "alpha": ("Alpha", "Non-Color"),
}


@command("add_image_texture", undo=True)
def add_image_texture(p):
    mat = bpy.data.materials.get(_p(p, "material", required=True))
    if mat is None:
        raise BridgeError(f"Material '{p['material']}' not found")
    path = _p(p, "path", required=True)
    target = _p(p, "target", "base_color").lower()
    if target not in _TEX_TARGETS:
        raise BridgeError(f"target must be one of {list(_TEX_TARGETS)}")
    if not os.path.isfile(path):
        raise BridgeError(f"Image file not found: {path}")
    sock_name, cs = _TEX_TARGETS[target]
    node = _principled(mat)
    nt = mat.node_tree
    img = bpy.data.images.load(path, check_existing=True)
    img.colorspace_settings.name = cs
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    tex.location = (node.location.x - 500, node.location.y - 250 * len([n for n in nt.nodes if n.type == "TEX_IMAGE"]))
    if target == "normal":
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nm.location = (node.location.x - 220, tex.location.y)
        nt.links.new(tex.outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], node.inputs["Normal"])
    elif target == "alpha":
        nt.links.new(tex.outputs["Alpha"], node.inputs["Alpha"])
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = "BLENDED"
    else:
        nt.links.new(tex.outputs["Color"], node.inputs[sock_name])
    return {"material": mat.name, "image": img.name, "target": target, "size": list(img.size)}


@command("list_materials")
def list_materials(p):
    out = []
    for m in bpy.data.materials:
        item = {"name": m.name, "users": m.users}
        if m.node_tree:
            node = next((n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if node:
                item["base_color"] = _r(node.inputs["Base Color"].default_value, 3)
                item["metallic"] = round(node.inputs["Metallic"].default_value, 3)
                item["roughness"] = round(node.inputs["Roughness"].default_value, 3)
            item["textures"] = [n.image.name for n in m.node_tree.nodes if n.type == "TEX_IMAGE" and n.image]
        out.append(item)
    return {"materials": out}


# ------------------------------------------------------------- mesh ops

@command("mesh_cleanup", undo=True)
def mesh_cleanup(p):
    """Context-free bmesh cleanup: merge by distance, dissolve degenerate, delete loose, recalc normals."""
    ob = _mesh_obj(_p(p, "object", required=True))
    _ensure_object_mode()
    me = ob.data
    before = {"verts": len(me.vertices), "faces": len(me.polygons)}
    bm = bmesh.new()
    bm.from_mesh(me)
    report = {}
    dist = float(_p(p, "merge_distance", 0.0001))
    if dist > 0:
        n = len(bm.verts)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
        report["merged_verts"] = n - len(bm.verts)
    if _p(p, "dissolve_degenerate", True):
        bmesh.ops.dissolve_degenerate(bm, dist=dist or 0.0001, edges=bm.edges)
    if _p(p, "delete_loose", True):
        loose_v = [v for v in bm.verts if not v.link_edges]
        loose_e = [e for e in bm.edges if not e.link_faces]
        bmesh.ops.delete(bm, geom=loose_e, context="EDGES")
        loose_v = [v for v in bm.verts if not v.link_edges]
        bmesh.ops.delete(bm, geom=loose_v, context="VERTS")
        report["deleted_loose"] = len(loose_v) + len(loose_e)
    if _p(p, "recalc_normals", True):
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    if _p(p, "triangulate", False):
        bmesh.ops.triangulate(bm, faces=bm.faces, quad_method="BEAUTY", ngon_method="BEAUTY")
    bm.to_mesh(me)
    bm.free()
    me.update()
    report.update({"object": ob.name, "before": before,
                   "after": {"verts": len(me.vertices), "faces": len(me.polygons), "tris": _tri_count(me)}})
    return report


def _auto_sharp(me, angle):
    """Smooth shading with edges sharper than `angle` (radians) marked sharp. Returns sharp edge count."""
    me.shade_smooth()
    bm = bmesh.new()
    bm.from_mesh(me)
    sharp = 0
    for e in bm.edges:
        if len(e.link_faces) != 2 or e.calc_face_angle(0.0) > angle:
            e.smooth = False
            sharp += 1
    bm.to_mesh(me)
    bm.free()
    return sharp


@command("set_shading", undo=True)
def set_shading(p):
    """mode: flat | smooth | auto (smooth + sharp edges above angle_deg)."""
    names = _p(p, "objects", required=True)
    mode = _p(p, "mode", "auto").lower()
    angle = math.radians(float(_p(p, "angle_deg", 30.0)))
    _ensure_object_mode()  # mesh edits made in edit mode are overwritten on exit
    out = []
    for ob in _objs(names):
        if ob.type != "MESH":
            continue
        me = ob.data
        if mode == "flat":
            me.shade_flat()
        elif mode == "smooth":
            me.shade_smooth()
        else:
            sharp = _auto_sharp(me, angle)
            out.append({"object": ob.name, "sharp_edges": sharp})
            continue
        out.append({"object": ob.name, "mode": mode})
    return {"results": out}


@command("apply_transform", undo=True)
def apply_transform(p):
    """Bake location/rotation/scale into mesh data (like Ctrl+A), keeping children in place."""
    names = _p(p, "objects", required=True)
    do_loc = bool(_p(p, "location", False))
    do_rot = bool(_p(p, "rotation", True))
    do_scl = bool(_p(p, "scale", True))
    _ensure_object_mode()  # mesh edits made in edit mode are overwritten on exit
    out = []
    for ob in _objs(names):
        if ob.type != "MESH":
            continue
        if ob.data.users > 1:
            ob.data = ob.data.copy()
        basis = ob.matrix_basis.copy()
        loc, rot, scl = basis.decompose()
        keep_loc = Matrix.Identity(4) if do_loc else Matrix.Translation(loc)
        keep_rot = Matrix.Identity(4) if do_rot else rot.to_matrix().to_4x4()
        keep_scl = Matrix.Identity(4) if do_scl else Matrix.Diagonal(scl).to_4x4()
        new_basis = keep_loc @ keep_rot @ keep_scl
        # whatever part of the old basis we drop is baked into the mesh: basis == new_basis @ applied
        applied = new_basis.inverted_safe() @ basis
        ob.data.transform(applied, shape_keys=True)
        if applied.determinant() < 0:
            ob.data.flip_normals()  # Mesh.transform inverts winding for mirroring matrices
        ob.matrix_basis = new_basis
        for child in ob.children:
            child.matrix_parent_inverse = applied @ child.matrix_parent_inverse
        ob.data.update()
    for ob in _objs(names):
        if ob.type == "MESH":
            out.append(_summary(ob))
    return {"results": out}


@command("set_origin", undo=True)
def set_origin(p):
    """where: bounds_center | bottom_center | median | world_origin | cursor | [x,y,z] (world)."""
    ob = _mesh_obj(_p(p, "object", required=True))
    where = _p(p, "where", "bottom_center")
    _ensure_object_mode()  # mesh edits made in edit mode are overwritten on exit
    me = ob.data
    if me.users > 1:
        ob.data = me = me.copy()
    bpy.context.view_layer.update()
    bb = [Vector(c) for c in ob.bound_box]
    mn = Vector(map(min, *bb)) if bb else Vector()
    mx = Vector(map(max, *bb)) if bb else Vector()
    inv = ob.matrix_world.inverted()
    if isinstance(where, list):
        local = inv @ Vector(_vec(where))
    elif where == "bounds_center":
        local = (mn + mx) / 2
    elif where == "bottom_center":
        c = (mn + mx) / 2
        local = Vector((c.x, c.y, mn.z))
    elif where == "median":
        local = sum((v.co for v in me.vertices), Vector()) / max(1, len(me.vertices))
    elif where == "world_origin":
        local = inv @ Vector((0, 0, 0))
    elif where == "cursor":
        local = inv @ _scene().cursor.location
    else:
        raise BridgeError("where must be bounds_center, bottom_center, median, world_origin, cursor or [x,y,z]")
    me.transform(Matrix.Translation(-local), shape_keys=True)
    ob.matrix_world = ob.matrix_world @ Matrix.Translation(local)
    for child in ob.children:
        child.matrix_parent_inverse = Matrix.Translation(-local) @ child.matrix_parent_inverse
    me.update()
    bpy.context.view_layer.update()
    return _summary(ob)


@command("uv_unwrap", undo=True)
def uv_unwrap(p):
    """method: smart | lightmap | cube. Optionally into a named (new) UV layer."""
    ob = _mesh_obj(_p(p, "object", required=True))
    method = _p(p, "method", "smart").lower()
    if method not in ("smart", "lightmap", "cube"):
        raise BridgeError("method must be smart, lightmap or cube")
    layer = _p(p, "uv_layer")
    me = ob.data
    prev_active = me.uv_layers.active.name if me.uv_layers.active else None
    if layer:
        uvl = me.uv_layers.get(layer) or me.uv_layers.new(name=layer)
        if uvl is None:
            raise BridgeError("Could not create UV layer (limit is 8)")
        me.uv_layers.active = uvl
    elif not me.uv_layers:
        me.uv_layers.new(name="UVMap")
    margin = float(_p(p, "margin", 0.02))
    with _edit_mode(ob):
        if method == "smart":
            bpy.ops.uv.smart_project(angle_limit=math.radians(float(_p(p, "angle_deg", 66))), island_margin=margin)
        elif method == "lightmap":
            bpy.ops.uv.lightmap_pack(PREF_CONTEXT="ALL_FACES", PREF_MARGIN_DIV=max(0.001, margin * 10))
        else:
            bpy.ops.uv.cube_project(cube_size=float(_p(p, "cube_size", 1.0)))
    written = me.uv_layers.active.name
    if layer and prev_active and prev_active in me.uv_layers:
        # keep the texturing UV active so later edits don't land in e.g. the lightmap channel
        me.uv_layers.active = me.uv_layers[prev_active]
    return {"object": ob.name, "uv_layers": [l.name for l in me.uv_layers], "written": written,
            "active": me.uv_layers.active.name}


# ------------------------------------------------------ game-asset pipeline

@command("mesh_stats")
def mesh_stats(p):
    names = _p(p, "objects")
    objs = _objs(names) if names else [o for o in _scene().objects if o.type == "MESH"]
    rows, tot = [], {"verts": 0, "tris": 0}
    for ob in objs:
        if ob.type != "MESH":
            continue
        base = {"verts": len(ob.data.vertices), "tris": _tri_count(ob.data)}
        ev = _eval_mesh_stats(ob) if ob.modifiers else base
        rows.append({"name": ob.name, "base": base, "evaluated": ev, "materials": len(ob.material_slots)})
        tot["verts"] += ev["verts"]
        tot["tris"] += ev["tris"]
    rows.sort(key=lambda r: -r["evaluated"]["tris"])
    return {"objects": rows, "total_evaluated": tot}


@command("validate_asset")
def validate_asset(p):
    """Game-readiness checks. Returns issues with severity so the model can fix them."""
    ob = _mesh_obj(_p(p, "object", required=True))
    budget = _p(p, "tri_budget")
    me = ob.data
    issues = []

    def add(sev, code, msg):
        issues.append({"severity": sev, "code": code, "message": msg})

    sc = ob.scale
    if any(abs(s - 1.0) > 1e-4 for s in sc):
        add("warning", "unapplied_scale", f"Scale is {_r(sc, 3)}; apply scale before export")
    if any(abs(a) > 1e-4 for a in ob.rotation_euler):
        add("info", "unapplied_rotation", "Rotation is not zero; consider applying rotation")
    if (sc.x * sc.y * sc.z) < 0:
        add("error", "negative_scale", "Negative scale flips normals in engines")
    if not me.uv_layers:
        add("error", "no_uvs", "Mesh has no UV map")
    elif len(me.uv_layers) < 2 and _p(p, "require_lightmap_uv", False):
        add("warning", "no_lightmap_uv", "No second UV channel for lightmaps")
    if not ob.material_slots or all(s.material is None for s in ob.material_slots):
        add("warning", "no_material", "No material assigned")
    if len(ob.material_slots) > int(_p(p, "max_materials", 4)):
        add("warning", "many_materials", f"{len(ob.material_slots)} material slots = {len(ob.material_slots)} draw calls")
    ngons = sum(1 for poly in me.polygons if len(poly.vertices) > 4)
    if ngons:
        add("info", "ngons", f"{ngons} n-gons (engine will triangulate; check shading)")

    bm = bmesh.new()
    bm.from_mesh(me)
    non_manifold = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    loose = sum(1 for v in bm.verts if not v.link_edges)
    zero_faces = sum(1 for f in bm.faces if f.calc_area() < 1e-10)
    dup = len(bmesh.ops.find_doubles(bm, verts=bm.verts, dist=1e-5)["targetmap"])
    bm.free()
    if non_manifold:
        add("warning", "non_manifold", f"{non_manifold} non-manifold edges")
    if boundary:
        add("info", "open_edges", f"{boundary} boundary edges (mesh is not closed)")
    if loose:
        add("warning", "loose_verts", f"{loose} loose vertices")
    if zero_faces:
        add("warning", "zero_area_faces", f"{zero_faces} zero-area faces")
    if dup:
        add("warning", "duplicate_verts", f"{dup} overlapping vertices (merge by distance)")

    ev = _eval_mesh_stats(ob)
    if budget and ev["tris"] > int(budget):
        add("error", "over_budget", f"{ev['tris']} tris exceeds budget {budget}")
    if ob.modifiers:
        add("info", "unapplied_modifiers", f"Modifiers not applied: {[m.name for m in ob.modifiers]} (exporter can apply them)")

    ucx = [o.name for o in bpy.data.objects if o.name.startswith(_collision_prefixes(ob.name))]
    if _p(p, "require_collision", False) and not ucx:
        add("warning", "no_collision", "No UCX_/UBX_/USP_ collision mesh found")
    dims = ob.dimensions
    return {
        "object": ob.name,
        "ok": not any(i["severity"] == "error" for i in issues),
        "tris": ev["tris"],
        "dimensions_m": _r(dims, 3),
        "collision": ucx,
        "uv_layers": [l.name for l in me.uv_layers],
        "issues": issues,
    }


@command("decimate", undo=True)
def decimate(p):
    """Reduce to a target triangle count (or ratio). apply=true bakes it into the mesh."""
    ob = _mesh_obj(_p(p, "object", required=True))
    cur = _eval_mesh_stats(ob)["tris"]
    target = _p(p, "target_tris")
    ratio = float(_p(p, "ratio", 0.5))
    apply = bool(_p(p, "apply", True))
    if target:
        # applying moves the modifier to the top of the stack, so it sees the base mesh, not the evaluated one
        base = _tri_count(ob.data) if apply else cur
        ratio = max(0.0001, min(1.0, float(target) / max(1, base)))
    mod = ob.modifiers.new("MCP_Decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = ratio
    mod.use_collapse_triangulate = bool(_p(p, "triangulate", False))
    if _p(p, "symmetry"):
        mod.use_symmetry = True
        mod.symmetry_axis = p["symmetry"].upper()
    if apply:
        _bake_evaluated_single(ob, mod)
    return {"object": ob.name, "ratio": round(ratio, 4), "tris_before": cur, "tris_after": _eval_mesh_stats(ob)["tris"]}


def _bake_evaluated_single(ob, mod):
    """Apply only `mod` by moving it to the top of the stack first."""
    _ensure_object_mode()
    if ob.data.users > 1:
        ob.data = ob.data.copy()
    idx = list(ob.modifiers).index(mod)
    if idx != 0:
        ob.modifiers.move(idx, 0)
    with bpy.context.temp_override(**_window_ctx(), object=ob, active_object=ob,
                                   selected_objects=[ob], selected_editable_objects=[ob]):
        bpy.ops.object.modifier_apply(modifier=mod.name)


@command("generate_lods", undo=True)
def generate_lods(p):
    """Create <name>_LOD0..N copies with decimation baked in, grouped in a collection."""
    ob = _mesh_obj(_p(p, "object", required=True))
    ratios = _p(p, "ratios", [1.0, 0.5, 0.25, 0.125])
    base_tris = _eval_mesh_stats(ob)["tris"]
    col = _collection(_p(p, "collection") or f"{ob.name}_LODs", create=True)
    spacing = float(_p(p, "spacing", 0.0))
    out = []
    dg = bpy.context.evaluated_depsgraph_get()
    base_me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    for i, r in enumerate(ratios):
        name = f"{ob.name}_LOD{i}"
        old = bpy.data.objects.get(name)
        if old:
            _remove_object_and_mesh(old)
        lod = bpy.data.objects.new(name, base_me.copy())
        lod.data.name = name
        lod.matrix_world = ob.matrix_world.copy()
        if spacing:
            lod.location.x += spacing * (i + 1)
        col.objects.link(lod)
        if r < 0.999:
            m = lod.modifiers.new("Decimate", "DECIMATE")
            m.ratio = float(r)
            _bake_evaluated(lod)
            # Decimation scrambles sharp-edge flags; re-derive them so LODs shade like LOD0.
            sharp_deg = _p(p, "sharp_angle_deg", 35.0)
            if sharp_deg:
                _auto_sharp(lod.data, math.radians(float(sharp_deg)))
        out.append({"name": name, "ratio": r, "tris": _tri_count(lod.data)})
    bpy.data.meshes.remove(base_me)
    if _p(p, "hide_source", False):
        ob.hide_set(True)
    return {"source": ob.name, "source_tris": base_tris, "collection": col.name, "lods": out}


def _hull_only(bm):
    """Replace bm's contents with just its convex hull (convex_hull() adds the hull but keeps the input)."""
    if len(bm.verts) < 4:
        raise BridgeError("Need at least 4 vertices for a convex hull")
    res = bmesh.ops.convex_hull(bm, input=bm.verts[:] + bm.edges[:] + bm.faces[:], use_existing_faces=False)
    hull = set(res["geom"])
    bmesh.ops.delete(bm, geom=[f for f in bm.faces if f not in hull], context="FACES_ONLY")
    bmesh.ops.delete(bm, geom=[e for e in bm.edges if e not in hull], context="EDGES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)


@command("generate_collision", undo=True)
def generate_collision(p):
    """Unreal-style collision: box (UBX_), sphere (USP_), convex hull (UCX_)."""
    ob = _mesh_obj(_p(p, "object", required=True))
    kind = _p(p, "kind", "convex").lower()
    idx = int(_p(p, "index", 0))
    prefix = {"box": "UBX", "sphere": "USP", "convex": "UCX"}.get(kind)
    if prefix is None:
        raise BridgeError("kind must be box, sphere or convex")
    name = f"{prefix}_{ob.name}_{idx:02d}"
    old = bpy.data.objects.get(name)
    if old:
        _remove_object_and_mesh(old)
    bpy.context.view_layer.update()
    bm = bmesh.new()
    bb = [Vector(c) for c in ob.bound_box]
    mn, mx = Vector(map(min, *bb)), Vector(map(max, *bb))
    center, size = (mn + mx) / 2, (mx - mn)
    if kind == "box":
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=size, verts=bm.verts)
        bmesh.ops.translate(bm, vec=center, verts=bm.verts)
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=8, radius=max(size) / 2)
        bmesh.ops.translate(bm, vec=center, verts=bm.verts)
    else:
        dg = bpy.context.evaluated_depsgraph_get()
        ev = ob.evaluated_get(dg)
        tmp = ev.to_mesh()
        bm.from_mesh(tmp)
        ev.to_mesh_clear()
        _hull_only(bm)
        max_verts = int(_p(p, "max_verts", 64))
        if len(bm.verts) > max_verts:
            # simplify (decimate) then re-hull so the result is convex again
            me_tmp = bpy.data.meshes.new("_tmp_hull")
            bm.to_mesh(me_tmp)
            o_tmp = bpy.data.objects.new("_tmp_hull", me_tmp)
            m = o_tmp.modifiers.new("d", "DECIMATE")
            m.ratio = max_verts / len(bm.verts)
            _scene().collection.objects.link(o_tmp)
            try:
                dg = bpy.context.evaluated_depsgraph_get()
                ev2 = o_tmp.evaluated_get(dg)
                m2 = ev2.to_mesh()
                bm.free()
                bm = bmesh.new()
                bm.from_mesh(m2)
                ev2.to_mesh_clear()
            finally:
                bpy.data.objects.remove(o_tmp)
                bpy.data.meshes.remove(me_tmp)
            _hull_only(bm)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    col_ob = bpy.data.objects.new(name, me)
    for c in ob.users_collection:
        c.objects.link(col_ob)
    col_ob.matrix_world = ob.matrix_world.copy()
    col_ob.display_type = "WIRE"
    col_ob.hide_render = True
    if _p(p, "parent", False):
        col_ob.parent = ob
        col_ob.matrix_parent_inverse = ob.matrix_world.inverted()
    return {"collision": name, "kind": kind, "verts": len(me.vertices), "tris": _tri_count(me)}


def _collision_prefixes(name):
    return tuple(f"{k}_{name}_" for k in ("UCX", "UBX", "USP", "UCP"))


def _remove_object_and_mesh(ob):
    data = ob.data
    bpy.data.objects.remove(ob, do_unlink=True)
    if isinstance(data, bpy.types.Mesh) and data.users == 0:
        bpy.data.meshes.remove(data)


def _gather_export_objects(p):
    names = _p(p, "objects")
    col = _p(p, "collection")
    if names:
        objs = _objs(names)
    elif col:
        objs = list(_collection(col).all_objects)
    else:
        objs = [o for o in bpy.context.view_layer.objects if o.select_get()]
    if not objs:
        raise BridgeError("Nothing to export: pass objects, collection, or select something")
    if _p(p, "include_collision", True):
        extra = []
        for ob in objs:
            for o in bpy.context.view_layer.objects:
                if o.name.startswith(_collision_prefixes(ob.name)) and o not in objs and o not in extra:
                    extra.append(o)
        objs += extra
    if _p(p, "include_children", True):
        for ob in list(objs):
            for c in ob.children_recursive:
                if c not in objs:
                    objs.append(c)
    return objs


@contextlib.contextmanager
def _selected_only(objs):
    vl = bpy.context.view_layer
    # objects outside this view layer (other scene, excluded collection) can't be selected or exported
    objs = [o for o in objs if o.name in vl.objects]
    if not objs:
        raise BridgeError("None of the objects to export are in the current view layer")
    prev_sel = [o for o in vl.objects if o.select_get()]
    prev_act = vl.objects.active
    hidden = []
    try:
        for o in vl.objects:
            o.select_set(False)
        for o in objs:
            if o.hide_get():
                o.hide_set(False)
                hidden.append(o)
            o.select_set(True)
        vl.objects.active = objs[0]
        yield
    finally:
        for o in vl.objects:
            o.select_set(False)
        for o in hidden:
            o.hide_set(True)
        for o in prev_sel:
            if o.name in vl.objects:
                o.select_set(True)
        vl.objects.active = prev_act


def _export(objs, path, fmt, p):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    preset = (_p(p, "preset", "unreal") or "").lower()
    apply_mods = bool(_p(p, "apply_modifiers", True))
    _ensure_object_mode()
    with _selected_only(objs), bpy.context.temp_override(**_window_ctx()):
        if fmt == "fbx":
            kw = dict(filepath=path, use_selection=True, use_mesh_modifiers=apply_mods,
                      mesh_smooth_type="FACE", add_leaf_bones=False, use_tspace=True,
                      bake_anim=bool(_p(p, "animation", False)),
                      object_types={"MESH", "EMPTY", "ARMATURE", "OTHER"})
            if preset == "unreal":
                # Verified against UE 5.8's FBX importer: FBX_SCALE_NONE writes UnitScaleFactor=100 and Unreal
                # converts it, so a 1 m cube arrives as 100 uu. (FBX_SCALE_UNITS/ALL arrive 100x too small.)
                kw.update(apply_scale_options="FBX_SCALE_NONE", use_triangles=bool(_p(p, "triangulate", False)))
            elif preset == "unity":
                kw.update(apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y",
                          bake_space_transform=True)
            bpy.ops.export_scene.fbx(**kw)
        elif fmt in ("glb", "gltf"):
            bpy.ops.export_scene.gltf(filepath=path, use_selection=True, export_apply=apply_mods,
                                      export_format="GLB" if fmt == "glb" else "GLTF_SEPARATE",
                                      export_animations=bool(_p(p, "animation", False)))
        elif fmt == "obj":
            bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, apply_modifiers=apply_mods)
        elif fmt == "usd":
            bpy.ops.wm.usd_export(filepath=path, selected_objects_only=True)
        else:
            raise BridgeError("format must be fbx, glb, gltf, obj or usd")
    return {"path": path, "bytes": os.path.getsize(path) if os.path.exists(path) else None,
            "objects": [o.name for o in objs]}


@command("export_asset")
def export_asset(p):
    fmt = _p(p, "format", "fbx").lower()
    path = _p(p, "path", required=True)
    if not os.path.splitext(path)[1]:
        path += "." + fmt
    return _export(_gather_export_objects(p), path, fmt, p)


@command("batch_export")
def batch_export(p):
    """One file per object, centred at the origin, named <prefix><object>.<ext>."""
    fmt = _p(p, "format", "fbx").lower()
    folder = _p(p, "folder", required=True)
    prefix = _p(p, "prefix", "SM_")
    center = bool(_p(p, "center", True))
    roots = _objs(_p(p, "objects")) if _p(p, "objects") else \
        [o for o in (_collection(p["collection"]).all_objects if _p(p, "collection")
                     else [o for o in bpy.context.view_layer.objects if o.select_get()])
         if o.type == "MESH" and not o.name.startswith(("UCX_", "UBX_", "USP_")) and o.parent is None]
    if not roots:
        raise BridgeError("No objects to export")
    results = []
    for ob in roots:
        sub = dict(p, objects=[ob.name])
        objs = _gather_export_objects(sub)
        saved = ob.location.copy()
        coll_saved = {o: o.location.copy() for o in objs if o is not ob and o.parent is None}
        try:
            if center:
                for o, l in coll_saved.items():
                    o.location = l - saved
                ob.location = (0, 0, 0)
            safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in ob.name)
            path = os.path.join(folder, f"{prefix}{safe}.{fmt}")
            results.append(_export(objs, path, fmt, p))
        finally:
            ob.location = saved
            for o, l in coll_saved.items():
                o.location = l
    return {"exported": results}


@command("import_file", undo=True)
def import_file(p):
    path = _p(p, "path", required=True)
    if not os.path.isfile(path):
        raise BridgeError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    with bpy.context.temp_override(**_window_ctx()):
        if ext == ".fbx":
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext in (".glb", ".gltf"):
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".obj":
            bpy.ops.wm.obj_import(filepath=path)
        elif ext in (".usd", ".usda", ".usdc", ".usdz"):
            bpy.ops.wm.usd_import(filepath=path)
        elif ext == ".stl":
            bpy.ops.wm.stl_import(filepath=path)
        elif ext == ".ply":
            bpy.ops.wm.ply_import(filepath=path)
        else:
            raise BridgeError(f"Unsupported file type {ext}")
    new = [o.name for o in bpy.data.objects if o not in before]
    return {"imported": new}


# ------------------------------------------------------------- visuals

@command("viewport_screenshot")
def viewport_screenshot(p):
    """Redraw the user's 3D viewport (same camera, shading and overlays) offscreen and return it."""
    ctx = _window_ctx(need_view3d=True)
    bpy.context.view_layer.update()
    return imaging.capture_viewport(ctx, int(_p(p, "max_size", 1280)))


@command("render_views")
def render_views(p):
    """Offscreen OpenGL renders from preset angles, framed on the given objects (or whole scene).
    Fast (~ms per view), no camera needed, uses the viewport's current shading mode."""
    ctx = _window_ctx(need_view3d=True)
    bpy.context.view_layer.update()
    names = _p(p, "objects")
    objs = _objs(names) if names else [o for o in _scene().objects if o.type in ("MESH", "CURVE", "SURFACE", "META", "FONT") and o.visible_get()]
    views = _p(p, "views", ["iso"])
    bad = [v for v in views if v not in imaging.VIEW_DIRS]
    if bad:
        raise BridgeError(f"Unknown view(s) {bad}. Valid: {', '.join(imaging.VIEW_DIRS)}")
    size = max(64, min(2048, int(_p(p, "size", 512))))
    isolate = bool(_p(p, "isolate", bool(names)))
    shading = _p(p, "shading")
    wire = bool(_p(p, "wireframe", False))
    images = imaging.render_views(ctx, objs, views, size, isolate, shading, wire)
    tris = {o.name: _eval_mesh_stats(o)["tris"] for o in objs if o.type == "MESH"}
    return {"views": images, "tris": tris, "total_tris": sum(tris.values()), "wireframe": wire}


@command("render_image")
def render_image(p):
    """Full engine render (EEVEE/Cycles/Workbench) through the scene camera or an auto camera."""
    sc = _scene()
    r = sc.render
    engine = _p(p, "engine")
    saved = (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
             r.image_settings.file_format, sc.camera)
    saved_samples = sc.cycles.samples if hasattr(sc, "cycles") else None
    tmp_cam = None
    path = os.path.join(tempfile.gettempdir(), f"blender_mcp_render_{os.getpid()}.png")
    hidden = []
    if _p(p, "objects") and _p(p, "isolate", True):
        keep = set(_objs(p["objects"]))
        for o in sc.objects:
            if o not in keep and not o.hide_render and o.type not in ("LIGHT", "CAMERA"):
                o.hide_render = True
                hidden.append(o)
    try:
        if engine:
            r.engine = engine.upper()
        r.resolution_x = max(16, min(8192, int(_p(p, "width", 960))))
        r.resolution_y = max(16, min(8192, int(_p(p, "height", 540))))
        r.resolution_percentage = 100
        r.image_settings.file_format = "PNG"
        r.filepath = path
        if r.engine == "CYCLES" and _p(p, "samples"):
            sc.cycles.samples = int(p["samples"])
        if _p(p, "auto_camera", sc.camera is None):
            names = _p(p, "objects")
            objs = _objs(names) if names else [o for o in sc.objects if o.type == "MESH" and o.visible_get()]
            tmp_cam = imaging.make_framing_camera(sc, objs, _p(p, "view", "iso"), r.resolution_x / r.resolution_y)
            sc.camera = tmp_cam
        with bpy.context.temp_override(**_window_ctx()):
            bpy.ops.render.render(write_still=True)
        return imaging.file_result(path, int(_p(p, "max_size", 1600)))
    finally:
        (r.engine, r.resolution_x, r.resolution_y, r.resolution_percentage, r.filepath,
         r.image_settings.file_format, sc.camera) = saved
        if saved_samples is not None:
            sc.cycles.samples = saved_samples
        for o in hidden:
            o.hide_render = False
        if tmp_cam is not None:
            cd = tmp_cam.data
            bpy.data.objects.remove(tmp_cam, do_unlink=True)
            bpy.data.cameras.remove(cd)


@command("focus_view")
def focus_view(p):
    """Frame objects in the user's 3D viewport and optionally switch shading/view angle."""
    ctx = _window_ctx(need_view3d=True)
    names = _p(p, "objects")
    space = ctx["area"].spaces.active
    if _p(p, "shading"):
        space.shading.type = p["shading"].upper()
    if names:
        objs = _objs(names)
        with _selected_only(objs), bpy.context.temp_override(**ctx):
            if _p(p, "view"):
                bpy.ops.view3d.view_axis(type=p["view"].upper())
            bpy.ops.view3d.view_selected()
    else:
        with bpy.context.temp_override(**ctx):
            if _p(p, "view"):
                bpy.ops.view3d.view_axis(type=p["view"].upper())
            bpy.ops.view3d.view_all()
    return {"ok": True}


# ----------------------------------------------------------- file / undo

@command("undo")
def undo(p):
    steps = int(_p(p, "steps", 1))
    with bpy.context.temp_override(**_window_ctx()):
        for _ in range(steps):
            bpy.ops.ed.undo()
    return {"undone": steps}


@command("redo")
def redo(p):
    steps = int(_p(p, "steps", 1))
    with bpy.context.temp_override(**_window_ctx()):
        for _ in range(steps):
            bpy.ops.ed.redo()
    return {"redone": steps}


@command("save_file")
def save_file(p):
    path = _p(p, "path")
    with bpy.context.temp_override(**_window_ctx()):
        if path:
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=bool(_p(p, "copy", False)))
        elif bpy.data.filepath:
            bpy.ops.wm.save_mainfile()
        else:
            raise BridgeError("File has never been saved; pass a path")
    return {"file": path or bpy.data.filepath}


# ------------------------------------------------------ batch + scripting

@command("batch")
def batch(p):
    """Run several commands in one main-thread tick; one undo step for the lot."""
    steps = _p(p, "commands", required=True)
    stop = bool(_p(p, "stop_on_error", True))
    if not isinstance(steps, list):
        raise BridgeError("'commands' must be a list of {cmd, params} objects")
    # validate everything before running anything, so a typo can't leave half a batch applied
    for i, s in enumerate(steps):
        if not isinstance(s, dict) or not isinstance(s.get("cmd"), str):
            raise BridgeError(f"commands[{i}] must be an object with a string 'cmd'")
        if s["cmd"] in ("batch", "undo", "redo"):
            raise BridgeError(f"commands[{i}]: '{s['cmd']}' is not allowed inside batch")
        if s["cmd"] not in _COMMANDS:
            raise BridgeError(f"commands[{i}]: unknown command '{s['cmd']}'")
        if not isinstance(s.get("params") or {}, dict):
            raise BridgeError(f"commands[{i}].params must be an object")
    results = []
    for s in steps:
        fn, _ = _COMMANDS[s["cmd"]]
        try:
            results.append({"ok": True, "result": fn(s.get("params") or {})})
        except Exception as ex:
            results.append({"ok": False, "error": f"{type(ex).__name__}: {ex}"})
            if stop:
                break
    _undo_push(_p(p, "undo_label", "MCP: batch"))
    return {"results": results, "completed": sum(1 for r in results if r["ok"]), "total": len(steps)}


@command("execute_python", undo=True)
def execute_python(p):
    """Escape hatch. Set a variable named `result` to return data; stdout is captured."""
    code = _p(p, "code", required=True)
    ns = {"bpy": bpy, "bmesh": bmesh, "Vector": Vector, "Matrix": Matrix, "Euler": Euler, "math": math, "result": None}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(compile(code, "<mcp>", "exec"), ns)
    res = ns.get("result")
    try:
        import json
        json.dumps(res)
    except (TypeError, ValueError):
        res = repr(res)
    return {"result": res, "stdout": buf.getvalue()[-20000:]}


@command("list_commands")
def list_commands(p):
    return {"commands": sorted(_COMMANDS)}


# Extra command modules register themselves through @command on import.
from . import animation, baking, editmesh, geonodes, layout, modeling, palette, review, unreal_export, user_context  # noqa: E402,F401
