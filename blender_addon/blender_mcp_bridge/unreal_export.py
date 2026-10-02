"""Unreal Engine hand-off: sockets, naming conventions, one-FBX-per-asset export with LOD groups."""

import os
import re

import bpy
from mathutils import Euler, Matrix, Vector

from .handlers import (BridgeError, _collision_prefixes, _export, _obj, _objs, _p, _remove_object_and_mesh,
                       _scene, _tri_count, _vec, command)

_PREFIX = {"MESH": "SM_", "MATERIAL": "M_", "TEXTURE": "T_"}
_LOD_RE = re.compile(r"^(?P<base>.+)_LOD(?P<i>\d+)$")
_SKIP_IMAGES = {"Render Result", "Viewer Node"}


def _base_name(name):
    m = _LOD_RE.match(name)
    return m.group("base") if m else name


def _lods_for(ob):
    """[(index, object)] for <base>_LOD1.. objects; LOD0 is the source object itself."""
    base = _base_name(ob.name)
    found = {}
    for o in bpy.data.objects:
        m = _LOD_RE.match(o.name)
        if o.type == "MESH" and m and m.group("base") == base and int(m.group("i")) > 0:
            found[int(m.group("i"))] = o
    return [found[i] for i in sorted(found)]


@command("add_socket", undo=True)
def add_socket(p):
    """Empty named SOCKET_<name>, parented to the mesh; Unreal imports it as a static mesh socket."""
    ob = _obj(_p(p, "object", required=True))
    name = _p(p, "name", required=True)
    sock_name = name if name.startswith("SOCKET_") else f"SOCKET_{name}"
    old = bpy.data.objects.get(sock_name)
    if old is not None:
        bpy.data.objects.remove(old, do_unlink=True)
    s = bpy.data.objects.new(sock_name, None)
    s.empty_display_type = "ARROWS"
    s.empty_display_size = float(_p(p, "size", 0.25))
    for c in ob.users_collection:
        c.objects.link(s)
    loc = Vector(_vec(_p(p, "location", [0, 0, 0]), name="location"))
    rot = [v * 3.141592653589793 / 180 for v in _vec(_p(p, "rotation", [0, 0, 0]), name="rotation")]
    local = Matrix.Translation(loc) @ Euler(rot).to_matrix().to_4x4()
    if _p(p, "space", "local") == "world":
        world = local
    else:
        world = ob.matrix_world @ local
    s.parent = ob
    s.matrix_parent_inverse = ob.matrix_world.inverted()
    s.matrix_world = world
    return {"socket": s.name, "parent": ob.name, "world_location": [round(v, 4) for v in s.matrix_world.translation]}


@command("check_naming", undo=True)
def check_naming(p):
    """Unreal naming conventions: SM_ meshes, M_ materials, T_ textures. fix=true renames."""
    fix = bool(_p(p, "fix", False))
    names = _p(p, "objects")
    objs = _objs(names) if names else [o for o in _scene().objects if o.type == "MESH"]
    issues, renamed = [], []

    def want(kind, idb, label):
        prefix = _PREFIX[kind]
        if idb.name.startswith(prefix):
            return
        new = prefix + re.sub(r"^(SM|M|T|S|MI)_", "", idb.name)
        issues.append({"kind": label, "name": idb.name, "suggested": new})
        if fix:
            old = idb.name
            idb.name = new
            renamed.append([old, idb.name])

    mats, imgs = set(), set()
    for ob in objs:
        if ob.type != "MESH" or ob.name.startswith(("UCX_", "UBX_", "USP_", "UCP_", "SOCKET_")):
            continue
        if not _LOD_RE.match(ob.name):
            base_old = ob.name
            want("MESH", ob, "mesh")
            if fix and ob.name != base_old:
                # keep collision / LOD companions matched to the renamed mesh
                for o in bpy.data.objects:
                    for pre in _collision_prefixes(base_old):
                        if o.name.startswith(pre):
                            o.name = o.name.replace(f"_{base_old}_", f"_{ob.name}_", 1)
                    m = _LOD_RE.match(o.name)
                    if m and m.group("base") == base_old:
                        o.name = f"{ob.name}_LOD{m.group('i')}"
        for slot in ob.material_slots:
            if slot.material:
                mats.add(slot.material)
    for m in mats:
        want("MATERIAL", m, "material")
        if m.node_tree:
            for n in m.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image and n.image.name not in _SKIP_IMAGES:
                    imgs.add(n.image)
    for img in imgs:
        want("TEXTURE", img, "texture")
    return {"ok": not issues or fix, "issues": issues, "renamed": renamed}


@command("export_for_unreal")
def export_for_unreal(p):
    """One FBX per asset: mesh + its _LODn meshes as an FBX LOD group + UCX_/UBX_/USP_ collision + SOCKET_ empties,
    centred at the origin. Returns the file path and the arguments for Unreal's static-mesh import."""
    ob = _obj(_p(p, "object", required=True))
    if ob.type != "MESH":
        raise BridgeError(f"'{ob.name}' is not a mesh")
    if _LOD_RE.match(ob.name):
        ob = _obj(_base_name(ob.name))  # passed a LOD: export its asset
    folder = _p(p, "folder") or os.path.join(bpy.app.tempdir or os.environ.get("TEMP", "."), "BlenderMcp_Unreal")
    asset = _p(p, "asset_name") or (ob.name if ob.name.startswith("SM_") else f"SM_{ob.name}")
    safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in asset)
    path = os.path.join(folder, safe + ".fbx")
    use_lods = bool(_p(p, "lods", True))
    lods = _lods_for(ob) if use_lods else []
    center = bool(_p(p, "center", True))

    # gather companions
    collision = [o for o in bpy.data.objects if o.name.startswith(_collision_prefixes(ob.name))]
    extras = [c for c in ob.children_recursive if c not in collision]
    objs = [ob] + lods + collision + extras

    # temporary state
    saved_parent = {o: (o.parent, o.matrix_parent_inverse.copy(), o.matrix_world.copy()) for o in [ob] + lods}
    saved_world = {o: o.matrix_world.copy() for o in collision + lods + [ob]}
    group = None
    offset = Matrix.Translation(-ob.matrix_world.translation) if center else Matrix.Identity(4)
    try:
        for o in [ob] + [c for c in collision if c.parent is None]:
            o.matrix_world = offset @ saved_world[o]
        for o in lods:
            # LODs may sit side by side for comparison (generate_lods spacing); in the file they must overlap LOD0
            o.matrix_world = offset @ saved_world[ob]
        if lods:
            group = bpy.data.objects.new(f"{safe}_LODGroup", None)
            group["fbx_type"] = "LodGroup"  # Blender's FBX exporter writes this empty as an FbxLODGroup
            _scene().collection.objects.link(group)
            for o in [ob] + lods:  # child order = LOD order
                mw = o.matrix_world.copy()
                o.parent = group
                o.matrix_parent_inverse = Matrix.Identity(4)
                o.matrix_world = mw
            objs.insert(0, group)
        bpy.context.view_layer.update()
        # triangulate by default: n-gons (e.g. from booleans) can't get exported tangents, which breaks normal maps
        result = _export(objs, path, "fbx", dict(p, preset="unreal", triangulate=bool(_p(p, "triangulate", True))))
    finally:
        if group is not None:
            for o in [ob] + lods:
                par, inv, _ = saved_parent[o]
                o.parent = par
                o.matrix_parent_inverse = inv
            bpy.data.objects.remove(group, do_unlink=True)
        for o, mw in saved_world.items():
            o.matrix_world = mw
        bpy.context.view_layer.update()

    folder_ue = _p(p, "unreal_folder", "/Game/Meshes")
    return {
        "path": result["path"],
        "bytes": result["bytes"],
        "asset_name": safe,
        "lods": [ob.name] + [o.name for o in lods],
        "lod_tris": [_tri_count(o.data) for o in [ob] + lods],
        "collision": [o.name for o in collision],
        "sockets": [o.name for o in extras if o.name.startswith("SOCKET_")],
        "unreal_import": {
            "toolset": "editor_toolset.toolsets.static_mesh.StaticMeshTools",
            "tool": "import_file",
            # combine_meshes must stay False: True flattens every LOD into one LOD0 mesh.
            "arguments": {"folder_path": folder_ue, "asset_name": safe, "source_file": result["path"],
                          "import_materials": bool(_p(p, "import_materials", True)),
                          "import_textures": bool(_p(p, "import_materials", True)), "combine_meshes": False},
        },
        "notes": ([f"Epic's StaticMeshTools.import_file imports LOD0 only (it has no 'Import Mesh LODs' option). "
                   f"To use these exact LODs, import the file through the editor's FBX dialog with 'Import Mesh LODs' "
                   f"ticked; or after import_file call StaticMeshTools.generate_lods with triangle_percents "
                   f"{[round(_tri_count(o.data) / max(1, _tri_count(ob.data)), 3) for o in lods]}."] if lods else []),
    }
