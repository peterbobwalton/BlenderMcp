"""Texture baking (Cycles): high-poly -> low-poly normal/AO/colour/roughness maps, saved and wired up."""

import os

import bpy

from .handlers import BridgeError, _ensure_object_mode, _mesh_obj, _objs, _p, _principled, _window_ctx, command

# map name -> (Cycles bake type, colour space, Principled input it feeds or None)
_MAPS = {
    "normal": ("NORMAL", "Non-Color", "Normal"),
    "ao": ("AO", "Non-Color", None),
    "color": ("DIFFUSE", "sRGB", "Base Color"),
    "roughness": ("ROUGHNESS", "Non-Color", "Roughness"),
    "emission": ("EMIT", "sRGB", "Emission Color"),
}
_SUFFIX = {"normal": "N", "ao": "AO", "color": "BC", "roughness": "R", "emission": "E"}


@command("bake_maps", undo=True)
def bake_maps(p):
    low = _mesh_obj(_p(p, "low", required=True))
    highs = _objs(_p(p, "high")) if _p(p, "high") else []
    maps = [m.lower() for m in _p(p, "maps", ["normal", "ao"])]
    bad = [m for m in maps if m not in _MAPS]
    if bad:
        raise BridgeError(f"Unknown map(s) {bad}. Valid: {', '.join(_MAPS)}")
    if "normal" in maps and not highs:
        raise BridgeError("A normal bake needs 'high' (the detailed source mesh)")
    if not low.data.uv_layers:
        raise BridgeError(f"'{low.name}' has no UVs - run uv_unwrap first")
    for h in highs:
        if h == low or h.type != "MESH":
            raise BridgeError(f"'{h.name}' cannot be a high-poly source")
    size = max(64, min(8192, int(_p(p, "size", 1024))))
    folder = _p(p, "folder") or (os.path.join(os.path.dirname(bpy.data.filepath), "textures")
                                 if bpy.data.filepath else os.path.join(bpy.app.tempdir, "bakes"))
    os.makedirs(folder, exist_ok=True)
    connect = bool(_p(p, "connect", True))
    base = low.name[3:] if low.name.startswith("SM_") else low.name

    _ensure_object_mode()
    if not low.material_slots or all(s.material is None for s in low.material_slots):
        mat = bpy.data.materials.new(f"M_{base}")
        low.data.materials.append(mat)
    mats = list({s.material for s in low.material_slots if s.material})

    sc = bpy.context.scene
    vl = bpy.context.view_layer
    saved = {"engine": sc.render.engine, "samples": sc.cycles.samples,
             "sel": [o for o in vl.objects if o.select_get()], "active": vl.objects.active}
    unhidden = [o for o in highs + [low] if o.hide_get()]
    hidden_render = [o for o in highs + [low] if o.hide_render]
    # Everything else in the scene would cast shadows/occlusion onto the bake (overlapping copies, floors...):
    # hide it from render while baking unless isolate=false.
    isolated = []
    if _p(p, "isolate", True):
        keep = set(highs + [low])
        isolated = [o for o in sc.objects if o not in keep and not o.hide_render and o.type not in ("CAMERA",)]
    ray_vis = {}
    results = []
    try:
        for o in unhidden:
            o.hide_set(False)
        for o in hidden_render:
            o.hide_render = False  # bake skips render-hidden objects
        for o in isolated:
            o.hide_render = True
        if highs:
            # the low-poly cage sits inside/over the high-poly surface; it must not block AO/shadow rays
            for attr in ("visible_diffuse", "visible_glossy", "visible_shadow", "visible_transmission"):
                ray_vis[attr] = getattr(low, attr)
                setattr(low, attr, False)
        sc.render.engine = "CYCLES"
        bake = sc.render.bake
        for name in maps:
            btype, cs, socket = _MAPS[name]
            img_name = f"T_{base}_{_SUFFIX[name]}"
            old = bpy.data.images.get(img_name)
            if old is not None:
                bpy.data.images.remove(old)
            img = bpy.data.images.new(img_name, size, size, alpha=False, float_buffer=False)
            img.colorspace_settings.name = cs
            if name == "normal":
                img.generated_color = (0.5, 0.5, 1.0, 1.0)
            nodes = []
            for m in mats:
                nt = m.node_tree
                n = nt.nodes.new("ShaderNodeTexImage")
                n.image = img
                n.label = img_name
                for other in nt.nodes:
                    other.select = False
                n.select = True
                nt.nodes.active = n
                nodes.append((m, n))
            sc.cycles.samples = int(_p(p, "samples", 64 if name == "ao" else 1))
            kwargs = dict(type=btype, margin=int(_p(p, "margin", 8)), use_clear=True, target="IMAGE_TEXTURES")
            if btype == "NORMAL":
                kwargs["normal_space"] = "TANGENT"
            if btype == "DIFFUSE":
                kwargs["pass_filter"] = {"COLOR"}
            if highs:
                kwargs.update(use_selected_to_active=True, cage_extrusion=float(_p(p, "cage_extrusion", 0.05)),
                              max_ray_distance=float(_p(p, "max_ray_distance", 0.0)))
            for o in vl.objects:
                o.select_set(False)
            for o in highs + [low]:
                o.select_set(True)
            vl.objects.active = low
            sel = highs + [low]
            with bpy.context.temp_override(**_window_ctx(), active_object=low, object=low,
                                           selected_objects=sel, selected_editable_objects=sel):
                bpy.ops.object.bake(**kwargs)
            path = os.path.join(folder, img_name + ".png")
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
            img.source = "FILE"  # now backed by the saved file
            for m, n in nodes:
                bsdf = _principled(m)
                n.location = (bsdf.location.x - 600, bsdf.location.y - 300 * (list(_MAPS).index(name)))
                if connect and socket:
                    if name == "normal":
                        nm = m.node_tree.nodes.new("ShaderNodeNormalMap")
                        nm.location = (bsdf.location.x - 250, n.location.y)
                        m.node_tree.links.new(n.outputs["Color"], nm.inputs["Color"])
                        m.node_tree.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
                    else:
                        m.node_tree.links.new(n.outputs["Color"], bsdf.inputs[socket])
            results.append({"map": name, "image": img_name, "path": path, "connected": bool(connect and socket)})
    finally:
        for attr, v in ray_vis.items():
            setattr(low, attr, v)
        for o in isolated:
            o.hide_render = False
        sc.render.engine = saved["engine"]
        sc.cycles.samples = saved["samples"]
        for o in hidden_render:
            o.hide_render = True
        for o in vl.objects:
            o.select_set(False)
        for o in saved["sel"]:
            if o.name in vl.objects:
                o.select_set(True)
        vl.objects.active = saved["active"]
        for o in unhidden:
            o.hide_set(True)
    out = {"low": low.name, "high": [h.name for h in highs], "size": size, "maps": results}
    if "ao" in maps:
        out["note"] = "AO is saved but not wired (Principled BSDF has no AO input); Unreal uses it in the material."
    return out
