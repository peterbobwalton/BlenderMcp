"""Geometry Nodes: list node groups (local + Blender's Essentials library), add them as modifiers,
and set their inputs by display name."""

import os

import bpy

from .handlers import BridgeError, _jsonable, _obj, _p, command

_ESSENTIALS = ("nodes/geometry_nodes_essentials.blend", "nodes/geometry_nodes_dynamics_assets.blend",
               "nodes/procedural_hair_node_assets.blend")
_LOOKUP = {"NodeSocketObject": "objects", "NodeSocketCollection": "collections",
           "NodeSocketMaterial": "materials", "NodeSocketImage": "images", "NodeSocketTexture": "textures"}


def _essentials_files():
    base = bpy.utils.system_resource("DATAFILES", path="assets")
    return [os.path.join(base, f) for f in _ESSENTIALS if base and os.path.exists(os.path.join(base, f))]


def _inputs(ng):
    return [i for i in ng.interface.items_tree
            if getattr(i, "item_type", "") == "SOCKET" and i.in_out == "INPUT" and i.socket_type != "NodeSocketGeometry"]


def _slot(mod, item):
    """Blender 5.x keeps modifier inputs in mod.properties.inputs.<identifier> (.value/.type/.attribute_name).
    Returns None on 4.x, where they are ID properties: mod[identifier]."""
    props = getattr(mod, "properties", None)
    inputs = getattr(props, "inputs", None) if props is not None else None
    return getattr(inputs, item.identifier, None) if inputs is not None else None


def _get_value(mod, item):
    slot = _slot(mod, item)
    if slot is not None:
        v = slot.value
        if getattr(slot, "type", "VALUE") == "ATTRIBUTE":
            return {"attribute": slot.attribute_name}
    else:
        if item.identifier not in mod.keys():
            return None
        v = mod[item.identifier]
    return getattr(v, "name", None) if isinstance(v, bpy.types.ID) else _jsonable(v)


def _describe(ng, mod=None):
    out = []
    for i in _inputs(ng):
        d = {"name": i.name, "type": i.socket_type.replace("NodeSocket", "")}
        for attr, key in (("default_value", "default"), ("min_value", "min"), ("max_value", "max")):
            if hasattr(i, attr):
                v = getattr(i, attr)
                d[key] = v.name if isinstance(v, bpy.types.ID) else _jsonable(v)
        if mod is not None:
            d["value"] = _get_value(mod, i)
            slot = _slot(mod, i)
            if slot is not None and slot.bl_rna.properties["value"].type == "ENUM":
                d["options"] = [e.identifier for e in slot.bl_rna.properties["value"].enum_items]
        if getattr(i, "description", ""):
            d["description"] = i.description
        out.append(d)
    return out


@command("list_node_groups")
def list_node_groups(p):
    """Geometry node groups in this file, plus (optionally) the ones bundled with Blender's Essentials library."""
    local = [{"name": g.name, "inputs": _describe(g)} for g in bpy.data.node_groups if g.bl_idname == "GeometryNodeTree"]
    out = {"local": local}
    if _p(p, "include_essentials", True):
        ess = {}
        for f in _essentials_files():
            with bpy.data.libraries.load(f, assets_only=True) as (src, _):
                ess[os.path.basename(f)] = list(src.node_groups)
        out["essentials"] = ess
        out["note"] = "Essentials groups are appended automatically by add_geometry_nodes; their inputs are listed after adding."
    return out


def _get_group(name):
    ng = bpy.data.node_groups.get(name)
    if ng is not None:
        if ng.bl_idname != "GeometryNodeTree":
            raise BridgeError(f"'{name}' is a {ng.bl_idname}, not a geometry node group")
        return ng, False
    for f in _essentials_files():
        wanted = None
        with bpy.data.libraries.load(f, link=False, assets_only=True) as (src, dst):
            if name in src.node_groups:
                wanted = [name]
                dst.node_groups = wanted
        if wanted:
            # Blender replaces the names in this list with the appended IDs (which may be renamed .001)
            ng = wanted[0]
            if isinstance(ng, bpy.types.NodeTree):
                return ng, True
    raise BridgeError(f"No geometry node group '{name}' in this file or Blender's Essentials (see list_node_groups)")


_KIND = {  # which socket types suit a JSON value, to disambiguate inputs that share a display name
    list: ("Vector", "Color", "Rotation", "Matrix"), bool: ("Bool",), int: ("Int", "Float"),
    float: ("Float", "Int"), str: ("Menu", "String", "Object", "Collection", "Material", "Image", "Texture"),
    dict: (),
}


def _pick(items, val):
    if len(items) == 1:
        return items[0]
    kinds = _KIND.get(type(val), ())
    for kind in kinds:  # kinds are in preference order
        for it in items:
            if it.socket_type.startswith("NodeSocket" + kind):
                return it
    return items[0]


def _set_inputs(ob, mod, values):
    ng = mod.node_group
    by_name, by_id = {}, {}
    for i in _inputs(ng):
        by_name.setdefault(i.name.lower(), []).append(i)
        by_id[i.identifier] = i
    warnings = []
    for key, val in (values or {}).items():
        item = by_id.get(key) or (_pick(by_name[str(key).lower()], val) if str(key).lower() in by_name else None)
        if item is None:
            warnings.append(f"'{key}' is not an input of {ng.name}; inputs: {sorted({i.name for i in _inputs(ng)})}")
            continue
        try:
            st = item.socket_type
            coll = _LOOKUP.get(st)
            if isinstance(val, dict) and "attribute" in val:
                slot = _slot(mod, item)
                if slot is None:
                    mod[item.identifier + "_use_attribute"] = True
                    mod[item.identifier + "_attribute_name"] = str(val["attribute"])
                else:
                    slot.type = "ATTRIBUTE"
                    slot.attribute_name = str(val["attribute"])
                continue
            if coll and isinstance(val, str):
                pool = getattr(bpy.data, coll)
                if val not in pool:
                    raise BridgeError(f"{coll[:-1]} '{val}' not found")
                val = pool[val]
            elif st == "NodeSocketBool":
                val = bool(val)
            elif st == "NodeSocketInt":
                val = int(val)
            elif st == "NodeSocketFloat" and not isinstance(val, list):
                val = float(val)
            slot = _slot(mod, item)
            if slot is not None:
                slot.type = "VALUE" if "VALUE" in {e.identifier for e in slot.bl_rna.properties["type"].enum_items} else slot.type
                slot.value = val
            else:
                mod[item.identifier] = val
        except Exception as ex:
            warnings.append(f"{item.name}: {ex}")
    ob.update_tag()
    bpy.context.view_layer.update()
    return warnings


@command("add_geometry_nodes", undo=True)
def add_geometry_nodes(p):
    """Add a Geometry Nodes modifier using a node group (local, or appended from Essentials) and set inputs."""
    ob = _obj(_p(p, "object", required=True))
    ng, appended = _get_group(_p(p, "group", required=True))
    mod = ob.modifiers.new(_p(p, "name", ng.name), "NODES")
    if mod is None:
        raise BridgeError(f"Cannot add Geometry Nodes to a {ob.type}")
    mod.node_group = ng
    warnings = _set_inputs(ob, mod, _p(p, "inputs"))
    out = {"object": ob.name, "modifier": mod.name, "group": ng.name, "appended_from_essentials": appended,
           "inputs": _describe(ng, mod)}
    if warnings:
        out["warnings"] = warnings
    return out


@command("set_geometry_nodes_inputs", undo=True)
def set_geometry_nodes_inputs(p):
    ob = _obj(_p(p, "object", required=True))
    name = _p(p, "modifier")
    mods = [m for m in ob.modifiers if m.type == "NODES" and (not name or m.name == name)]
    if not mods:
        raise BridgeError(f"No Geometry Nodes modifier{' ' + repr(name) if name else ''} on {ob.name}")
    mod = mods[0]
    warnings = _set_inputs(ob, mod, _p(p, "inputs", required=True))
    out = {"object": ob.name, "modifier": mod.name, "inputs": _describe(mod.node_group, mod)}
    if warnings:
        out["warnings"] = warnings
    return out
