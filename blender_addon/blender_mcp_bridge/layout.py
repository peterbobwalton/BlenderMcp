"""Scene layout: instancing/scatter, align, distribute, drop-to-floor, and a cheap scene diff."""

import hashlib
import math
import random

import bpy
from mathutils import Euler, Matrix, Vector
from mathutils.bvhtree import BVHTree

from .handlers import (BridgeError, _collection, _obj, _objs, _p, _scene, _vec, _world_bounds, command)

_AXES = {"x": 0, "y": 1, "z": 2}


def _axis(p, default="x"):
    a = str(_p(p, "axis", default)).lower()
    if a not in _AXES:
        raise BridgeError("axis must be x, y or z")
    return _AXES[a]


# ------------------------------------------------------------------ scatter

def _surface_points(target, count, rng, up_only, min_dist):
    """Area-weighted random points on a mesh (world space) with their normals."""
    dg = bpy.context.evaluated_depsgraph_get()
    ev = target.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        mw = target.matrix_world
        nm = mw.to_3x3().inverted_safe().transposed()
        me.calc_loop_triangles()
        tris = []
        for t in me.loop_triangles:
            a, b, c = (mw @ me.vertices[i].co for i in t.vertices)
            n = (nm @ t.normal).normalized()
            if up_only and n.z < 0.5:
                continue
            area = ((b - a).cross(c - a)).length / 2
            if area > 0:
                tris.append((area, a, b, c, n))
        if not tris:
            raise BridgeError(f"No usable faces on '{target.name}'" + (" facing up" if up_only else ""))
        total = sum(t[0] for t in tris)
        cum, acc = [], 0.0
        for t in tris:
            acc += t[0]
            cum.append(acc)
        pts, tries = [], 0
        while len(pts) < count and tries < count * 50:
            tries += 1
            r = rng.random() * total
            lo, hi = 0, len(cum) - 1
            while lo < hi:
                mid = (lo + hi) // 2
                if cum[mid] < r:
                    lo = mid + 1
                else:
                    hi = mid
            _, a, b, c, n = tris[lo]
            u, v = rng.random(), rng.random()
            if u + v > 1:
                u, v = 1 - u, 1 - v
            p = a + (b - a) * u + (c - a) * v
            if min_dist and any((p - q).length < min_dist for q, _ in pts):
                continue
            pts.append((p, n))
        return pts
    finally:
        ev.to_mesh_clear()


@command("scatter_instances", undo=True)
def scatter_instances(p):
    """Place linked copies (instances share mesh data: cheap, edit once) of a source object.
    mode=grid | surface | box. Random yaw/scale per copy, deterministic with 'seed'."""
    src = _obj(_p(p, "source", required=True))
    mode = _p(p, "mode", "grid").lower()
    rng = random.Random(int(_p(p, "seed", 1)))
    linked = bool(_p(p, "linked", True))
    col = _collection(_p(p, "collection") or f"{src.name}_Instances", create=True)
    yaw = float(_p(p, "random_yaw_deg", 0))
    smin, smax = (_p(p, "scale_range") or [1, 1])[:2]
    align = bool(_p(p, "align_to_normal", False))

    if mode == "grid":
        cnt = _p(p, "grid", [3, 3])
        nx, ny = int(cnt[0]), int(cnt[1])
        sp = _p(p, "spacing", 2.0)
        sp = [float(sp), float(sp)] if isinstance(sp, (int, float)) else [float(sp[0]), float(sp[1])]
        origin = Vector(_vec(_p(p, "origin", list(src.location)), name="origin"))
        jitter = float(_p(p, "jitter", 0))
        placements = []
        for ix in range(nx):
            for iy in range(ny):
                pos = origin + Vector((ix * sp[0], iy * sp[1], 0))
                if jitter:
                    pos += Vector((rng.uniform(-jitter, jitter), rng.uniform(-jitter, jitter), 0))
                placements.append((pos, Vector((0, 0, 1))))
    elif mode == "surface":
        target = _obj(_p(p, "target", required=True))
        placements = _surface_points(target, int(_p(p, "count", 10)), rng, bool(_p(p, "up_only", True)),
                                     float(_p(p, "min_distance", 0)))
    elif mode == "box":
        mn = Vector(_vec(_p(p, "min", required=True), name="min"))
        mx = Vector(_vec(_p(p, "max", required=True), name="max"))
        placements = [(Vector([rng.uniform(mn[i], mx[i]) for i in range(3)]), Vector((0, 0, 1)))
                      for _ in range(int(_p(p, "count", 10)))]
    else:
        raise BridgeError("mode must be grid, surface or box")
    if len(placements) > 5000:
        raise BridgeError("Refusing to create more than 5000 objects in one call")

    names = []
    for pos, normal in placements:
        ob = src.copy()
        if src.data is not None and not linked:
            ob.data = src.data.copy()
        ob.parent = None
        col.objects.link(ob)
        rot = Euler((0, 0, math.radians(rng.uniform(-yaw, yaw)) if yaw else 0))
        basis = rot.to_matrix()
        if align and normal.length > 0:
            basis = normal.to_track_quat("Z", "Y").to_matrix() @ basis
        s = rng.uniform(float(smin), float(smax))
        ob.matrix_world = Matrix.Translation(pos) @ basis.to_4x4() @ Matrix.Diagonal((s, s, s, 1))
        names.append(ob.name)
    return {"source": src.name, "mode": mode, "count": len(names), "collection": col.name, "linked": linked,
            "objects": names[:50], "note": "linked copies share one mesh - edit the source mesh to change them all" if linked else ""}


# ------------------------------------------------------------------ align / distribute / floor

@command("align_objects", undo=True)
def align_objects(p):
    """Align objects' bounding boxes on an axis: to='min'|'center'|'max', relative to the first object
    (default), the group's combined bounds ('group'), or an absolute world value."""
    objs = _objs(_p(p, "objects", required=True))
    i = _axis(p)
    to = str(_p(p, "to", "min")).lower()
    if to not in ("min", "center", "max"):
        raise BridgeError("to must be min, center or max")
    rel = _p(p, "relative_to", "first")
    bpy.context.view_layer.update()

    def key(o):
        mn, mx = _world_bounds([o])
        return {"min": mn[i], "max": mx[i], "center": (mn[i] + mx[i]) / 2}[to]

    if isinstance(rel, (int, float)):
        target = float(rel)
    elif rel == "group":
        mn, mx = _world_bounds(objs)
        target = {"min": mn[i], "max": mx[i], "center": (mn[i] + mx[i]) / 2}[to]
    else:
        target = key(objs[0])
    moved = []
    for o in objs:
        d = target - key(o)
        if abs(d) > 1e-9:
            o.location[i] += d
            moved.append(o.name)
    return {"axis": "xyz"[i], "to": to, "value": round(target, 4), "moved": moved}


@command("distribute_objects", undo=True)
def distribute_objects(p):
    """Spread objects along an axis in their current order (or sorted by position): fixed 'gap' between
    bounding boxes, or evenly between the first and last."""
    objs = _objs(_p(p, "objects", required=True))
    if len(objs) < 2:
        raise BridgeError("Need at least two objects")
    i = _axis(p)
    bpy.context.view_layer.update()
    if _p(p, "sort", True):
        objs.sort(key=lambda o: _world_bounds([o])[0][i])
    gap = _p(p, "gap")
    if gap is not None:
        cursor = _world_bounds([objs[0]])[1][i]
        for o in objs[1:]:
            mn, mx = _world_bounds([o])
            o.location[i] += cursor + float(gap) - mn[i]
            bpy.context.view_layer.update()
            cursor = _world_bounds([o])[1][i]
    else:
        first = (lambda b: (b[0][i] + b[1][i]) / 2)(_world_bounds([objs[0]]))
        last = (lambda b: (b[0][i] + b[1][i]) / 2)(_world_bounds([objs[-1]]))
        step = (last - first) / (len(objs) - 1)
        for k, o in enumerate(objs[1:-1], start=1):
            mn, mx = _world_bounds([o])
            o.location[i] += first + step * k - (mn[i] + mx[i]) / 2
    return {"axis": "xyz"[i], "order": [o.name for o in objs]}


@command("drop_to_floor", undo=True)
def drop_to_floor(p):
    """Move objects down (world Z) so they rest on the floor height, or on whatever mesh is below them."""
    objs = _objs(_p(p, "objects", required=True))
    floor = _p(p, "floor_z", 0.0)
    onto = bool(_p(p, "onto_objects", False))
    bpy.context.view_layer.update()
    trees = []
    if onto:
        dg = bpy.context.evaluated_depsgraph_get()
        for o in _scene().objects:
            if o.type == "MESH" and o not in objs and o.visible_get():
                # BVH is in the object's local space; rays are transformed into it below
                trees.append((BVHTree.FromObject(o, dg), o.matrix_world.copy()))
    out = []
    for o in objs:
        mn, mx = _world_bounds([o])
        rest = float(floor)
        if onto:
            c = Vector(((mn.x + mx.x) / 2, (mn.y + mx.y) / 2, mn.z + 1e-4))
            best = None
            for tree, mw in trees:
                inv = mw.inverted()
                hit, _, _, dist = tree.ray_cast(inv @ c, (inv.to_3x3() @ Vector((0, 0, -1))).normalized())
                if hit is not None:
                    z = (mw @ hit).z
                    if best is None or z > best:
                        best = z
            rest = best if best is not None else float(floor)
        d = rest - mn.z
        o.location.z += d
        out.append({"name": o.name, "moved_z": round(d, 4), "rests_at": round(rest, 4)})
    return {"results": out}


# ------------------------------------------------------------------ scene diff

_SNAPSHOTS = {}


def _fingerprint(o):
    h = hashlib.md5()
    h.update(repr([round(v, 5) for row in o.matrix_world for v in row]).encode())
    h.update(repr((o.type, o.data.name if o.data else None, o.hide_get(), o.parent.name if o.parent else None)).encode())
    h.update(repr([(m.name, m.type, m.show_viewport) for m in o.modifiers]).encode())
    if o.type == "MESH":
        me = o.data
        h.update(repr((len(me.vertices), len(me.polygons), [s.material.name if s.material else None for s in o.material_slots])).encode())
        if len(me.vertices) <= 200000:
            co = [0.0] * (len(me.vertices) * 3)
            me.vertices.foreach_get("co", co)
            h.update(repr([round(v, 4) for v in co[::max(1, len(co) // 3000)]]).encode())
    return h.hexdigest()


@command("scene_changes")
def scene_changes(p):
    """Take a snapshot (returns a token) or diff against an earlier token: objects added, removed, changed."""
    current = {o.name: _fingerprint(o) for o in _scene().objects}
    token = _p(p, "since")
    new_token = f"s{len(_SNAPSHOTS) + 1}"
    _SNAPSHOTS[new_token] = current
    while len(_SNAPSHOTS) > 20:
        _SNAPSHOTS.pop(next(iter(_SNAPSHOTS)))
    if token is None:
        return {"token": new_token, "objects": len(current)}
    old = _SNAPSHOTS.get(token)
    if old is None:
        raise BridgeError(f"Unknown or expired token '{token}' (Blender restarted?) - take a new snapshot")
    return {
        "token": new_token,
        "added": sorted(set(current) - set(old)),
        "removed": sorted(set(old) - set(current)),
        "changed": sorted(n for n in set(current) & set(old) if current[n] != old[n]),
    }
