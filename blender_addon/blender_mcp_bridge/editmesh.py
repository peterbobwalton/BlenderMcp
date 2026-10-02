"""Component-level modelling (extrude, inset, bevel, loop cuts, delete) done with bmesh -
no edit-mode/selection state, so it is fast, undoable and context-free.

Faces are chosen with a *selector* dict (all keys optional, combined with AND):
  {"all": true}                         every face (default)
  {"side": "top"}                       top|bottom|front|back|left|right  (world axes; front = -Y)
  {"normal": [0,0,1], "angle_deg": 15}  faces whose world normal is within angle of a direction
  {"material": "M_Metal"}               faces using a material slot
  {"indices": [0, 4, 7]}                explicit face indices
  {"region": {"min": [x,y,z], "max": [x,y,z]}}   face centres inside a world-space box
  {"largest": 3}                        keep only the N largest of the matches
"""

import math

import bmesh
import bpy
from mathutils import Vector

from .handlers import BridgeError, _ensure_object_mode, _mesh_obj, _p, _summary, _tri_count, command

_SIDES = {"top": (0, 0, 1), "bottom": (0, 0, -1), "front": (0, -1, 0), "back": (0, 1, 0),
          "right": (1, 0, 0), "left": (-1, 0, 0)}


def _world_normal(ob, f):
    return (ob.matrix_world.to_3x3().inverted_safe().transposed() @ f.normal).normalized()


def select_faces(ob, bm, sel):
    sel = sel or {"all": True}
    if not isinstance(sel, dict):
        raise BridgeError("selector must be an object, e.g. {\"side\": \"top\"}")
    bm.faces.ensure_lookup_table()
    faces = list(bm.faces)
    if "indices" in sel:
        idx = set(int(i) for i in sel["indices"])
        faces = [f for f in faces if f.index in idx]
    direction = sel.get("normal") or (_SIDES.get(str(sel.get("side", "")).lower()) if sel.get("side") else None)
    if sel.get("side") and direction is None:
        raise BridgeError(f"side must be one of {', '.join(_SIDES)}")
    if direction is not None:
        d = Vector(direction).normalized()
        lim = math.cos(math.radians(float(sel.get("angle_deg", 15))))
        faces = [f for f in faces if _world_normal(ob, f).dot(d) >= lim]
    if "material" in sel:
        names = sel["material"] if isinstance(sel["material"], list) else [sel["material"]]
        slots = {i for i, s in enumerate(ob.material_slots) if s.material and s.material.name in names}
        if not slots:
            raise BridgeError(f"{ob.name} has no material slot using {names}")
        faces = [f for f in faces if f.material_index in slots]
    if "region" in sel:
        mn, mx = Vector(sel["region"]["min"]), Vector(sel["region"]["max"])
        mw = ob.matrix_world
        faces = [f for f in faces
                 if all(mn[i] <= (mw @ f.calc_center_median())[i] <= mx[i] for i in range(3))]
    if "largest" in sel:
        faces = sorted(faces, key=lambda f: f.calc_area(), reverse=True)[:int(sel["largest"])]
    if not faces:
        raise BridgeError(f"Selector {sel} matched no faces on {ob.name}")
    return faces


class _Edit:
    """bmesh round-trip on an object's mesh (object mode, single user)."""

    def __init__(self, name):
        _ensure_object_mode()
        self.ob = _mesh_obj(name)
        if self.ob.data.users > 1:
            self.ob.data = self.ob.data.copy()
        self.before = _tri_count(self.ob.data)

    def __enter__(self):
        self.bm = bmesh.new()
        self.bm.from_mesh(self.ob.data)
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.bm.normal_update()
            self.bm.to_mesh(self.ob.data)
            self.ob.data.update()
        self.bm.free()
        return False

    def result(self, **extra):
        out = _summary(self.ob)
        out.update({"tris_before": self.before, "tris_after": _tri_count(self.ob.data)}, **extra)
        return out

    def local_vec(self, world_vec):
        return self.ob.matrix_world.to_3x3().inverted_safe() @ Vector(world_vec)


@command("face_info")
def face_info(p):
    """Preview what a selector matches (count, total area, centre) before editing. Read-only."""
    ob = _mesh_obj(_p(p, "object", required=True))
    bm = bmesh.new()
    try:
        bm.from_mesh(ob.data)
        faces = select_faces(ob, bm, _p(p, "selector"))
        mw = ob.matrix_world
        centre = sum((mw @ f.calc_center_median() for f in faces), Vector()) / len(faces)
        return {"object": ob.name, "faces": len(faces), "total_faces": len(bm.faces),
                "area_m2": round(sum(f.calc_area() for f in faces), 4),
                "centre": [round(v, 4) for v in centre],
                "indices": [f.index for f in faces][:200]}
    finally:
        bm.free()


@command("extrude_faces", undo=True)
def extrude_faces(p):
    """Extrude faces along their normal by `distance` metres (negative pushes in)."""
    dist = float(_p(p, "distance", 0.2))
    individual = bool(_p(p, "individual", False))
    with _Edit(_p(p, "object", required=True)) as e:
        faces = select_faces(e.ob, e.bm, _p(p, "selector"))
        if individual:
            res = bmesh.ops.extrude_discrete_faces(e.bm, faces=faces)
            for f in res["faces"]:
                bmesh.ops.translate(e.bm, vec=f.normal * dist, verts=f.verts)
            n = len(res["faces"])
        else:
            avg = sum((f.normal for f in faces), Vector()).normalized()
            res = bmesh.ops.extrude_face_region(e.bm, geom=faces)
            verts = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
            stale = [f for f in faces if f.is_valid]  # originals left behind as inner caps (if any)
            if stale:
                bmesh.ops.delete(e.bm, geom=stale, context="FACES")
            bmesh.ops.translate(e.bm, vec=avg * dist, verts=verts)
            n = len(faces)
        bmesh.ops.recalc_face_normals(e.bm, faces=e.bm.faces)
    return e.result(extruded_faces=n, distance=dist)


@command("inset_faces", undo=True)
def inset_faces(p):
    """Inset faces by `thickness` m, optionally pushed in/out by `depth` m (panels, windows, vents)."""
    thickness = float(_p(p, "thickness", 0.05))
    depth = float(_p(p, "depth", 0.0))
    with _Edit(_p(p, "object", required=True)) as e:
        faces = select_faces(e.ob, e.bm, _p(p, "selector"))
        if _p(p, "individual", False):
            bmesh.ops.inset_individual(e.bm, faces=faces, thickness=thickness, depth=depth, use_even_offset=True)
        else:
            bmesh.ops.inset_region(e.bm, faces=faces, thickness=thickness, depth=depth, use_even_offset=True)
    return e.result(inset_faces=len(faces))


@command("bevel_edges", undo=True)
def bevel_edges(p):
    """Bevel edges sharper than angle_deg (default 30), or the boundary edges of a face selector."""
    width = float(_p(p, "width", 0.02))
    segments = int(_p(p, "segments", 1))
    with _Edit(_p(p, "object", required=True)) as e:
        if _p(p, "selector"):
            faces = set(select_faces(e.ob, e.bm, p["selector"]))
            edges = [ed for ed in e.bm.edges if sum(1 for f in ed.link_faces if f in faces) == 1]
        else:
            lim = math.radians(float(_p(p, "angle_deg", 30)))
            edges = [ed for ed in e.bm.edges if len(ed.link_faces) == 2 and ed.calc_face_angle(0) > lim]
        if not edges:
            raise BridgeError("No edges matched (try a lower angle_deg)")
        bmesh.ops.bevel(e.bm, geom=edges, offset=width, offset_type="OFFSET", segments=segments,
                        profile=float(_p(p, "profile", 0.5)), affect="EDGES", clamp_overlap=True)
    return e.result(beveled_edges=len(edges))


@command("loop_cut", undo=True)
def loop_cut(p):
    """Slice the mesh with planes across an axis - adds edge loops for detail or deformation.
    positions: list of 0..1 fractions along the object's local bounds (default: evenly spaced `cuts`)."""
    axis = str(_p(p, "axis", "z")).lower()
    if axis not in ("x", "y", "z"):
        raise BridgeError("axis must be x, y or z")
    i = "xyz".index(axis)
    with _Edit(_p(p, "object", required=True)) as e:
        lo = min(v.co[i] for v in e.bm.verts)
        hi = max(v.co[i] for v in e.bm.verts)
        pos = _p(p, "positions")
        if pos is None:
            n = int(_p(p, "cuts", 1))
            pos = [(k + 1) / (n + 1) for k in range(n)]
        normal = Vector((0, 0, 0))
        normal[i] = 1.0
        for t in pos:
            co = Vector((0, 0, 0))
            co[i] = lo + (hi - lo) * float(t)
            geom = e.bm.verts[:] + e.bm.edges[:] + e.bm.faces[:]
            bmesh.ops.bisect_plane(e.bm, geom=geom, plane_co=co, plane_no=normal)
    return e.result(cuts=len(pos), axis=axis)


@command("delete_faces", undo=True)
def delete_faces(p):
    """Delete the faces a selector matches (e.g. hidden bottoms of props to save triangles)."""
    with _Edit(_p(p, "object", required=True)) as e:
        faces = select_faces(e.ob, e.bm, _p(p, "selector"))
        n = len(faces)
        bmesh.ops.delete(e.bm, geom=faces, context="FACES")
    return e.result(deleted_faces=n)
