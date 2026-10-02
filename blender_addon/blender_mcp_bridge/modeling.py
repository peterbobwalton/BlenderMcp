"""Object-level modelling ops: join, separate, boolean."""

import bpy

from .handlers import (BridgeError, _bake_evaluated_single, _edit_mode, _ensure_object_mode, _eval_mesh_stats,
                       _mesh_obj, _objs, _p, _summary, _tri_count, _window_ctx, command)


@command("join_objects", undo=True)
def join_objects(p):
    """Merge several meshes into one object (the first, or 'target'); keeps materials and UVs."""
    objs = [o for o in _objs(_p(p, "objects", required=True))]
    if len(objs) < 2:
        raise BridgeError("Give at least two objects to join")
    bad = [o.name for o in objs if o.type != "MESH"]
    if bad:
        raise BridgeError(f"Only meshes can be joined; not meshes: {bad}")
    target = _mesh_obj(_p(p, "target")) if _p(p, "target") else objs[0]
    if target not in objs:
        objs.insert(0, target)
    _ensure_object_mode()
    if target.data.users > 1:
        target.data = target.data.copy()
    hidden = [o for o in objs if o.hide_get()]
    for o in hidden:
        o.hide_set(False)
    names = [o.name for o in objs]
    with bpy.context.temp_override(**_window_ctx(), active_object=target, object=target,
                                   selected_objects=objs, selected_editable_objects=objs):
        bpy.ops.object.join()
    if _p(p, "new_name"):
        target.name = p["new_name"]
        target.data.name = target.name
    out = _summary(target)
    out["joined"] = names
    return out


@command("separate", undo=True)
def separate(p):
    """Split a mesh into objects by loose parts or by material."""
    ob = _mesh_obj(_p(p, "object", required=True))
    mode = _p(p, "by", "loose").upper()
    if mode not in ("LOOSE", "MATERIAL"):
        raise BridgeError("by must be loose or material")
    before = set(bpy.data.objects)
    with _edit_mode(ob):
        bpy.ops.mesh.separate(type=mode)
    new = [o for o in bpy.data.objects if o not in before]
    return {"source": ob.name, "created": [o.name for o in new], "parts": 1 + len(new)}


@command("boolean", undo=True)
def boolean(p):
    """Union / difference / intersect with one or more cutter objects, applied by default."""
    ob = _mesh_obj(_p(p, "object", required=True))
    cutters = _objs(_p(p, "cutters", required=True))
    op = _p(p, "operation", "difference").upper()
    if op not in ("DIFFERENCE", "UNION", "INTERSECT"):
        raise BridgeError("operation must be difference, union or intersect")
    solver = _p(p, "solver", "exact").upper()
    apply = bool(_p(p, "apply", True))
    after = _p(p, "cutter_action", "hide").lower()
    if after not in ("hide", "delete", "keep"):
        raise BridgeError("cutter_action must be hide, delete or keep")
    before = _eval_mesh_stats(ob)["tris"]
    mods = []
    for c in cutters:
        if c == ob or c.type != "MESH":
            raise BridgeError(f"'{c.name}' cannot be a cutter (must be another mesh)")
        m = ob.modifiers.new(f"Bool_{c.name}", "BOOLEAN")
        m.operation = op
        m.object = c
        try:
            m.solver = solver
        except TypeError:
            ob.modifiers.remove(m)
            raise BridgeError("solver must be exact, float or manifold")
        mods.append(m)
    if apply:
        for m in mods:
            _bake_evaluated_single(ob, m)
        for c in cutters:
            if after == "delete":
                data = c.data
                bpy.data.objects.remove(c, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.meshes.remove(data)
            elif after == "hide":
                c.hide_set(True)
                c.hide_render = True
    elif after == "hide":
        for c in cutters:
            c.display_type = "WIRE"
            c.hide_render = True
    out = _summary(ob)
    out.update({"operation": op, "tris_before": before, "tris_after": _eval_mesh_stats(ob)["tris"], "applied": apply})
    if apply and _tri_count(ob.data) == 0:
        out["warning"] = "Result is empty - check the cutter overlaps the object and the operation"
    return out
