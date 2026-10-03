"""Animation: keyframes, timeline, procedural presets, armatures/skinning, actions as clips, frame previews,
and the Unreal round trip (validate a rig, export a skeletal mesh + one FBX per action, import FBX animation).

Works with Blender 4.2+ legacy actions and 4.4+/5.x slotted (layered) actions."""

import contextlib
import math
import os
import re
import tempfile

import bpy
from mathutils import Euler, Matrix, Vector

from . import imaging
from .handlers import (BridgeError, _ensure_object_mode, _obj, _objs, _p, _r, _scene, _selected_only, _vec,
                       _window_ctx, command)

try:
    from bpy_extras import anim_utils
except Exception:  # pragma: no cover
    anim_utils = None

_BONE_RE = re.compile(r'^pose\.bones\["((?:[^"\\]|\\.)*)"\]\.(.+)$')
_EULER_MODES = {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}
_CHANNELS = {
    "location": ("location",), "rotation": ("rotation_euler", "rotation_quaternion", "rotation_axis_angle"),
    "scale": ("scale",),
}
_AXIS = {"X": 0, "Y": 1, "Z": 2}


# ------------------------------------------------------------------ action helpers (legacy + slotted)

def _bags(action, slot=None):
    """F-curve containers of an action: channelbags (4.4+/5.x) or the action itself (legacy)."""
    if getattr(action, "layers", None) is not None and anim_utils and hasattr(anim_utils, "action_get_channelbag_for_slot"):
        slots = [slot] if slot is not None else list(action.slots)
        bags = [b for b in (anim_utils.action_get_channelbag_for_slot(action, s) for s in slots) if b is not None]
        # with an explicit slot never fall back: on 4.4/4.5 action.fcurves is the FIRST slot's curves, not this one's
        if bags or slot is not None or not hasattr(action, "fcurves"):
            return bags
    return [action] if hasattr(action, "fcurves") else []


def _fcurves(action, slot=None):
    """[(owning collection, fcurve)]"""
    return [(b.fcurves, fc) for b in _bags(action, slot) for fc in b.fcurves]


def _obj_action(ob):
    ad = ob.animation_data
    if ad is None or ad.action is None:
        return None, None
    return ad.action, getattr(ad, "action_slot", None)


def _assign_action(ob, act, slot_identifier=None):
    ad = ob.animation_data or ob.animation_data_create()
    if ad.action != act:
        ad.action = act
    if slot_identifier and hasattr(ad, "action_slot"):
        slot = next((s for s in act.slots if s.identifier == slot_identifier), None)
        if slot is not None:
            ad.action_slot = slot
    if hasattr(ad, "action_slot") and ad.action_slot is None:
        suitable = list(getattr(ad, "action_suitable_slots", []) or [])
        if suitable:
            ad.action_slot = suitable[0]
    act.use_fake_user = True  # clips must survive while another action is active
    return act


def _use_action(ob, name):
    """Assign (creating if needed) the named action; with no name keep the current one."""
    if not name:
        return _obj_action(ob)[0]
    act = bpy.data.actions.get(name) or bpy.data.actions.new(name)
    return _assign_action(ob, act)


def _action_range(act):
    fr = act.frame_range
    return [round(fr[0], 3), round(fr[1], 3)]


def _action_bones(act):
    return sorted({m.group(1) for _, fc in _fcurves(act) if (m := _BONE_RE.match(fc.data_path))})


def _key_count(act, slot=None):
    return sum(len(fc.keyframe_points) for _, fc in _fcurves(act, slot))


def _path(bone, prop):
    return f'pose.bones["{bone}"].{prop}' if bone else prop


def _target(ob, bone):
    if not bone:
        return ob
    if ob.type != "ARMATURE":
        raise BridgeError(f"'{ob.name}' is a {ob.type}; 'bone' needs an armature")
    pb = ob.pose.bones.get(bone)
    if pb is None:
        close = [b.name for b in ob.pose.bones if bone.lower() in b.name.lower()][:10]
        raise BridgeError(f"Bone '{bone}' not found in {ob.name}." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    return pb


def _match_channels(fc_path, bone, channels):
    m = _BONE_RE.match(fc_path)
    fc_bone, prop = (m.group(1), m.group(2)) if m else (None, fc_path)
    if bone is not None and fc_bone != bone:
        return False
    if not channels:
        return True
    for c in channels:
        if prop in _CHANNELS.get(c.lower(), ()) or prop == c or fc_path == c:
            return True
    return False


def _set_rotation(target, deg):
    """Rotation given as Euler degrees, written in the target's own rotation mode. Returns the property name."""
    mode = target.rotation_mode
    e = Euler([math.radians(v) for v in _vec(deg, name="rotation")], mode if mode in _EULER_MODES else "XYZ")
    if mode == "QUATERNION":
        target.rotation_quaternion = e.to_quaternion()
        return "rotation_quaternion"
    if mode == "AXIS_ANGLE":
        axis, ang = e.to_quaternion().to_axis_angle()
        target.rotation_axis_angle = (ang, *axis)
        return "rotation_axis_angle"
    target.rotation_euler = e
    return "rotation_euler"


def _set_interp(ob, paths, frames, interp, easing=None):
    act, slot = _obj_action(ob)
    if act is None or not interp:
        return
    want = {round(f, 3) for f in frames} if frames is not None else None
    for _, fc in _fcurves(act, slot):
        if fc.data_path in paths:
            for kp in fc.keyframe_points:
                if want is None or round(kp.co.x, 3) in want:
                    kp.interpolation = interp
                    if easing:
                        kp.easing = easing
            fc.update()


def _refresh():
    sc = _scene()
    sc.frame_set(sc.frame_current, subframe=sc.frame_subframe)


def _deform_meshes(arm):
    return [o for o in bpy.data.objects if o.type == "MESH"
            and any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers)]


def _armature(name):
    ob = _obj(name)
    if ob.type != "ARMATURE":
        # accept a skinned mesh and use its armature
        arm = next((m.object for m in getattr(ob, "modifiers", []) if m.type == "ARMATURE" and m.object), None)
        if arm is None:
            raise BridgeError(f"'{ob.name}' is a {ob.type}, not an armature (and has no Armature modifier)")
        return arm
    return ob


def _interp_name(v):
    return v.upper() if v else None


# ------------------------------------------------------------------ timeline

@command("set_timeline", undo=True)
def set_timeline(p):
    """Scene frame range, frame rate and current frame. fit_to_action=<object> uses that object's action range."""
    sc = _scene()
    fit = _p(p, "fit_to_action")
    if fit:
        act, _ = _obj_action(_obj(fit))
        if act is None:
            raise BridgeError(f"'{fit}' has no action")
        a, b = act.frame_range
        sc.frame_start, sc.frame_end = int(math.floor(a)), int(math.ceil(b))
    if _p(p, "frame_start") is not None:
        sc.frame_start = int(p["frame_start"])
    if _p(p, "frame_end") is not None:
        sc.frame_end = int(p["frame_end"])
    if _p(p, "fps") is not None:
        fps = float(p["fps"])
        if fps <= 0:
            raise BridgeError("fps must be > 0")
        # 29.97 etc. need fps_base: fps / fps_base
        if abs(fps - round(fps)) < 1e-6:
            sc.render.fps, sc.render.fps_base = int(round(fps)), 1.0
        else:
            sc.render.fps, sc.render.fps_base = int(math.ceil(fps)), math.ceil(fps) / fps
    if _p(p, "frame") is not None:
        sc.frame_set(int(p["frame"]))
    return _timeline_info()


def _timeline_info():
    sc = _scene()
    fps = sc.render.fps / sc.render.fps_base
    return {"frame_start": sc.frame_start, "frame_end": sc.frame_end, "frame": sc.frame_current,
            "fps": round(fps, 3), "seconds": round((sc.frame_end - sc.frame_start + 1) / fps, 3)}


# ------------------------------------------------------------------ keyframes

@command("insert_keyframes", undo=True)
def insert_keyframes(p):
    """keys: [{frame, location?, rotation? (deg), scale?, properties?: {data_path: value}}] on an object or pose bone."""
    ob = _obj(_p(p, "object", required=True))
    bone = _p(p, "bone")
    tgt = _target(ob, bone)
    _ensure_object_mode()
    act = _use_action(ob, _p(p, "action"))
    keys = _p(p, "keys", required=True)
    if not isinstance(keys, list) or not keys:
        raise BridgeError("'keys' must be a non-empty list of {frame, location/rotation/scale/properties}")
    interp = _interp_name(_p(p, "interpolation"))
    paths, frames, count, errors = set(), [], 0, []
    for k in keys:
        if not isinstance(k, dict) or "frame" not in k:
            raise BridgeError(f"Each key needs a 'frame': {k!r}")
        f = float(k["frame"])
        frames.append(f)
        if k.get("location") is not None:
            tgt.location = _vec(k["location"], name="location")
            tgt.keyframe_insert("location", frame=f)
            paths.add(_path(bone, "location"))
            count += 1
        if k.get("rotation") is not None:
            prop = _set_rotation(tgt, k["rotation"])
            tgt.keyframe_insert(prop, frame=f)
            paths.add(_path(bone, prop))
            count += 1
        if k.get("scale") is not None:
            tgt.scale = _vec(k["scale"], name="scale")
            tgt.keyframe_insert("scale", frame=f)
            paths.add(_path(bone, "scale"))
            count += 1
        for dp, val in (k.get("properties") or {}).items():
            try:
                owner, attr = (tgt.path_resolve(dp.rsplit(".", 1)[0]), dp.rsplit(".", 1)[1]) if "." in dp else (tgt, dp)
                if isinstance(getattr(owner, attr), float) and owner.bl_rna.properties[attr].subtype in ("ANGLE",):
                    val = math.radians(val)
                setattr(owner, attr, val)
                owner.keyframe_insert(attr, frame=f)
                count += 1
                if owner is tgt:
                    paths.add(_path(bone, attr))
            except Exception as ex:
                errors.append(f"frame {f:g} {dp}: {ex}")
    act = _obj_action(ob)[0] or act
    if act is not None:
        act.use_fake_user = True
    _set_interp(ob, paths, frames, interp)
    _refresh()
    out = {"object": ob.name, "bone": bone, "action": act.name if act else None, "channels_keyed": count,
           "frames": sorted(set(frames)), "action_range": _action_range(act) if act else None}
    if errors:
        out["errors"] = errors
    return out


@command("delete_keyframes", undo=True)
def delete_keyframes(p):
    """Remove keys by channel / bone / frames. With no frames or frame_range every key of the matching channels goes."""
    ob = _obj(_p(p, "object", required=True))
    act, slot = _obj_action(ob)
    if act is None:
        return {"object": ob.name, "removed": 0, "note": "no action"}
    bone = _p(p, "bone")
    if bone:
        _target(ob, bone)
    channels = _p(p, "channels")
    frames = {round(float(f), 3) for f in (_p(p, "frames") or [])}
    rng = _p(p, "frame_range")
    removed, dropped = 0, []
    for coll, fc in _fcurves(act, slot):
        if not _match_channels(fc.data_path, bone, channels):
            continue
        if not frames and not rng:
            removed += len(fc.keyframe_points)
            dropped.append(fc.data_path)
            coll.remove(fc)
            continue
        idx = [i for i, kp in enumerate(fc.keyframe_points)
               if round(kp.co.x, 3) in frames or (rng and rng[0] <= kp.co.x <= rng[1])]
        for i in reversed(idx):
            fc.keyframe_points.remove(fc.keyframe_points[i], fast=True)
        removed += len(idx)
        if len(fc.keyframe_points) == 0:
            dropped.append(fc.data_path)
            coll.remove(fc)
        else:
            fc.update()
    _refresh()
    return {"object": ob.name, "action": act.name, "removed": removed,
            "channels_removed": sorted(set(dropped)), "keys_left": _key_count(act, slot)}


@command("set_interpolation", undo=True)
def set_interpolation(p):
    """Interpolation/easing, looping (Cycles modifier) and extrapolation for an object's or bone's keys."""
    ob = _obj(_p(p, "object", required=True))
    act, slot = _obj_action(ob)
    if act is None:
        raise BridgeError(f"'{ob.name}' has no animation")
    bone = _p(p, "bone")
    channels = _p(p, "channels")
    interp = _interp_name(_p(p, "interpolation"))
    easing = _interp_name(_p(p, "easing"))
    if easing and not easing.startswith("EASE") and easing != "AUTO":
        easing = {"IN": "EASE_IN", "OUT": "EASE_OUT", "IN_OUT": "EASE_IN_OUT", "INOUT": "EASE_IN_OUT"}.get(easing, easing)
    loop = _p(p, "loop")
    extrap = _interp_name(_p(p, "extrapolation"))
    touched = 0
    for _, fc in _fcurves(act, slot):
        if not _match_channels(fc.data_path, bone, channels):
            continue
        touched += 1
        for kp in fc.keyframe_points:
            if interp:
                kp.interpolation = interp
            if easing:
                kp.easing = easing
        if extrap:
            fc.extrapolation = extrap
        if loop is not None:
            cyc = [m for m in fc.modifiers if m.type == "CYCLES"]
            if loop and not cyc:
                m = fc.modifiers.new("CYCLES")
                if str(loop).lower() == "offset":
                    m.mode_before = m.mode_after = "REPEAT_OFFSET"
            elif not loop:
                for m in cyc:
                    fc.modifiers.remove(m)
        fc.update()
    if loop is not None and hasattr(act, "use_cyclic"):
        act.use_cyclic = bool(loop)  # also tells exporters/NLA that the clip loops
    _refresh()
    return {"object": ob.name, "action": act.name, "fcurves": touched, "interpolation": interp, "easing": easing,
            "loop": loop, "extrapolation": extrap}


# ------------------------------------------------------------------ procedural presets

def _key_series(ob, tgt, bone, prop, idx, series, interp, easing=None):
    for f, v in series:
        getattr(tgt, prop)[idx] = v
        tgt.keyframe_insert(prop, index=idx, frame=f)
    path = _path(bone, prop)
    act, slot = _obj_action(ob)
    fcs = []
    for _, fc in _fcurves(act, slot):
        if fc.data_path == path and fc.array_index == idx:
            frames = {round(f, 3) for f, _ in series}
            for kp in fc.keyframe_points:
                if round(kp.co.x, 3) in frames:
                    kp.interpolation = interp
                    if easing:
                        kp.easing = easing
            fcs.append(fc)
    return fcs


def _loop(fcs, offset=False):
    for fc in fcs:
        if not any(m.type == "CYCLES" for m in fc.modifiers):
            m = fc.modifiers.new("CYCLES")
            if offset:
                m.mode_before = m.mode_after = "REPEAT_OFFSET"
        fc.update()


@command("animate_preset", undo=True)
def animate_preset(p):
    """Procedural motion: spin, bob, sway, pulse, bounce, follow_path. Loops by default."""
    ob = _obj(_p(p, "object", required=True))
    bone = _p(p, "bone")
    tgt = _target(ob, bone)
    _ensure_object_mode()
    preset = str(_p(p, "preset", required=True)).lower()
    sc = _scene()
    fps = sc.render.fps / sc.render.fps_base
    period = float(_p(p, "period", round(2 * fps)))
    if period <= 0:
        raise BridgeError("period must be > 0 frames")
    start = float(_p(p, "start", sc.frame_start))
    axis = str(_p(p, "axis", "Z")).upper()
    if axis not in _AXIS:
        raise BridgeError("axis must be X, Y or Z")
    i = _AXIS[axis]
    loop = bool(_p(p, "loop", True))
    act = _use_action(ob, _p(p, "action"))
    notes = []

    def rot_euler():
        if tgt.rotation_mode not in _EULER_MODES:
            m = tgt.matrix_basis.to_quaternion() if hasattr(tgt, "matrix_basis") else None
            tgt.rotation_mode = "XYZ"
            if m is not None:
                tgt.rotation_euler = m.to_euler("XYZ")
            notes.append("rotation_mode switched to XYZ Euler")
        return "rotation_euler"

    fcs = []
    if preset == "spin":
        turns = float(_p(p, "amount", 1.0))
        prop = rot_euler()
        base = getattr(tgt, prop)[i]
        fcs = _key_series(ob, tgt, bone, prop, i, [(start, base), (start + period, base + 2 * math.pi * turns)], "LINEAR")
        if loop:
            _loop(fcs, offset=True)
    elif preset in ("bob", "bounce"):
        amt = float(_p(p, "amount", 0.1 if preset == "bob" else 0.5))
        base = tgt.location[i]
        if preset == "bob":
            fcs = _key_series(ob, tgt, bone, "location", i,
                              [(start, base), (start + period / 2, base + amt), (start + period, base)], "SINE", "EASE_IN_OUT")
        else:  # fast contact, slow apex
            fcs = _key_series(ob, tgt, bone, "location", i, [(start, base)], "QUAD", "EASE_OUT")
            fcs = _key_series(ob, tgt, bone, "location", i, [(start + period / 2, base + amt)], "QUAD", "EASE_IN")
            fcs = _key_series(ob, tgt, bone, "location", i, [(start + period, base)], "QUAD", "EASE_OUT")
        if loop:
            _loop(fcs)
    elif preset == "sway":
        amt = math.radians(float(_p(p, "amount", 15.0)))
        prop = rot_euler()
        base = getattr(tgt, prop)[i]
        fcs = _key_series(ob, tgt, bone, prop, i, [(start, base), (start + period / 4, base + amt),
                                                   (start + 3 * period / 4, base - amt), (start + period, base)],
                          "SINE", "EASE_IN_OUT")
        if loop:
            _loop(fcs)
    elif preset == "pulse":
        amt = float(_p(p, "amount", 0.1))
        base = Vector(tgt.scale)
        for k in range(3):
            fcs += _key_series(ob, tgt, bone, "scale", k, [(start, base[k]), (start + period / 2, base[k] * (1 + amt)),
                                                           (start + period, base[k])], "SINE", "EASE_IN_OUT")
        if loop:
            _loop(fcs)
    elif preset == "follow_path":
        if bone:
            raise BridgeError("follow_path works on objects, not bones")
        path = _obj(_p(p, "path", required=True))
        if path.type != "CURVE":
            raise BridgeError(f"'{path.name}' is a {path.type}; follow_path needs a curve")
        for c in [c for c in ob.constraints if c.type == "FOLLOW_PATH"]:
            ob.constraints.remove(c)
        con = ob.constraints.new("FOLLOW_PATH")
        con.target = path
        con.use_curve_follow = True
        con.forward_axis = str(_p(p, "forward_axis", "TRACK_NEGATIVE_Y")).upper()
        con.up_axis = "UP_Z"
        cu = path.data
        cu.use_path = True
        cu.path_duration = int(period)
        cu.eval_time = 0
        cu.keyframe_insert("eval_time", frame=start)
        cu.eval_time = period
        cu.keyframe_insert("eval_time", frame=start + period)
        ad = cu.animation_data
        for _, fc in _fcurves(ad.action, getattr(ad, "action_slot", None)):
            if fc.data_path == "eval_time":
                for kp in fc.keyframe_points:
                    kp.interpolation = "LINEAR"
                fcs.append(fc)
                if loop:
                    _loop([fc], offset=False)
        notes.append("the object's own location is an offset from the path - set it to 0,0,0 to ride exactly on it")
    else:
        raise BridgeError("preset must be spin, bob, sway, pulse, bounce or follow_path")
    act = _obj_action(ob)[0] or act
    if act is not None:
        act.use_fake_user = True
        if loop and hasattr(act, "use_cyclic"):
            act.use_cyclic = True
    _refresh()
    out = {"object": ob.name, "bone": bone, "preset": preset, "action": act.name if act else None,
           "frames": [start, start + period], "seconds": round(period / fps, 3), "loop": loop}
    if notes:
        out["notes"] = notes
    return out


# ------------------------------------------------------------------ inspection

@command("list_actions")
def list_actions(p):
    """Every action (clip) in the file: frame range, keys, animated bones, who uses it."""
    users = {}
    for o in bpy.data.objects:
        act, _ = _obj_action(o)
        if act is not None:
            users.setdefault(act.name, []).append(o.name)
    sc = _scene()
    fps = sc.render.fps / sc.render.fps_base
    rows = []
    for act in bpy.data.actions:
        rng = _action_range(act)
        bones = _action_bones(act)
        rows.append({
            "name": act.name, "frame_range": rng, "seconds": round((rng[1] - rng[0]) / fps, 3),
            "fcurves": len(_fcurves(act)), "keys": _key_count(act), "bones_animated": len(bones),
            "active_on": users.get(act.name, []), "fake_user": act.use_fake_user, "users": act.users,
            "looping": bool(getattr(act, "use_cyclic", False)),
            "slots": [s.identifier for s in getattr(act, "slots", [])],
        })
    return {"fps": round(fps, 3), "actions": rows}


def _world_head(arm, bone):
    return arm.matrix_world @ arm.pose.bones[bone].matrix.translation


def _root_motion(arm, act):
    """World travel of the root bone and of the hips (first child of the root) over the action."""
    roots = [b for b in arm.data.bones if b.parent is None]
    if not roots:
        return None
    root = roots[0]
    hips = next(iter(root.children), None)
    sc = _scene()
    saved = (sc.frame_current, sc.frame_subframe)
    a, b = act.frame_range
    try:
        sc.frame_set(int(math.floor(a)))
        r0 = _world_head(arm, root.name)
        h0 = _world_head(arm, hips.name) if hips else None
        sc.frame_set(int(math.ceil(b)))
        r1 = _world_head(arm, root.name)
        h1 = _world_head(arm, hips.name) if hips else None
    finally:
        sc.frame_set(saved[0], subframe=saved[1])
    out = {"root_bone": root.name, "root_travel_m": _r(r1 - r0, 3)}
    if hips:
        out["hips_bone"] = hips.name
        out["hips_travel_m"] = _r(h1 - h0, 3)
    return out


@command("get_animation")
def get_animation(p):
    """An object's animation: action, slot, keyed channels per bone/object, range, NLA, root motion."""
    ob = _obj(_p(p, "object", required=True))
    act, slot = _obj_action(ob)
    ad = ob.animation_data
    out = {"object": ob.name, "type": ob.type, "timeline": _timeline_info(), "action": None}
    if ad is not None and ad.nla_tracks:
        out["nla_tracks"] = [{"name": t.name, "mute": t.mute,
                              "strips": [{"name": s.name, "action": s.action.name if s.action else None,
                                          "frames": [s.frame_start, s.frame_end]} for s in t.strips]}
                             for t in ad.nla_tracks]
    if act is None:
        return out
    include_keys = bool(_p(p, "include_keys", False))
    bone_filter = _p(p, "bone")
    max_keys = int(_p(p, "max_keys", 200))
    channels, shown = {}, 0
    for _, fc in _fcurves(act, slot):
        m = _BONE_RE.match(fc.data_path)
        tgt, prop = (m.group(1), m.group(2)) if m else ("<object>", fc.data_path)
        if bone_filter and tgt != bone_filter:
            continue
        d = channels.setdefault(tgt, {}).setdefault(prop, {"indices": [], "keys": 0, "range": [math.inf, -math.inf]})
        d["indices"].append(fc.array_index)
        d["keys"] = max(d["keys"], len(fc.keyframe_points))
        if fc.keyframe_points:
            d["range"] = [min(d["range"][0], fc.keyframe_points[0].co.x), max(d["range"][1], fc.keyframe_points[-1].co.x)]
            d["interpolation"] = fc.keyframe_points[0].interpolation
        if any(mo.type == "CYCLES" for mo in fc.modifiers):
            d["loop"] = True
        if include_keys and shown < max_keys:
            d.setdefault("values", {})[fc.array_index] = [[round(kp.co.x, 2), round(kp.co.y, 4)] for kp in fc.keyframe_points]
            shown += len(fc.keyframe_points)
    for props in channels.values():
        for d in props.values():
            if d["range"][0] == math.inf:
                d["range"] = None
    out["action"] = {"name": act.name, "slot": getattr(slot, "identifier", None), "frame_range": _action_range(act),
                     "keys": _key_count(act, slot), "looping": bool(getattr(act, "use_cyclic", False))}
    if ob.type == "ARMATURE" and not bone_filter and len(channels) > 40:
        out["bones_animated"] = sorted(channels)  # compact for big rigs
        out["note"] = "Large rig: pass bone=<name> for that bone's channels"
    else:
        out["channels"] = channels
    if ob.type == "ARMATURE":
        rm = _root_motion(ob, act)
        if rm:
            out["root_motion"] = rm
    if include_keys and shown >= max_keys:
        out["keys_truncated"] = True
    return out


# ------------------------------------------------------------------ actions (clips)

@command("set_action", undo=True)
def set_action(p):
    """Make an action the object's active clip (creating it if needed); optional manual range and looping."""
    ob = _obj(_p(p, "object", required=True))
    name = _p(p, "action", required=True)
    act = bpy.data.actions.get(name)
    if act is None:
        if not _p(p, "create", True):
            raise BridgeError(f"Action '{name}' not found. Existing: {[a.name for a in bpy.data.actions][:40]}")
        act = bpy.data.actions.new(name)
    _assign_action(ob, act)
    rng = _p(p, "frame_range")
    if rng:
        act.use_frame_range = True
        act.frame_start, act.frame_end = float(rng[0]), float(rng[1])
    if _p(p, "loop") is not None:
        act.use_cyclic = bool(p["loop"])
    if _p(p, "fit_timeline", True) and _key_count(act):
        a, b = act.frame_range
        _scene().frame_start, _scene().frame_end = int(math.floor(a)), int(math.ceil(b))
    _refresh()
    ad = ob.animation_data
    return {"object": ob.name, "action": act.name, "slot": getattr(getattr(ad, "action_slot", None), "identifier", None),
            "frame_range": _action_range(act), "keys": _key_count(act), "timeline": _timeline_info()}


@command("manage_action", undo=True)
def manage_action(p):
    """rename | duplicate | delete an action."""
    name = _p(p, "action", required=True)
    act = bpy.data.actions.get(name)
    if act is None:
        raise BridgeError(f"Action '{name}' not found. Existing: {[a.name for a in bpy.data.actions][:40]}")
    op = str(_p(p, "operation", required=True)).lower()
    if op == "rename":
        act.name = _p(p, "new_name", required=True)
        return {"renamed": [name, act.name]}
    if op == "duplicate":
        dup = act.copy()
        dup.name = _p(p, "new_name") or f"{name}_copy"
        dup.use_fake_user = True
        return {"duplicated": name, "action": dup.name}
    if op == "delete":
        for o in bpy.data.objects:
            if o.animation_data and o.animation_data.action == act:
                o.animation_data.action = None
        bpy.data.actions.remove(act)
        return {"deleted": name}
    raise BridgeError("operation must be rename, duplicate or delete")


# ------------------------------------------------------------------ rigging

_HUMANOID = [  # (name, head, tail, parent, connected) - Unreal mannequin bone names, 1.8 m tall, facing -Y
    ("root", (0, 0, 0), (0, 0.25, 0), None, False),
    ("pelvis", (0, 0, 0.95), (0, 0, 1.05), "root", False),
    ("spine_01", (0, 0, 1.05), (0, 0, 1.18), "pelvis", True),
    ("spine_02", (0, 0, 1.18), (0, 0, 1.32), "spine_01", True),
    ("spine_03", (0, 0, 1.32), (0, 0, 1.45), "spine_02", True),
    ("neck_01", (0, 0, 1.45), (0, 0, 1.55), "spine_03", True),
    ("head", (0, 0, 1.55), (0, 0, 1.78), "neck_01", True),
]
for _s, _x in (("l", 1), ("r", -1)):
    _HUMANOID += [
        (f"clavicle_{_s}", (0.02 * _x, 0, 1.42), (0.17 * _x, 0, 1.43), "spine_03", False),
        (f"upperarm_{_s}", (0.17 * _x, 0, 1.43), (0.45 * _x, 0.01, 1.43), f"clavicle_{_s}", True),
        (f"lowerarm_{_s}", (0.45 * _x, 0.01, 1.43), (0.71 * _x, 0, 1.43), f"upperarm_{_s}", True),
        (f"hand_{_s}", (0.71 * _x, 0, 1.43), (0.80 * _x, 0, 1.43), f"lowerarm_{_s}", True),
        (f"thigh_{_s}", (0.09 * _x, 0, 0.95), (0.09 * _x, -0.01, 0.52), "pelvis", False),
        (f"calf_{_s}", (0.09 * _x, -0.01, 0.52), (0.09 * _x, 0, 0.09), f"thigh_{_s}", True),
        (f"foot_{_s}", (0.09 * _x, 0, 0.09), (0.09 * _x, -0.12, 0.03), f"calf_{_s}", True),
        (f"ball_{_s}", (0.09 * _x, -0.12, 0.03), (0.09 * _x, -0.19, 0.03), f"foot_{_s}", True),
    ]


@contextlib.contextmanager
def _arm_edit(ob):
    _ensure_object_mode()
    with _selected_only([ob]), bpy.context.temp_override(**_window_ctx(), active_object=ob, object=ob,
                                                          selected_objects=[ob], selected_editable_objects=[ob]):
        bpy.ops.object.mode_set(mode="EDIT")
        try:
            yield ob.data.edit_bones
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")


@command("create_armature", undo=True)
def create_armature(p):
    """Armature from a bone list ({name, head, tail, parent?, connect?, roll?, deform?}) and/or template=humanoid."""
    from .handlers import _collection
    name = _p(p, "name", "Armature_Rig")
    spec = []
    tmpl = _p(p, "template")
    if tmpl:
        if str(tmpl).lower() != "humanoid":
            raise BridgeError("template must be 'humanoid'")
        s = float(_p(p, "height", 1.8)) / 1.8
        spec += [{"name": n, "head": [c * s for c in h], "tail": [c * s for c in t], "parent": par, "connect": con}
                 for n, h, t, par, con in _HUMANOID]
    spec += list(_p(p, "bones") or [])
    if not spec:
        raise BridgeError("Give 'bones' [{name, head, tail, parent?}] and/or template='humanoid'")
    names = [b.get("name") for b in spec]
    if len(set(names)) != len(names) or not all(names):
        raise BridgeError("Every bone needs a unique name")
    for b in spec:
        if b.get("parent") and b["parent"] not in names:
            raise BridgeError(f"Bone '{b['name']}': parent '{b['parent']}' is not in the list")
    data = bpy.data.armatures.new(name)
    ob = bpy.data.objects.new(name, data)
    _collection(_p(p, "collection"), create=True).objects.link(ob)
    ob.location = _vec(_p(p, "location", [0, 0, 0]), name="location")
    data.display_type = str(_p(p, "display", "OCTAHEDRAL")).upper()
    ob.show_in_front = bool(_p(p, "in_front", True))
    with _arm_edit(ob) as ebs:
        made = {}
        for b in spec:
            eb = ebs.new(b["name"])
            eb.head = _vec(b.get("head") or [0, 0, 0], name=f"{b['name']}.head")
            eb.tail = _vec(b.get("tail") or [0, 0.1, 0], name=f"{b['name']}.tail")
            if (eb.tail - eb.head).length < 1e-5:
                raise BridgeError(f"Bone '{b['name']}' has zero length")
            eb.roll = math.radians(float(b.get("roll") or 0))
            eb.use_deform = b.get("deform") is not False
            made[b["name"]] = eb
        for b in spec:
            if b.get("parent"):
                eb = made[b["name"]]
                eb.parent = made[b["parent"]]
                eb.use_connect = bool(b.get("connect", False)) and (eb.head - eb.parent.tail).length < 1e-4
    return {"armature": ob.name, "bones": len(data.bones), "roots": [b.name for b in data.bones if b.parent is None],
            "height_m": round(max((ob.matrix_world @ b.tail_local).z for b in data.bones), 3)}


def _unweighted(mesh_ob, bone_names):
    gi = {g.index for g in mesh_ob.vertex_groups if g.name in bone_names}
    return sum(1 for v in mesh_ob.data.vertices if not any(g.group in gi and g.weight > 0 for g in v.groups))


def _fill_nearest(m, arm, bone_names):
    """Bind every vertex without bone weights 100% to the closest deform bone (distance to the bone segment)."""
    from mathutils.geometry import intersect_point_line
    segs = [(b.name, arm.matrix_world @ b.head_local, arm.matrix_world @ b.tail_local)
            for b in arm.data.bones if b.name in bone_names]
    gi = {g.index for g in m.vertex_groups if g.name in bone_names}
    todo = {}
    for v in m.data.vertices:
        if any(g.group in gi and g.weight > 0 for g in v.groups):
            continue
        co = m.matrix_world @ v.co
        best, bd = None, math.inf
        for name, h, t in segs:
            pt, f = intersect_point_line(co, h, t)
            pt = h if f < 0 else t if f > 1 else pt
            d = (co - pt).length
            if d < bd:
                best, bd = name, d
        if best:
            todo.setdefault(best, []).append(v.index)
    for name, idx in todo.items():
        (m.vertex_groups.get(name) or m.vertex_groups.new(name=name)).add(idx, 1.0, "REPLACE")
    return sum(len(i) for i in todo.values())


@command("parent_with_weights", undo=True)
def parent_with_weights(p):
    """Skin meshes to an armature: auto (heat) weights, envelope, empty groups, or rigid to one bone."""
    arm = _obj(_p(p, "armature", required=True))
    if arm.type != "ARMATURE":
        raise BridgeError(f"'{arm.name}' is not an armature")
    meshes = _objs(_p(p, "objects", required=True))
    for m in meshes:
        if m.type != "MESH":
            raise BridgeError(f"'{m.name}' is a {m.type}, not a mesh")
    mode = str(_p(p, "mode", "auto")).lower()
    _ensure_object_mode()
    bone_names = {b.name for b in arm.data.bones if b.use_deform}
    if mode == "rigid":
        bone = _p(p, "bone", required=True)
        _target(arm, bone)
        if bone not in bone_names:
            raise BridgeError(f"'{bone}' is not a deform bone, so the armature modifier would ignore its weights")
        for m in meshes:
            mw = m.matrix_world.copy()
            # rigid means one bone only: drop weights left by an earlier auto/envelope skin (the suggested fallback)
            for g in [g for g in m.vertex_groups if g.name in bone_names and g.name != bone]:
                m.vertex_groups.remove(g)
            vg = m.vertex_groups.get(bone) or m.vertex_groups.new(name=bone)
            vg.add(range(len(m.data.vertices)), 1.0, "REPLACE")
            if not any(md.type == "ARMATURE" and md.object == arm for md in m.modifiers):
                md = m.modifiers.new("Armature", "ARMATURE")
                md.object = arm
            m.parent = arm
            m.parent_type = "OBJECT"
            m.matrix_parent_inverse = arm.matrix_world.inverted()
            m.matrix_world = mw
    else:
        op = {"auto": "ARMATURE_AUTO", "envelope": "ARMATURE_ENVELOPE", "empty": "ARMATURE_NAME"}.get(mode)
        if op is None:
            raise BridgeError("mode must be auto, envelope, empty or rigid")
        with _selected_only([arm] + meshes), bpy.context.temp_override(
                **_window_ctx(), active_object=arm, object=arm, selected_objects=[arm] + meshes,
                selected_editable_objects=[arm] + meshes):
            bpy.ops.object.parent_set(type=op, keep_transform=True)
    out, warnings = [], []
    fill = bool(_p(p, "fill_unweighted", True))
    for m in meshes:
        un = _unweighted(m, bone_names)
        row = {"mesh": m.name, "vertices": len(m.data.vertices), "unweighted_vertices": un}
        if un and mode in ("auto", "envelope"):
            if fill:
                row["filled_from_nearest_bone"] = _fill_nearest(m, arm, bone_names)
                un = row["unweighted_vertices"] = _unweighted(m, bone_names)
                warnings.append(f"{m.name}: bone heat left {row['filled_from_nearest_bone']} vertices unweighted "
                                f"(common with intersecting or very low-poly parts); they were bound 100% to the "
                                f"nearest bone - check the deformation with render_animation_frames")
            else:
                warnings.append(f"{m.name}: {un} vertices got no weights (bone heat can fail on non-manifold or "
                                f"intersecting geometry - run mesh_cleanup, or use mode=rigid for hard-surface parts)")
        row["groups"] = len([g for g in m.vertex_groups if g.name in bone_names])
        out.append(row)
    res = {"armature": arm.name, "mode": mode, "meshes": out}
    if warnings:
        res["warnings"] = warnings
    return res


@command("pose_bones", undo=True)
def pose_bones(p):
    """Pose several bones at once ({bone: {location?, rotation? deg, scale?}}), optionally keyed at a frame."""
    arm = _armature(_p(p, "armature", required=True))
    pose = _p(p, "pose") or {}
    frame = _p(p, "frame")
    key = bool(_p(p, "key", frame is not None))
    _ensure_object_mode()
    act = _use_action(arm, _p(p, "action")) if key else None
    if _p(p, "reset", False):
        for pb in arm.pose.bones:
            pb.location = (0, 0, 0)
            pb.rotation_quaternion = (1, 0, 0, 0)
            pb.rotation_euler = (0, 0, 0)
            pb.rotation_axis_angle = (0, 0, 1, 0)
            pb.scale = (1, 1, 1)
    f = float(frame) if frame is not None else _scene().frame_current
    done = []
    for bone, vals in pose.items():
        pb = _target(arm, bone)
        props = []
        if vals.get("location") is not None:
            pb.location = _vec(vals["location"], name="location")
            props.append("location")
        if vals.get("rotation") is not None:
            props.append(_set_rotation(pb, vals["rotation"]))
        if vals.get("scale") is not None:
            pb.scale = _vec(vals["scale"], name="scale")
            props.append("scale")
        if key:
            for prop in props:
                pb.keyframe_insert(prop, frame=f)
        done.append(bone)
    if key:
        act = _obj_action(arm)[0] or act
        if act is not None:
            act.use_fake_user = True
        _refresh()
    else:
        bpy.context.view_layer.update()
    return {"armature": arm.name, "posed": done, "keyed_at": f if key else None,
            "action": act.name if key and act else None}


@command("set_shape_key", undo=True)
def set_shape_key(p):
    """Set (and optionally key) a shape key value; creates the key (and Basis) if missing."""
    ob = _obj(_p(p, "object", required=True))
    if ob.type != "MESH":
        raise BridgeError(f"'{ob.name}' is not a mesh")
    name = _p(p, "name", required=True)
    me = ob.data
    created = False
    if me.shape_keys is None or name not in me.shape_keys.key_blocks:
        if not _p(p, "create", True):
            raise BridgeError(f"'{ob.name}' has no shape key '{name}'")
        if me.shape_keys is None:
            ob.shape_key_add(name="Basis", from_mix=False)
        ob.shape_key_add(name=name, from_mix=bool(_p(p, "from_mix", False)))
        created = True
    kb = me.shape_keys.key_blocks[name]
    if _p(p, "value") is not None:
        kb.value = float(p["value"])
    if _p(p, "frame") is not None:
        kb.keyframe_insert("value", frame=float(p["frame"]))
        if me.shape_keys.animation_data and me.shape_keys.animation_data.action:
            me.shape_keys.animation_data.action.use_fake_user = True
    return {"object": ob.name, "shape_key": name, "value": kb.value, "created": created,
            "keys": [k.name for k in me.shape_keys.key_blocks],
            "note": "a new key starts identical to Basis - sculpt/edit it (or use execute_python) to give it a shape" if created else None}


# ------------------------------------------------------------------ preview

@command("render_animation_frames")
def render_animation_frames(p):
    """Contact sheet of N frames from a fixed camera that frames the whole motion."""
    objs = _objs(_p(p, "objects", required=True))
    render = []
    for o in objs:
        if o.type == "ARMATURE":
            render += [m for m in _deform_meshes(o) if m not in render]
        elif o not in render:
            render.append(o)
    if not render:
        raise BridgeError("Nothing visible to render: the armature has no skinned meshes (parent_with_weights)")
    sc = _scene()
    rng = _p(p, "frame_range")
    if not rng:
        anim = next((a for a in (_obj_action(o)[0] for o in objs) if a is not None), None) or \
            next((a for a in (_obj_action(o.parent)[0] for o in render if o.parent) if a is not None), None)
        rng = list(anim.frame_range) if anim else [sc.frame_start, sc.frame_end]
    a, b = float(rng[0]), float(rng[1])
    count = max(1, min(24, int(_p(p, "count", 8))))
    frames = sorted({int(round(a + (b - a) * k / max(1, count - 1))) for k in range(count)}) if count > 1 else [int(a)]
    size = max(96, min(768, int(_p(p, "size", 256))))
    view = _p(p, "view", "iso")
    if isinstance(view, list):
        view = (float(view[0]), float(view[1]))
    ctx = _window_ctx(need_view3d=True)
    saved = (sc.frame_current, sc.frame_subframe)
    try:
        pts = []
        for f in frames:  # framing that contains the whole motion, so travel is visible
            sc.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            for o in render:
                ev = o.evaluated_get(dg)
                pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
        mn, mx = Vector(map(min, *pts)), Vector(map(max, *pts))
        bounds = ((mn + mx) / 2, max((mx - mn).length / 2, 0.01))
        shots = imaging.render_rgba(ctx, render, [view] * len(frames), size, bool(_p(p, "isolate", True)),
                                    _p(p, "shading"), bool(_p(p, "wireframe", False)), bounds=bounds,
                                    before_view=lambda i: sc.frame_set(frames[i]))
    finally:
        sc.frame_set(saved[0], subframe=saved[1])
    sheet = imaging.contact_sheet([rgba for _, rgba in shots], max(1, min(int(_p(p, "columns", 4)), len(frames))))
    sheet.update({"frames": frames, "fps": round(sc.render.fps / sc.render.fps_base, 3)})
    return sheet


# ------------------------------------------------------------------ Unreal round trip

_BAD_NAME = re.compile(r"[^A-Za-z0-9_]")


def _actions_for(arm):
    bones = {b.name for b in arm.data.bones}
    return [a for a in bpy.data.actions if bones.intersection(_action_bones(a))]


def _issue(issues, level, msg, fix=None):
    d = {"level": level, "message": msg}
    if fix:
        d["fix"] = fix
    issues.append(d)


@command("validate_rig")
def validate_rig(p):
    """Pre-flight checks for taking a rig + animations into Unreal."""
    arm = _armature(_p(p, "armature", required=True))
    target_fps = float(_p(p, "target_fps", 30))
    deform_only = bool(_p(p, "deform_only", True))
    issues = []
    bones = list(arm.data.bones)
    deform = [b for b in bones if b.use_deform]
    exported = deform if deform_only else bones
    roots = [b for b in exported if b.parent is None or (deform_only and not _has_deform_ancestor(b))]
    if not bones:
        _issue(issues, "error", "Armature has no bones")
    if len(roots) > 1:
        _issue(issues, "error", f"{len(roots)} root bones ({', '.join(b.name for b in roots[:6])}); Unreal needs exactly one",
               "parent them all under a single 'root' bone at the origin")
    elif roots and roots[0].name.lower() != "root":
        _issue(issues, "info", f"Root bone is '{roots[0].name}'. Unreal's mannequin uses 'root' (needed for root motion "
                               f"and easy retargeting)")
    if arm.name == "Armature":
        _issue(issues, "info", "Armature object is named 'Armature': Unreal drops it, so no extra root bone")
    else:
        _issue(issues, "info", f"Armature object '{arm.name}' would become an extra root bone in Unreal; "
                               f"export_animation_for_unreal renames it to 'Armature' while exporting")
    sc_ = arm.matrix_world.to_scale()
    if any(abs(s - 1) > 1e-3 for s in sc_):
        _issue(issues, "error", f"Armature scale is {_r(sc_, 3)} - bone lengths and translation keys will be scaled in Unreal",
               "apply_transform on the armature (and its meshes) before animating, or re-import at scale 1")
    if any(abs(a) > 1e-4 for a in arm.matrix_world.to_euler()):
        _issue(issues, "warning", "Armature is rotated; the rotation is baked into the root",
               "apply_transform rotation on the armature")
    if arm.matrix_world.translation.length > 1e-4:
        _issue(issues, "warning", f"Armature is at {_r(arm.matrix_world.translation, 3)}, not the origin; the offset is "
                                  f"baked into every clip", "move it to 0,0,0")
    leaf = [b.name for b in bones if re.search(r"(_end|_End|_leaf|\.end)$", b.name)]
    if leaf:
        _issue(issues, "warning", f"{len(leaf)} leaf/end bones ({', '.join(leaf[:5])}) - leftovers from an FBX import",
               "delete them (they carry no weights); import_animation ignores leaf bones")
    bad = [b.name for b in exported if _BAD_NAME.search(b.name)]
    if bad:
        _issue(issues, "warning", f"Bone names with spaces/special characters: {', '.join(bad[:6])} (Unreal rewrites them, "
                                  f"which breaks matching on re-import)", "rename to letters, digits and _")
    nondeform = len(bones) - len(deform)
    if nondeform and deform_only:
        _issue(issues, "info", f"{nondeform} non-deform (control/IK) bones are left out; their effect is baked into the "
                               f"deform bones on export")
    meshes = _deform_meshes(arm)
    mesh_rows = []
    names = {b.name for b in deform}
    for m in meshes:
        un = _unweighted(m, names)
        gi = {g.index for g in m.vertex_groups if g.name in names}
        max_inf = max((sum(1 for g in v.groups if g.group in gi and g.weight > 0) for v in m.data.vertices), default=0)
        row = {"mesh": m.name, "vertices": len(m.data.vertices), "unweighted": un, "max_influences": max_inf}
        mesh_rows.append(row)
        if un:
            _issue(issues, "error", f"{m.name}: {un} vertices have no bone weights (they stay behind when the rig moves)",
                   "parent_with_weights mode=auto, or weight-paint them")
        if max_inf > 8:
            _issue(issues, "warning", f"{m.name}: up to {max_inf} bone influences per vertex; Unreal keeps 8 by default "
                                      f"(4 on mobile)", "limit total weights to 4-8")
        if m.parent != arm:
            _issue(issues, "warning", f"{m.name} is not parented to the armature", "parent_with_weights")
        if any(abs(s - 1) > 1e-3 for s in m.matrix_world.to_scale()):
            _issue(issues, "warning", f"{m.name} has unapplied scale", "apply_transform")
        stray = [g.name for g in m.vertex_groups if g.name not in {b.name for b in bones}]
        if stray:
            _issue(issues, "info", f"{m.name}: {len(stray)} vertex groups don't match a bone (ignored): {', '.join(stray[:5])}")
        if m.data.shape_keys:
            _issue(issues, "info", f"{m.name}: {len(m.data.shape_keys.key_blocks) - 1} shape keys export as morph targets")
    if not meshes:
        _issue(issues, "warning", "No meshes are skinned to this armature (animation-only export is still possible)")
    sc = _scene()
    fps = sc.render.fps / sc.render.fps_base
    if abs(fps - target_fps) > 1e-3:
        _issue(issues, "warning", f"Scene is {fps:g} fps, target is {target_fps:g}; Unreal resamples the clips "
                                  f"(timing is kept, but keys no longer land on frames)", f"set_timeline fps={target_fps:g} before animating")
    if abs(sc.unit_settings.scale_length - 1) > 1e-6:
        _issue(issues, "warning", f"Scene unit scale is {sc.unit_settings.scale_length}; the Unreal preset expects 1.0")
    acts = _actions_for(arm)
    all_bones = {b.name for b in bones}
    act_rows = []
    for a in acts:
        ab = _action_bones(a)
        missing = [b for b in ab if b not in all_bones]
        act_rows.append({"name": a.name, "frame_range": _action_range(a), "bones": len(ab), "keys": _key_count(a)})
        if missing:
            _issue(issues, "warning", f"Action '{a.name}' animates {len(missing)} bones this rig doesn't have "
                                      f"({', '.join(missing[:4])})")
        if "|" in a.name:
            _issue(issues, "info", f"Action '{a.name}' has an FBX-import style name; it is exported as "
                                   f"'{_clip_name(a.name)}'")
        if not a.use_fake_user:
            _issue(issues, "warning", f"Action '{a.name}' has no fake user and is lost on save if unassigned",
                   "set_action or manage_action")
    if not acts:
        _issue(issues, "info", "No actions animate this rig yet")
    return {"ok": not any(i["level"] == "error" for i in issues), "armature": arm.name, "bones": len(bones),
            "deform_bones": len(deform), "root": roots[0].name if len(roots) == 1 else [b.name for b in roots],
            "meshes": mesh_rows, "actions": act_rows, "fps": round(fps, 3), "issues": issues}


def _has_deform_ancestor(b):
    p = b.parent
    while p is not None:
        if p.use_deform:
            return True
        p = p.parent
    return False


def _clip_name(name):
    return name.rsplit("|", 1)[-1]


def _safe(s):
    return "".join("_" if ch in '<>:"/\\|?* ' else ch for ch in s)


def _scale_bone_translation(act, f):
    for _, fc in _fcurves(act):
        m = _BONE_RE.match(fc.data_path)
        if m and m.group(2) == "location":
            for kp in fc.keyframe_points:
                kp.co.y *= f
                kp.handle_left.y *= f
                kp.handle_right.y *= f
            fc.update()


def _needs_bake(arm):
    """True when bones are moved by more than their own keys: active constraints (IK, Copy Rotation...) or
    drivers. The scaled export copy can't reproduce those, so such clips are baked to plain keys first."""
    if any(not c.mute and c.influence > 0 for pb in arm.pose.bones for c in pb.constraints):
        return True
    ad = arm.animation_data
    return bool(ad and any(fc.data_path.startswith("pose.bones") for fc in ad.drivers))


def _bake_clip(arm, rig, name, f0, f1, scale):
    """Sample the evaluated pose of the ORIGINAL rig (metres, constraints and drivers live) every frame and key
    the result on the export copy as plain local transforms (locations x scale). Returns the new action."""
    sc = _scene()
    samples = []
    for f in range(f0, f1 + 1):
        sc.frame_set(f)
        samples.append((f, [(pb.name, arm.convert_space(pose_bone=pb, matrix=pb.matrix, from_space="POSE",
                                                        to_space="LOCAL")) for pb in arm.pose.bones]))
    act = bpy.data.actions.new(name + "__mcp_baked")
    ad = rig.animation_data or rig.animation_data_create()
    ad.action = act
    pbs = rig.pose.bones
    for pb in pbs:
        pb.rotation_mode = "QUATERNION"
    prev = {}
    for f, rows in samples:
        for n, m in rows:
            pb = pbs[n]
            loc, rot, scl = m.decompose()
            if n in prev:
                rot.make_compatible(prev[n])  # no sign flips between frames
            prev[n] = rot
            pb.location, pb.rotation_quaternion, pb.scale = loc * scale, rot, scl
            for prop in ("location", "rotation_quaternion", "scale"):
                pb.keyframe_insert(prop, frame=f, group=n)
    return act


def _export_copies(arm, meshes, objs, datas, renamed, scale):
    """Temporary single-user copies of the rig ('Armature') and its meshes (original names), scaled about the
    origin with the scale applied. Originals are renamed out of the way and restored by the caller."""
    coll = _scene().collection
    rig = arm.copy()
    rig.data = arm.data.copy()
    datas.append(rig.data)
    rig.animation_data_clear()
    coll.objects.link(rig)
    objs.append(rig)
    if arm.name == "Armature":
        renamed.append((arm, arm.name))
        arm.name = "Armature__mcp_src"
    rig.name = "Armature"  # Unreal treats a node named 'Armature' as the armature itself: no extra root bone
    rig.parent = None
    rig.matrix_world = arm.matrix_world.copy()
    for pb in rig.pose.bones:
        for c in pb.constraints:
            if getattr(c, "target", None) == arm:
                c.target = rig
    copies = {}
    for m in meshes:
        mc = m.copy()
        others = [md for md in m.modifiers if md.type != "ARMATURE" and md.show_viewport]
        if others:  # bake the other modifiers first so the x100 scale can't change their result (bevel widths...)
            arms = [(md, md.show_viewport) for md in m.modifiers if md.type == "ARMATURE"]
            for md, _ in arms:
                md.show_viewport = False
            dg = bpy.context.evaluated_depsgraph_get()
            dg.update()
            me = bpy.data.meshes.new_from_object(m.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
            for md, v in arms:
                md.show_viewport = v
            mc.data = me
            for md in list(mc.modifiers):
                if md.type != "ARMATURE":
                    mc.modifiers.remove(md)
        else:
            mc.data = m.data.copy()
        datas.append(mc.data)
        coll.objects.link(mc)
        objs.append(mc)
        for md in mc.modifiers:
            if md.type == "ARMATURE":
                md.object = rig
        name = m.name
        renamed.append((m, name))
        m.name = name + "__mcp_src"
        mc.name = name
        mw = m.matrix_world.copy()
        mc.parent = rig
        mc.matrix_parent_inverse = rig.matrix_world.inverted()
        mc.matrix_world = mw
        copies[m] = mc
    if scale != 1:
        # scale about the world origin without operators: bones, mesh data (incl. shape keys) and the static pose
        S = Matrix.Scale(scale, 4)
        Si = S.inverted()
        worlds = {mc: mc.matrix_world.copy() for mc in copies.values()}
        rig.matrix_world = S @ rig.matrix_world @ Si  # location x100, rotation kept, no scale
        with _arm_edit(rig) as ebs:
            ends = {eb.name: (eb.head * scale, eb.tail * scale) for eb in ebs}  # read all first: moving a
            for eb in ebs:                                                     # connected head moves its parent's tail
                eb.head, eb.tail = ends[eb.name]
        for pb in rig.pose.bones:
            pb.location = pb.location * scale
        for mc, w in worlds.items():
            mc.data.transform(S, shape_keys=True)
            mc.matrix_parent_inverse = rig.matrix_world.inverted()
            mc.matrix_world = S @ w @ Si
        bpy.context.view_layer.update()
    return rig, copies


def _fbx(objs, path, anim, deform_only, scale_mode, triangulate=True, global_scale=1.0):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _selected_only(objs), bpy.context.temp_override(**_window_ctx()):
        bpy.ops.export_scene.fbx(
            filepath=path, use_selection=True, global_scale=global_scale, apply_unit_scale=True,
            object_types={"ARMATURE", "MESH"} if any(o.type == "MESH" for o in objs) else {"ARMATURE"},
            apply_scale_options=scale_mode, mesh_smooth_type="FACE", use_tspace=True, use_triangles=triangulate,
            use_mesh_modifiers=True, add_leaf_bones=False, primary_bone_axis="Y", secondary_bone_axis="X",
            use_armature_deform_only=deform_only, armature_nodetype="NULL",
            bake_anim=anim, bake_anim_use_all_bones=True, bake_anim_use_nla_strips=False,
            bake_anim_use_all_actions=False, bake_anim_force_startend_keying=True, bake_anim_step=1.0,
            bake_anim_simplify_factor=0.0)
    return os.path.getsize(path) if os.path.exists(path) else None


@command("export_animation_for_unreal")
def export_animation_for_unreal(p):
    """SK_<asset>.fbx (skinned mesh + skeleton, bind pose) and one A_<asset>_<clip>.fbx per action."""
    arm = _armature(_p(p, "armature", required=True))
    _ensure_object_mode()
    # not bpy.app.tempdir: Blender deletes that on exit, before the files are imported into Unreal
    folder = _p(p, "folder") or os.path.join(tempfile.gettempdir(), "BlenderMcp_Unreal")
    base = _p(p, "asset_name") or re.sub(r"^(SK_|Armature_?)", "", arm.name) or "Character"
    base = _safe(re.sub(r"^SK_", "", base))
    deform_only = bool(_p(p, "deform_only", True))
    scale_mode = str(_p(p, "scale_mode", "FBX_SCALE_NONE")).upper()
    include_mesh = bool(_p(p, "include_mesh", True))
    wanted = _p(p, "actions")
    acts = [bpy.data.actions.get(n) for n in wanted] if wanted else _actions_for(arm)
    if wanted and None in acts:
        raise BridgeError(f"Actions not found: {[n for n, a in zip(wanted, acts) if a is None]}")
    meshes = _deform_meshes(arm) if include_mesh else []
    if include_mesh and not meshes:
        raise BridgeError(f"No meshes are skinned to '{arm.name}' (parent_with_weights), or pass include_mesh=false")
    check = validate_rig({"armature": arm.name, "target_fps": _p(p, "target_fps", 30), "deform_only": deform_only})
    if not check["ok"] and not _p(p, "force", False):
        raise BridgeError("Rig has errors (pass force=true to export anyway): " +
                          "; ".join(i["message"] for i in check["issues"] if i["level"] == "error"))

    sc = _scene()
    units_cm = str(_p(p, "units", "cm")).lower() != "m"
    had_ad = arm.animation_data is not None
    ad = arm.animation_data or arm.animation_data_create()
    # assigning a clip marks it fake-user; export only borrows the clips, so put the flags back afterwards
    saved_fake = [(a, a.use_fake_user) for a in acts]
    saved = {"action": ad.action, "slot": getattr(ad, "action_slot", None), "use_nla": ad.use_nla,
             "range": (sc.frame_start, sc.frame_end), "frame": (sc.frame_current, sc.frame_subframe),
             "scene_name": sc.name}
    # clips are evaluated on the source rig too (root motion): put its pose back afterwards
    saved_pose = [(pb, pb.location.copy(), pb.rotation_quaternion.copy(), pb.rotation_euler.copy(),
                   tuple(pb.rotation_axis_angle), pb.scale.copy()) for pb in arm.pose.bones]
    files, clips = [], []
    temp_objs, temp_data, temp_acts, renamed = [], [], [], []
    try:
        ad.use_nla = False  # export exactly the clip, not NLA layers on top of it
        squatter = bpy.data.objects.get("Armature")
        if squatter is not None:
            renamed.append((squatter, squatter.name))
            squatter.name = "Armature__mcp_tmp"
        # Export temporary copies scaled to centimetres: bones, meshes and keys then arrive in
        # Unreal in its own units with a root bone of scale 1 (metres would put a x100 scale on the root bone).
        rig, copies = _export_copies(arm, meshes, temp_objs, temp_data, renamed, 100.0 if units_cm else 1.0)
        # the copies hold centimetre values in a metre scene: 0.01 x the exporter's 100 (m -> cm) = 1, so the
        # file is written 1:1 in centimetres (UnitScaleFactor 1, no scale on any node)
        gscale = 0.01 if units_cm else 1.0
        bake = _needs_bake(arm)
        if bake:
            # the baked keys already contain what the constraints did: the copy must not apply them again
            for pb in rig.pose.bones:
                for c in list(pb.constraints):
                    pb.constraints.remove(c)
        if include_mesh:
            rig.data.pose_position = "REST"
            sc.frame_set(sc.frame_current)
            path = os.path.join(folder, f"SK_{base}.fbx")
            size = _fbx([rig] + list(copies.values()), path, False, deform_only, scale_mode,
                        bool(_p(p, "triangulate", True)), gscale)
            files.append({"kind": "skeletal_mesh", "path": path, "bytes": size, "meshes": [m.name for m in meshes]})
            rig.data.pose_position = "POSE"
        fps = sc.render.fps / sc.render.fps_base
        for act in acts:
            _assign_action(arm, act)  # the original, for root-motion measurement
            a, b = act.frame_range
            sc.frame_start, sc.frame_end = int(math.floor(a)), int(math.ceil(b))
            if bake:
                cact = _bake_clip(arm, rig, act.name, sc.frame_start, sc.frame_end, 100.0 if units_cm else 1.0)
                temp_acts.append(cact)
            else:
                cact = act.copy()
                temp_acts.append(cact)
                if units_cm:
                    _scale_bone_translation(cact, 100.0)
                # the copy's slots keep the source's identifiers; pick the same one rather than the first suitable
                _assign_action(rig, cact, getattr(getattr(ad, "action_slot", None), "identifier", None))
            sc.frame_set(sc.frame_start)
            clip = _safe(re.sub(r"^A_", "", _clip_name(act.name)))
            sc.name = clip  # the FBX take is named after the scene: makes it 'Walk', not 'Scene'
            path = os.path.join(folder, f"A_{base}_{clip}.fbx")
            size = _fbx([rig], path, True, deform_only, scale_mode, True, gscale)
            n = sc.frame_end - sc.frame_start
            row = {"kind": "animation", "action": act.name, "path": path, "bytes": size,
                   "frames": [sc.frame_start, sc.frame_end], "seconds": round(n / fps, 3)}
            if bake:
                row["baked"] = "constraints/drivers baked to keys"
            rm = _root_motion(arm, act)
            if rm:
                row["root_travel_m"] = rm["root_travel_m"]
                if Vector(rm["root_travel_m"]).length > 0.01:
                    row["hint"] = "root bone moves: enable Root Motion on this AnimSequence in Unreal"
            files.append(row)
            clips.append(f"A_{base}_{clip}")
    finally:
        sc.name = saved["scene_name"]
        for o in temp_objs:
            bpy.data.objects.remove(o, do_unlink=True)
        for d in temp_data:
            if d.users == 0:
                (bpy.data.armatures if isinstance(d, bpy.types.Armature) else bpy.data.meshes).remove(d)
        for a_ in temp_acts:
            bpy.data.actions.remove(a_)
        for o, name in reversed(renamed):
            o.name = name
        ad.action = saved["action"]
        if saved["slot"] is not None and hasattr(ad, "action_slot"):
            try:
                ad.action_slot = saved["slot"]
            except Exception:
                pass
        ad.use_nla = saved["use_nla"]
        for a_, fake in saved_fake:
            a_.use_fake_user = fake
        for pb, loc, quat, eul, aa, scl in saved_pose:
            pb.location, pb.rotation_quaternion, pb.rotation_euler = loc, quat, eul
            pb.rotation_axis_angle, pb.scale = aa, scl
        sc.frame_start, sc.frame_end = saved["range"]
        if not had_ad:
            arm.animation_data_clear()
        sc.frame_set(saved["frame"][0], subframe=saved["frame"][1])

    ue_folder = _p(p, "unreal_folder") or f"/Game/Characters/{base}"
    out = {"asset": base, "folder": folder, "files": files, "fps": round(sc.render.fps / sc.render.fps_base, 3),
           "issues": [i for i in check["issues"] if i["level"] != "info"]}
    out["unreal_import"] = {
        "skeletal_mesh": f"{ue_folder}/SK_{base}", "skeleton": f"{ue_folder}/SK_{base}_Skeleton",
        "animations": [f"{ue_folder}/Animations/{c}" for c in clips],
        "python": _ue_python(files, ue_folder, base),
        "notes": "Import the skeletal mesh first; animations need its Skeleton. Run 'python' in the editor "
                 "(e.g. through the Unreal MCP's Python tool). Files are in centimetres with a scale-1 root bone; "
                 "the character faces +Y in Unreal like the mannequin. Unreal's 'invalid bind poses' warning on "
                 "the animation files is harmless (they carry no mesh).",
    }
    return out


def _ue_python(files, ue_folder, base):
    mesh = next((f["path"] for f in files if f["kind"] == "skeletal_mesh"), None)
    anims = [f["path"] for f in files if f["kind"] == "animation"]
    return "\n".join([
        "import unreal",
        "tools = unreal.AssetToolsHelpers.get_asset_tools()",
        "def task(src, dest, opts):",
        "    t = unreal.AssetImportTask(); t.filename = src; t.destination_path = dest",
        "    t.automated = True; t.save = True; t.replace_existing = True; t.options = opts",
        "    return t",
        "def ui(anim, skeleton=None):",
        "    o = unreal.FbxImportUI(); o.automated_import_should_detect_type = False",
        "    o.import_as_skeletal = True; o.import_materials = not anim; o.import_textures = not anim",
        "    o.import_mesh = not anim; o.import_animations = anim",
        "    o.mesh_type_to_import = unreal.FBXImportType.FBXIT_ANIMATION if anim else unreal.FBXImportType.FBXIT_SKELETAL_MESH",
        "    if skeleton: o.skeleton = skeleton",
        "    return o",
        f"mesh_src = {mesh!r}",
        f"anims = {anims!r}",
        f"if mesh_src: tools.import_asset_tasks([task(mesh_src, {ue_folder!r}, ui(False))])",
        f"skel = unreal.load_asset({f'{ue_folder}/SK_{base}_Skeleton'!r})",
        f"tools.import_asset_tasks([task(a, {ue_folder + '/Animations'!r}, ui(True, skel)) for a in anims])",
    ])


def _transfer_pose(src_arm, src_act, dst_arm, name):
    """Bake src_arm's animation (world-space bone poses) onto dst_arm as a new action, matching bones by name."""
    sc = _scene()
    _assign_action(src_arm, src_act)
    a, b = src_act.frame_range
    frames = range(int(math.floor(a)), int(math.ceil(b)) + 1)
    act = bpy.data.actions.get(name)
    if act is not None:
        bpy.data.actions.remove(act)
    act = bpy.data.actions.new(name)
    dad = dst_arm.animation_data or dst_arm.animation_data_create()
    saved = dad.action
    dad.action = None
    bones = [b for b in dst_arm.data.bones if b.name in src_arm.pose.bones]
    order = sorted(bones, key=lambda b: len(b.parent_recursive))  # parents first
    inv_dst = dst_arm.matrix_world.inverted()
    # world bone matrices carry the armature object's scale (e.g. 0.01 for a cm file): keep only real bone scale
    unit = src_arm.matrix_world.to_scale().x / max(1e-9, dst_arm.matrix_world.to_scale().x)
    saved_pose = {pb.name: (pb.location.copy(), pb.rotation_quaternion.copy(), pb.rotation_euler.copy(),
                            tuple(pb.rotation_axis_angle), pb.scale.copy()) for pb in dst_arm.pose.bones}
    prev_rot = {}
    keyed = []
    saved_frame = (sc.frame_current, sc.frame_subframe)
    try:
        for f in frames:
            sc.frame_set(f)
            want = {}  # desired armature-space pose matrices of the target
            for bone in order:
                l_, r_, s_ = (inv_dst @ src_arm.matrix_world @ src_arm.pose.bones[bone.name].matrix).decompose()
                want[bone.name] = Matrix.LocRotScale(l_, r_, s_ / unit)
            for bone in order:
                pb = dst_arm.pose.bones[bone.name]
                par = bone.parent
                if par is not None and par.name in want:
                    parent_space = want[par.name] @ par.matrix_local.inverted() @ bone.matrix_local
                elif par is not None:
                    parent_space = dst_arm.pose.bones[par.name].matrix @ par.matrix_local.inverted() @ bone.matrix_local
                else:
                    parent_space = bone.matrix_local
                basis = parent_space.inverted() @ want[bone.name]
                loc, rot, scl = basis.decompose()
                pb.location = loc
                pb.scale = scl
                if pb.rotation_mode == "QUATERNION":
                    q = prev_rot.get(bone.name)
                    if q is not None and q.dot(rot) < 0:
                        rot.negate()  # keep quaternion keys continuous
                    pb.rotation_quaternion = rot
                    prev_rot[bone.name] = rot.copy()
                    rprop = "rotation_quaternion"
                elif pb.rotation_mode == "AXIS_ANGLE":
                    ax, ang = rot.to_axis_angle()
                    pb.rotation_axis_angle = (ang, *ax)
                    rprop = "rotation_axis_angle"
                else:
                    e = rot.to_euler(pb.rotation_mode, prev_rot.get(bone.name))
                    pb.rotation_euler = e
                    prev_rot[bone.name] = e.copy()
                    rprop = "rotation_euler"
                if dad.action is None:
                    _assign_action(dst_arm, act)
                for prop in ("location", rprop, "scale"):
                    pb.keyframe_insert(prop, frame=f, group=bone.name)
            keyed.append(f)
    finally:
        for pb in dst_arm.pose.bones:  # don't leave the last frame behind in channels other clips don't key
            loc, q, e, aa, scl = saved_pose[pb.name]
            pb.location, pb.rotation_quaternion, pb.rotation_euler, pb.rotation_axis_angle, pb.scale = loc, q, e, aa, scl
        sc.frame_set(saved_frame[0], subframe=saved_frame[1])
    act.use_fake_user = True
    if saved is not None:
        dad.action = saved
    return act


@command("import_animation", undo=True)
def import_animation(p):
    """Import FBX animation (e.g. exported from Unreal). With target=<armature> the clips are moved onto that rig
    and the imported objects removed."""
    path = _p(p, "path", required=True)
    if not os.path.isfile(path):
        raise BridgeError(f"File not found: {path}")
    _ensure_object_mode()
    target = _armature(p["target"]) if _p(p, "target") else None
    before_objs, before_acts = set(bpy.data.objects), set(bpy.data.actions)
    # Blender's own ignore_leaf_bones drops every chain's last bone (head, hands, toes...), so it stays off;
    # only real end/leaf helper bones are removed below.
    with bpy.context.temp_override(**_window_ctx()):
        bpy.ops.import_scene.fbx(filepath=path, use_anim=True, ignore_leaf_bones=False,
                                 automatic_bone_orientation=bool(_p(p, "automatic_bone_orientation", False)),
                                 global_scale=float(_p(p, "scale", 1.0)), anim_offset=0.0)
    new_objs = [o for o in bpy.data.objects if o not in before_objs]
    new_acts = [a for a in bpy.data.actions if a not in before_acts]
    imp_arm = next((o for o in new_objs if o.type == "ARMATURE"), None)
    warnings, clips = [], []
    removed_leaf = []
    if imp_arm is not None:
        leaf = [b.name for b in imp_arm.data.bones if not b.children and re.search(r"(_end|_End|_leaf|\.end)$", b.name)]
        if leaf:
            with _arm_edit(imp_arm) as ebs:
                for n in leaf:
                    ebs.remove(ebs[n])
            removed_leaf = leaf
    start = _p(p, "start_frame", 1)
    for a in new_acts:
        name = _clip_name(a.name) if _p(p, "rename", True) else a.name
        a.name = (_p(p, "prefix") or "") + name  # one rename, so 'Walk' existing doesn't turn this into 'Walk.001'
        a.use_fake_user = True
        firsts = [fc.keyframe_points[0].co.x for _, fc in _fcurves(a) if fc.keyframe_points]
        if start is not None and firsts:
            first = min(firsts)
            delta = float(start) - math.floor(first + 1e-6)
            if delta:
                for _, fc in _fcurves(a):
                    for kp in fc.keyframe_points:
                        kp.co.x += delta
                        kp.handle_left.x += delta
                        kp.handle_right.x += delta
                    fc.update()
    out = {"imported_objects": [o.name for o in new_objs], "armature": imp_arm.name if imp_arm else None}
    if removed_leaf:
        out["leaf_bones_removed"] = removed_leaf
    if imp_arm is not None:
        out["bones"] = len(imp_arm.data.bones)
        out["armature_scale"] = _r(imp_arm.matrix_world.to_scale(), 4)
    if target is not None:
        if imp_arm is None:
            raise BridgeError("The file has no armature to take animation from")
        tb, ib = {b.name for b in target.data.bones}, {b.name for b in imp_arm.data.bones}
        match = len(tb & ib) / max(1, len(ib))
        out["bone_match"] = round(match, 3)
        if match < 0.9:
            warnings.append(f"Only {match:.0%} of the file's bones exist on {target.name} "
                            f"(missing e.g. {sorted(ib - tb)[:5]}); those channels do nothing - different skeletons "
                            f"need retargeting")
        # The file's rest pose generally differs from the target's (an animation-only FBX has no bind pose, and
        # Blender rebuilds bone rolls on import), so local keys can't be copied. Transfer the world-space pose
        # frame by frame instead - this also absorbs unit differences (cm vs m).
        baked = []
        for a in new_acts:
            if not _action_bones(a):  # shape-key / object actions in the file: nothing to put on the rig
                if not _p(p, "keep_imported", False):
                    bpy.data.actions.remove(a)  # their objects are removed below
                continue
            name = a.name
            a.name = name + "__src"
            baked.append(_transfer_pose(imp_arm, a, target, name))
            if not _p(p, "keep_imported", False):
                bpy.data.actions.remove(a)
            else:
                a.name = name + "_source"
        new_acts = baked
        if baked:
            _assign_action(target, baked[0])
        if not _p(p, "keep_imported", False):
            for o in new_objs:
                data = o.data
                bpy.data.objects.remove(o, do_unlink=True)
                if data is not None and getattr(data, "users", 1) == 0:
                    for coll in (bpy.data.meshes, bpy.data.armatures):
                        if data.name in coll and coll[data.name] == data:
                            coll.remove(data)
            out["imported_objects_removed"] = True
        out["target"] = target.name
    for a in new_acts:
        clips.append({"name": a.name, "frame_range": _action_range(a), "bones": len(_action_bones(a)), "keys": _key_count(a)})
    out["actions"] = clips
    if not new_acts:
        warnings.append("No animation found in the file")
    if warnings:
        out["warnings"] = warnings
    _refresh()
    return out
