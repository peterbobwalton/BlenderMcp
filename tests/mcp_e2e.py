"""End-to-end test: drives BlenderMcp.Server.exe over real MCP stdio, exactly like Claude does.

Blender must be running with the 'Blender MCP Bridge (C#)' add-on started.
Usage:  python tests/mcp_e2e.py [path\\to\\BlenderMcp.Server.exe] [output_dir]
Builds a low-poly supply crate game asset, validates it, makes LODs + collision,
exports FBX/GLB, renders views, and checks error handling and undo.
Writes report.json and PNGs to the output dir. Exit code 0 = all checks passed.
"""

import base64
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "publish", "BlenderMcp.Server.exe")
OUT = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "tests", "out")
# Run against a throw-away Blender instead of your working session: start one with
#   blender --factory-startup --python tests/start_test_blender.py  (bridge on 9879), then set BLENDER_MCP_PORT=9879
PORT = int(os.environ.get("BLENDER_MCP_PORT", "9877"))
os.makedirs(OUT, exist_ok=True)

report = {"steps": [], "checks": [], "images": []}


class Mcp:
    def __init__(self, exe):
        self.p = subprocess.Popen([exe, "--port", str(PORT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.i = 0

    def _send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method, params=None):
        self.i += 1
        self._send({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            msg = json.loads(line)
            if msg.get("id") == self.i:
                return msg

    def notify(self, method):
        self._send({"jsonrpc": "2.0", "method": method})

    def call(self, tool, **args):
        t = time.perf_counter()
        msg = self.request("tools/call", {"name": tool, "arguments": args})
        ms = round((time.perf_counter() - t) * 1000, 1)
        if "error" in msg:
            step = {"tool": tool, "ms": ms, "rpc_error": msg["error"]}
            report["steps"].append(step)
            return step, None
        res = msg["result"]
        texts = [c["text"] for c in res["content"] if c["type"] == "text"]
        images = [c for c in res["content"] if c["type"] == "image"]
        data = None
        if texts and not res.get("isError"):
            try:
                data = json.loads(texts[0])
            except ValueError:
                data = texts
        step = {"tool": tool, "ms": ms, "isError": bool(res.get("isError")),
                "text": (texts[0][:300] if texts else None), "images": len(images)}
        report["steps"].append(step)
        labels = [t.replace("View: ", "") for t in texts if t.startswith("View: ")]
        for n, img in enumerate(images):
            label = labels[n] if n < len(labels) else (texts[0].split(" ")[0] if texts else str(n))
            label = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in label)[:40] or str(n)
            fn = f"{len(report['steps']):02d}_{tool}_{label.split(' ')[0]}.png"
            with open(os.path.join(OUT, fn), "wb") as f:
                f.write(base64.b64decode(img["data"]))
            report["images"].append(fn)
        return step, data

    def close(self):
        self.p.stdin.close()
        self.p.terminate()


def check(name, cond, detail=""):
    report["checks"].append({"check": name, "pass": bool(cond), "detail": str(detail)[:300]})


def main():
    m = Mcp(EXE)
    init = m.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "e2e", "version": "1"}})
    m.notify("notifications/initialized")
    check("initialize", init["result"]["serverInfo"]["name"] == "blender-csharp")
    tools = m.request("tools/list")["result"]["tools"]
    check("91 tools listed", len(tools) == 91, len(tools))
    check("render_views schema", any(t["name"] == "render_views" for t in tools))

    _, st = m.call("bridge_status")
    check("bridge connected", st and st["connected"], st)

    # Clean slate for re-runs
    for pat in ("Crate", "E2E_", "Box", "Ring", "Hull", "Prop", "Ground", "Stack", "GN_"):
        _, lst = m.call("list_objects", nameContains=pat, limit=1000)
        if lst and lst["count"]:
            m.call("delete_objects", names=[o["name"] for o in lst["objects"]])

    # --- Build a low-poly supply crate in ONE batch call -------------------
    C = "E2E_SupplyCrate"
    cmds = [
        {"cmd": "create_collection", "params": {"name": C}},
        {"cmd": "create_material", "params": {"name": "M_Crate_Wood", "base_color": [0.42, 0.26, 0.12], "roughness": 0.85}},
        {"cmd": "create_material", "params": {"name": "M_Crate_Metal", "base_color": [0.25, 0.27, 0.3], "metallic": 1.0, "roughness": 0.4}},
        {"cmd": "create_primitive", "params": {"kind": "cube", "name": "Crate", "size": 1.0, "location": [-5, 0, 0.5],
                                                "scale": [1.2, 0.8, 0.7], "collection": C, "material": "M_Crate_Wood"}},
        {"cmd": "add_modifier", "params": {"object": "Crate", "type": "BEVEL",
                                           "props": {"width": 0.03, "segments": 1, "limit_method": "ANGLE", "angle_limit": 30}}},
    ]
    for i, x in enumerate((-0.4, 0.4)):
        cmds.append({"cmd": "create_primitive", "params": {
            "kind": "cube", "name": f"Crate_Strap{i}", "size": 1.0, "location": [x - 5, 0, 0.5],
            "scale": [0.08, 0.84, 0.74], "collection": C, "material": "M_Crate_Metal"}})
    for i, y in enumerate((-0.42, 0.42)):
        cmds.append({"cmd": "create_primitive", "params": {
            "kind": "torus", "name": f"Crate_Handle{i}", "major_radius": 0.12, "minor_radius": 0.02,
            "segments": 8, "rings": 4, "location": [-5, y, 0.55], "rotation": [90, 0, 0], "collection": C,
            "material": "M_Crate_Metal"}})
    step, b = m.call("batch", commands=cmds, undoLabel="Build supply crate")
    check("batch built crate (9 commands, 1 round trip)", b and b["completed"] == len(cmds), step.get("text"))
    report["batch_ms"] = step["ms"]

    # Join parts via Python escape hatch (no typed join tool yet)
    step, j = m.call("execute_python", code=(
        "parts=[bpy.data.objects[n] for n in ('Crate','Crate_Strap0','Crate_Strap1','Crate_Handle0','Crate_Handle1')]\n"
        "ctx={'active_object':parts[0],'object':parts[0],'selected_objects':parts,'selected_editable_objects':parts}\n"
        "with bpy.context.temp_override(**ctx): bpy.ops.object.join()\n"
        "result=len(bpy.data.objects['Crate'].data.polygons)"))
    check("join parts", j and isinstance(j.get("result"), int), step.get("text"))

    _, v1 = m.call("validate_asset", object="Crate", triBudget=500, requireCollision=True, requireLightmapUv=True)
    codes1 = {i["code"] for i in v1["issues"]}
    check("validate finds unapplied scale", "unapplied_scale" in codes1, codes1)
    check("validate finds missing collision", "no_collision" in codes1, codes1)

    # --- Fix everything the validator reported ---------------------------
    m.call("apply_modifiers", object="Crate")
    m.call("apply_transform", objects=["Crate"])
    _, org = m.call("set_origin", object="Crate", where="bottom_center")
    _, det = m.call("get_object", name="Crate")
    check("origin at bottom of geometry", det and abs(det["world_bounds"]["min"][2] - det["location"][2]) < 1e-3,
          det and (det["location"], det["world_bounds"]))
    m.call("mesh_cleanup", object="Crate")
    m.call("set_shading", objects=["Crate"], mode="auto", angleDeg=35)
    m.call("uv_unwrap", object="Crate", method="smart")
    m.call("uv_unwrap", object="Crate", method="lightmap", uvLayer="Lightmap")
    _, col = m.call("generate_collision", object="Crate", kind="box")
    check("UBX collision", col and col["collision"] == "UBX_Crate_00", col)

    _, v2 = m.call("validate_asset", object="Crate", triBudget=500, requireCollision=True, requireLightmapUv=True)
    report["validate_after"] = v2
    check("asset passes validation after fixes", v2["ok"] and not any(
        i["severity"] in ("error", "warning") for i in v2["issues"]), v2["issues"])

    _, lods = m.call("generate_lods", object="Crate", ratios=[1, 0.5, 0.25], spacing=1.8)
    tris = [l["tris"] for l in lods["lods"]] if lods else []
    check("LODs strictly decreasing", len(tris) == 3 and tris[0] > tris[1] > tris[2], tris)

    out_dir = os.path.join(OUT, "export")
    _, fbx = m.call("export_asset", path=os.path.join(out_dir, "SM_SupplyCrate.fbx"), objects=["Crate"])
    check("FBX export includes collision", fbx and "UBX_Crate_00" in fbx["objects"] and fbx["bytes"] > 1000, fbx)
    _, glb = m.call("export_asset", path=os.path.join(out_dir, "SM_SupplyCrate"), objects=["Crate"], format="glb")
    check("GLB export", glb and glb["path"].endswith(".glb"), glb)
    _, be = m.call("batch_export", folder=os.path.join(out_dir, "lods"),
                   objects=[f"Crate_LOD{i}" for i in range(3)], prefix="SM_")
    check("batch export 3 LOD files", be and len(be["exported"]) == 3, be)

    # --- Eyes -------------------------------------------------------------
    s, _ = m.call("render_views", objects=["Crate"], views=["iso", "front", "top", "iso_back"], size=512, shading="SOLID")
    check("render_views returns 4 images", s["images"] == 4, s)
    s, _ = m.call("render_views", objects=[f"Crate_LOD{i}" for i in range(3)], views=["front"], size=768)
    check("LOD comparison image", s["images"] == 1, s)
    s, _ = m.call("focus_view", objects=["Crate"], view="FRONT")
    s, _ = m.call("viewport_screenshot", maxSize=1024)
    check("viewport screenshot", s["images"] == 1, s)
    s, _ = m.call("render_image", engine="BLENDER_EEVEE", width=640, height=360, objects=["Crate"])
    check("EEVEE render with auto camera", s["images"] == 1, s)

    # --- Errors & undo ----------------------------------------------------
    s, _ = m.call("get_object", name="Crat")
    check("helpful not-found error", s["isError"] and "Did you mean" in (s["text"] or ""), s["text"])
    s, _ = m.call("add_modifier", object="Crate", type="FOO")
    check("bad modifier type error lists valid types", s["isError"] and "BEVEL" in (s["text"] or ""), s["text"])
    m.call("create_primitive", kind="ico_sphere", name="E2E_UndoMe", collection=C)
    m.call("undo")
    _, gone = m.call("list_objects", nameContains="E2E_UndoMe")
    check("undo removes last creation", gone and gone["count"] == 0, gone)

    # --- Latency ----------------------------------------------------------
    lat = []
    for _ in range(20):
        s, _ = m.call("list_objects", type="MESH", limit=3)
        lat.append(s["ms"])
    report["list_objects_latency_ms"] = {"min": min(lat), "avg": round(sum(lat) / len(lat), 1), "max": max(lat)}
    check("avg tool round trip < 25 ms", sum(lat) / len(lat) < 25, report["list_objects_latency_ms"])

    _, stats = m.call("mesh_stats", objects=["Crate"] + [f"Crate_LOD{i}" for i in range(3)])
    report["mesh_stats"] = stats

    regressions(m, C)
    pipeline(m, C)
    modelling(m, C)
    layout(m, C)
    geonodes_and_context(m, C)
    animation(m, C)
    m.close()


def py(m, code):
    _, r = m.call("execute_python", code=code)
    return r and r.get("result")


def pipeline(m, C):
    """Wishlist features: join/separate/boolean, baking, sockets, naming, Unreal export."""
    X = 14  # build away from everything else
    m.call("create_primitive", kind="cube", name="Box", size=1.0, location=[X, 0, 0.5], collection=C)
    m.call("create_primitive", kind="cylinder", name="Drill", radius=0.2, depth=2, segments=16,
           location=[X, 0, 0.5], rotation=[90, 0, 0], collection=C)
    _, b = m.call("boolean", object="Box", cutters=["Drill"], operation="difference", cutterAction="delete")
    check("boolean cuts a hole", b and b["tris_after"] > b["tris_before"], b and (b["tris_before"], b["tris_after"]))
    _, gone = m.call("list_objects", nameContains="Drill")
    check("boolean deletes cutter", gone and gone["count"] == 0, gone)

    m.call("create_primitive", kind="torus", name="Ring", majorRadius=0.15, minorRadius=0.03, segments=12, rings=6,
           location=[X + 0.55, 0, 0.6], rotation=[0, 90, 0], collection=C)
    _, j = m.call("join_objects", objects=["Box", "Ring"])
    check("join_objects merges", j and j["name"] == "Box" and j["mesh"]["tris"] > 100, j and j.get("mesh"))
    m.call("duplicate_object", name="Box", newName="BoxSplit")
    _, sp = m.call("separate", object="BoxSplit", by="loose")
    check("separate by loose parts", sp and sp["parts"] == 2, sp)

    # bake: high = bevelled + subdivided copy
    m.call("duplicate_object", name="Box", newName="Box_High")
    m.call("add_modifier", object="Box_High", type="BEVEL", props={"width": 0.04, "segments": 3})
    m.call("add_modifier", object="Box_High", type="SUBSURF", props={"levels": 2})
    m.call("uv_unwrap", object="Box", method="smart")
    m.call("create_material", name="BoxWood", baseColor=[0.45, 0.28, 0.12], roughness=0.8, assignTo=["Box"])
    bake_dir = os.path.join(OUT, "bake")
    s, bk = m.call("bake_maps", low="Box", high=["Box_High"], maps=["normal", "ao"], size=256, folder=bake_dir)
    check("bake_maps writes normal + AO", bk and all(os.path.exists(x["path"]) for x in bk["maps"]), s.get("text"))
    stats = py(m, (
        "import numpy as np\nout={}\n"
        "for n in ('T_Box_N','T_Box_AO'):\n"
        "    im=bpy.data.images[n]; px=np.empty(len(im.pixels),dtype=np.float32); im.pixels.foreach_get(px)\n"
        "    out[n]=float(px.reshape(-1,4)[:,:3].mean())\n"
        "m=bpy.data.materials['BoxWood']; b=[n for n in m.node_tree.nodes if n.type=='BSDF_PRINCIPLED'][0]\n"
        "out['normal_linked']=b.inputs['Normal'].is_linked\nresult=out"))
    check("AO bake is not black (scene isolated, low-poly not occluding)", stats and stats["T_Box_AO"] > 0.3, stats)
    check("normal map is mostly flat blue and wired", stats and 0.4 < stats["T_Box_N"] < 0.8 and stats["normal_linked"], stats)

    # LODs, collision, socket, naming
    m.call("generate_lods", object="Box", ratios=[1, 0.5, 0.25], spacing=2)
    m.call("generate_collision", object="Box", kind="box")
    _, so = m.call("add_socket", object="Box", name="Top", location=[0, 0, 0.5])
    check("socket at top of box", so and abs(so["world_location"][2] - 1.0) < 1e-3, so)
    _, nm = m.call("check_naming", objects=["Box"])
    check("naming check flags Box and BoxWood", nm and {i["name"] for i in nm["issues"]} >= {"Box", "BoxWood"}, nm)
    _, nf = m.call("check_naming", objects=["Box"], fix=True)
    _, names = m.call("list_objects", nameContains="Box", limit=100)
    have = {o["name"] for o in names["objects"]} if names else set()
    check("naming fix renames mesh, LODs and collision together",
          {"SM_Box", "SM_Box_LOD1", "UBX_SM_Box_00"} <= have, sorted(have))

    s, ex = m.call("export_for_unreal", object="SM_Box", folder=os.path.join(OUT, "unreal"), unrealFolder="/Game/_BlenderMcpTest")
    check("export_for_unreal: one FBX with LOD group, collision and socket",
          ex and os.path.exists(ex["path"]) and len(ex["lods"]) == 3 and ex["collision"] == ["UBX_SM_Box_00"]
          and ex["sockets"] == ["SOCKET_Top"] and ex["unreal_import"]["arguments"]["combine_meshes"] is False,
          s.get("text"))
    _, after = m.call("get_object", name="SM_Box_LOD1")
    check("export_for_unreal restores LOD positions", after and abs(after["location"][0] - (X + 4)) < 1e-3,
          after and after["location"])


def modelling(m, C):
    """Wishlist 2 + 4: component modelling with face selectors, palette / vertex-colour painting."""
    X = 24
    m.call("create_primitive", kind="cube", name="Hull", size=1.0, location=[X, 0, 0.5], collection=C)
    _, fi = m.call("face_info", object="Hull", selector={"side": "top"})
    check("face_info: side=top matches 1 face", fi and fi["faces"] == 1 and abs(fi["centre"][2] - 1.0) < 1e-3, fi)

    _, ins = m.call("inset_faces", object="Hull", selector={"side": "top"}, thickness=0.1, depth=-0.05)
    check("inset adds a recessed panel", ins and ins["tris_after"] == ins["tris_before"] + 8, ins and (ins["tris_before"], ins["tris_after"]))
    _, ex = m.call("extrude_faces", object="Hull", selector={"side": "front"}, distance=0.2)
    check("extrude front face by 0.2 m", ex and abs(ex["dimensions"][1] - 1.2) < 1e-3, ex and ex["dimensions"])
    _, lc = m.call("loop_cut", object="Hull", axis="z", cuts=2)
    check("loop_cut adds loops", lc and lc["tris_after"] > lc["tris_before"], lc and (lc["tris_before"], lc["tris_after"]))
    _, bv = m.call("bevel_edges", object="Hull", width=0.02, segments=1, angleDeg=60)
    check("bevel_edges bevels sharp edges", bv and bv["beveled_edges"] > 0 and bv["tris_after"] > bv["tris_before"], bv and bv.get("beveled_edges"))
    _, before = m.call("face_info", object="Hull", selector={"side": "bottom", "angle_deg": 1})
    _, de = m.call("delete_faces", object="Hull", selector={"side": "bottom", "angle_deg": 1})
    check("delete_faces removes the hidden bottom", de and before and de["deleted_faces"] == before["faces"], de and de.get("deleted_faces"))
    s, bad = m.call("extrude_faces", object="Hull", selector={"side": "sideways"})
    check("bad selector is a clear error", s["isError"] and "side must be" in (s["text"] or ""), s.get("text"))

    # palette texturing
    pal_dir = os.path.join(OUT, "palette")
    _, pal = m.call("create_palette", name="E2EPal", colors=["#3A4A5C", "#C8A03C"], folder=pal_dir)
    check("create_palette saves PNG + material", pal and os.path.exists(pal["path"]) and pal["material"] == "M_E2EPal", pal)
    m.call("paint_faces", object="Hull", palette="E2EPal", index=0)
    _, top = m.call("paint_faces", object="Hull", palette="E2EPal", color="#C8A03C", selector={"side": "top", "angle_deg": 1})
    _, red = m.call("paint_faces", object="Hull", palette="E2EPal", color="#FF0000", selector={"side": "front", "angle_deg": 1})
    check("paint reuses existing swatch", top and top["index"] == 1 and not top["added_to_palette"], top)
    check("paint adds missing colour to palette", red and red["index"] == 2 and red["added_to_palette"], red)
    sampled = py(m, (
        "import bmesh\nfrom mathutils import Vector\n"
        "o=bpy.data.objects['Hull']; im=bpy.data.images['T_E2EPal']; w,h=im.size; px=list(im.pixels)\n"
        "def at(u,v):\n    x=min(w-1,int(u*w)); y=min(h-1,int(v*h)); k=(y*w+x)*4; return '#%02X%02X%02X'%tuple(round(c*255) for c in px[k:k+3])\n"
        "me=o.data; uv=me.uv_layers['UVMap'].data; out={}\n"
        "for poly in me.polygons:\n"
        "    n=(o.matrix_world.to_3x3() @ poly.normal).normalized()\n"
        "    key='top' if n.z>0.99 else ('front' if n.y<-0.99 else None)\n"
        "    if key and key not in out:\n        u,v=uv[poly.loop_start].uv; out[key]=at(u,v)\n"
        "out['slots']=[s.material.name for s in o.material_slots if s.material]\nresult=out"))
    check("palette UVs sample the painted colours", sampled and sampled.get("top") == "#C8A03C" and sampled.get("front") == "#FF0000",
          sampled)
    _, gp = m.call("get_palette", name="E2EPal")
    check("palette lists 3 colours", gp and len(gp["colors"]) == 3, gp)

    # vertex colours
    m.call("create_primitive", kind="cube", name="Hull_VC", size=1.0, location=[X + 2, 0, 0.5], collection=C)
    _, vc = m.call("paint_faces", object="Hull_VC", mode="vertex", color="#40A060", selector={"side": "top"})
    col = py(m, "a=bpy.data.objects['Hull_VC'].data.color_attributes['Col']\nresult=sorted(set(tuple(round(c,2) for c in list(d.color_srgb)[:3]) for d in a.data))")
    check("vertex colour stored as the given sRGB value", vc and vc["faces"] == 1 and col and [0.25, 0.63, 0.38] in [list(c) for c in col], col)
    s, _ = m.call("render_views", objects=["Hull", "Hull_VC"], views=["iso"], size=384, shading="MATERIAL")
    check("render palette-painted hull", s["images"] == 1, s)


def layout(m, C):
    """Scatter / align / distribute / drop, turntable, texel density, wireframe views, scene diff."""
    X = 32
    _, t0 = m.call("scene_changes")
    m.call("create_primitive", kind="plane", name="Ground", size=6, location=[X, 0, 0], collection=C)
    m.call("create_primitive", kind="cube", name="Prop", size=0.5, location=[X, 0, 3], collection=C)

    _, g = m.call("scatter_instances", source="Prop", mode="grid", grid=[3, 2], spacing=[1.0], origin=[X - 1, -0.5, 3],
                  collection="Prop_Grid")
    check("grid scatter makes 6 linked copies", g and g["count"] == 6 and g["linked"], g and g.get("count"))
    shared = py(m, "c=bpy.data.collections['Prop_Grid']\nresult=sorted({o.data.name for o in c.objects})")
    check("instances share one mesh", shared and len(shared) == 1, shared)
    _, dr = m.call("drop_to_floor", objects=g["objects"], ontoObjects=True)
    check("drop_to_floor lands copies on the ground", dr and all(abs(r["rests_at"]) < 1e-3 for r in dr["results"]),
          dr and [r["rests_at"] for r in dr["results"]])

    _, sc = m.call("scatter_instances", source="Prop", mode="surface", target="Ground", count=12, minDistance=0.6,
                   seed=3, randomYawDeg=45, scaleRange=[0.8, 1.2], collection="Prop_Scatter")
    pos = py(m, "c=bpy.data.collections['Prop_Scatter']\nresult=[list(o.location) for o in c.objects]")
    inside = pos and all(X - 3.001 <= p[0] <= X + 3.001 and -3.001 <= p[1] <= 3.001 and abs(p[2]) < 1e-3 for p in pos)
    spaced = pos and all(((a[0]-b[0])**2 + (a[1]-b[1])**2) ** 0.5 >= 0.599 for i, a in enumerate(pos) for b in pos[i+1:])
    check("surface scatter: 12 copies on the ground, min distance respected", sc and sc["count"] == 12 and inside and spaced,
          (sc and sc.get("count"), inside, spaced))

    for k, z in enumerate((0.0, 0.7, 1.9)):
        m.call("create_primitive", kind="cube", name=f"Stack{k}", size=0.4 + 0.2 * k, location=[X + 5 + k * 3, 0, z], collection=C)
    names = ["Stack0", "Stack1", "Stack2"]
    m.call("align_objects", objects=names, axis="z", to="min", value=0.0)
    mins = py(m, "import mathutils\nresult=[min((o.matrix_world @ mathutils.Vector(c)).z for c in o.bound_box) for o in [bpy.data.objects[n] for n in %r]]" % names)
    check("align_objects z min to 0", mins and all(abs(z) < 1e-4 for z in mins), mins)
    m.call("distribute_objects", objects=names, axis="x", gap=0.5)
    gaps = py(m, "import mathutils\nb=[(min((o.matrix_world @ mathutils.Vector(c)).x for c in o.bound_box), max((o.matrix_world @ mathutils.Vector(c)).x for c in o.bound_box)) for o in [bpy.data.objects[n] for n in %r]]\nresult=[round(b[i+1][0]-b[i][1],4) for i in range(2)]" % names)
    check("distribute_objects gap 0.5", gaps == [0.5, 0.5], gaps)

    s, _ = m.call("turntable", objects=["Stack1"], frames=6, size=128, columns=3)
    check("turntable returns one contact sheet", s["images"] == 1 and "6 angles" in (s["text"] or ""), s.get("text"))
    s, _ = m.call("render_views", objects=["Prop"], views=["iso"], size=256, wireframe=True)
    check("wireframe view with triangle count", s["images"] == 1 and "Triangles: 12" in (s["text"] or ""), s.get("text"))
    s, _ = m.call("render_views", objects=["Prop"], views=["isometric"])
    check("bad view still rejected", s["isError"], s.get("text"))

    _, td = m.call("texel_density", objects=["Prop"], textureSize=1024)
    base = td and td["objects"][0]
    check("texel density reported", base and base["texel_density_px_per_m"] > 0, base)
    m.call("transform_object", name="Stack2", scale=[9, 1, 1])
    _, td2 = m.call("texel_density", objects=["Stack2"], textureSize=1024)
    st = td2 and td2["objects"][0]
    check("stretched object flagged as uneven", st and st["spread"] > 2, st and st.get("spread"))

    _, diff = m.call("scene_changes", since=t0["token"])
    check("scene diff sees added objects", diff and {"Prop", "Ground", "Stack0"} <= set(diff["added"]), diff and diff["added"][:8])
    _, t1 = m.call("scene_changes")
    m.call("transform_object", name="Stack0", location=[X + 5, 2, 0])
    _, d2 = m.call("scene_changes", since=t1["token"])
    check("scene diff sees a moved object", d2 and d2["changed"] == ["Stack0"] and not d2["added"], d2)


def geonodes_and_context(m, C):
    X = 44
    _, lg = m.call("list_node_groups")
    ess = [g for groups in (lg or {}).get("essentials", {}).values() for g in groups]
    check("Essentials node groups listed", "Scatter on Surface" in ess, ess[:10])
    m.call("create_primitive", kind="plane", name="GN_Ground", size=6, location=[X, 0, 0], collection=C)
    m.call("create_primitive", kind="cube", name="GN_Rock", size=0.3, location=[X, 0, 5], collection=C)
    _, gn = m.call("add_geometry_nodes", object="GN_Ground", group="Scatter on Surface",
                   inputs={"Density": 3, "Object": "GN_Rock", "Seed": 7, "Randomize Scale": 0.3, "Bogus": 1})
    check("add_geometry_nodes from Essentials with inputs", gn and gn["group"] == "Scatter on Surface"
          and any(i["name"] == "Object" and i.get("value") == "GN_Rock" for i in gn["inputs"]), gn and gn.get("warnings"))
    check("unknown GN input reported, not fatal", gn and any("Bogus" in w for w in gn.get("warnings", [])), gn and gn.get("warnings"))
    count_code = ("dg=bpy.context.evaluated_depsgraph_get(); ob=bpy.data.objects['GN_Ground']\n"
                  "result=sum(1 for i in dg.object_instances if i.is_instance and i.parent and i.parent.original==ob)")
    n1 = py(m, count_code)
    check("scatter density 3 on 36 m2 = 108 instances", n1 == 108, n1)
    m.call("set_geometry_nodes_inputs", object="GN_Ground", inputs={"Density Method": "Amount", "Amount": 40})
    n2 = py(m, count_code)
    check("menu input + amount: exactly 40 instances", n2 == 40, n2)

    _, none = m.call("get_user_context", clear=True)
    m.call("get_user_context", clear=True)
    py(m, "import sys\nU=[v for k,v in sys.modules.items() if k.endswith('blender_mcp_bridge.user_context')][0]\n"
          "bpy.context.view_layer.objects.active=bpy.data.objects['GN_Rock']; bpy.data.objects['GN_Rock'].select_set(True)\n"
          "result=U.capture(bpy.context, 'make this rock rounder')")
    s, ctx = m.call("get_user_context")
    check("Send to Claude context: note, selection and viewport image",
          s["images"] == 1 and "make this rock rounder" in (s["text"] or "") and "GN_Rock" in (s["text"] or ""), s.get("text", "")[:200])
    py(m, "bpy.data.objects['GN_Rock'].select_set(False)\nresult=1")


def animation(m, C):
    """Keyframes, presets, clips, rigging, previews and the Unreal FBX round trip."""
    X = 60
    py(m, "for a in list(bpy.data.actions):\n"
          "    if a.name.split('.')[0] in ('Walk','Idle','A_Spin','Hinge','Run','Run2','RT_Walk','RT_Idle') or a.name.startswith(('Armature','E2E')):\n"
          "        bpy.data.actions.remove(a)\nresult=1")
    _, tl = m.call("set_timeline", fps=30, frameStart=1, frameEnd=60, frame=1)
    check("set_timeline 30 fps", tl and tl["fps"] == 30 and tl["frame_end"] == 60, tl)

    # props: presets + keys
    m.call("create_primitive", kind="cube", name="E2E_Pickup", size=0.4, location=[X, 0, 1], collection=C)
    _, sp = m.call("animate_preset", object="E2E_Pickup", preset="spin", period=60, action="A_Spin")
    _, bob = m.call("animate_preset", object="E2E_Pickup", preset="bob", amount=0.2, period=60)
    val = py(m, "o=bpy.data.objects['E2E_Pickup']; s=bpy.context.scene; s.frame_set(31)\n"
                "result=[round(math.degrees(o.rotation_euler.z),2), round(o.location.z,3)]; s.frame_set(91)\n"
                "result+= [round(math.degrees(o.rotation_euler.z),2)]; s.frame_set(1)")
    check("spin preset: 180 deg at half period, keeps turning when looped (offset cycles)",
          val and abs(val[0] - 180) < 0.5 and abs(val[2] - 540) < 0.5, val)
    check("bob preset: +0.2 m at half period", val and abs(val[1] - 1.2) < 1e-3, val)
    check("presets share the named clip", sp and bob and sp["action"] == "A_Spin" == bob["action"], (sp, bob))

    m.call("create_primitive", kind="cube", name="E2E_Door", size=1, scale=[0.5, 0.05, 1], location=[X + 2, 0, 1], collection=C)
    _, ik = m.call("insert_keyframes", object="E2E_Door", action="Hinge", interpolation="LINEAR",
                   keys=[{"frame": 1, "rotation": [0, 0, 0]}, {"frame": 30, "rotation": [0, 0, 90]},
                         {"frame": 45, "rotation": [0, 0, 90], "location": [X + 2, 0, 1]}])
    v = py(m, "o=bpy.data.objects['E2E_Door']; s=bpy.context.scene; s.frame_set(15)\nresult=round(math.degrees(o.rotation_euler.z),3); s.frame_set(1)")
    check("insert_keyframes with LINEAR interpolation", ik and ik["action"] == "Hinge" and v is not None
          and abs(v - 90 * 14 / 29) < 0.01, (ik, v))
    _, dk = m.call("delete_keyframes", object="E2E_Door", channels=["location"])
    _, ga = m.call("get_animation", object="E2E_Door", includeKeys=True)
    ch = (ga or {}).get("channels", {}).get("<object>", {})
    check("delete_keyframes removes a channel; get_animation lists the rest",
          dk and dk["removed"] == 3 and "location" not in ch and ch.get("rotation_euler", {}).get("keys") == 3, (dk, ch))
    _, si = m.call("set_interpolation", object="E2E_Door", interpolation="BEZIER", easing="EASE_IN_OUT", loop=True)
    _, ga = m.call("get_animation", object="E2E_Door")
    check("set_interpolation loop", si and si["fcurves"] == 3 and ga["channels"]["<object>"]["rotation_euler"].get("loop"), si)

    # rig: humanoid template + simple body + auto weights
    _, arm = m.call("create_armature", name="E2E_Rig", template="humanoid", height=1.8)
    check("humanoid armature: 23 bones, single root", arm and arm["bones"] == 23 and arm["roots"] == ["root"]
          and abs(arm["height_m"] - 1.8) < 0.03, arm)
    parts = [("E2E_Torso", "cube", dict(size=1, scale=[0.36, 0.22, 0.55], location=[0, 0, 1.2])),
             ("E2E_Head", "ico_sphere", dict(radius=0.12, subdivisions=2, location=[0, 0, 1.66])),
             ("E2E_LegL", "cylinder", dict(radius=0.07, depth=0.9, segments=12, location=[0.09, 0, 0.5])),
             ("E2E_LegR", "cylinder", dict(radius=0.07, depth=0.9, segments=12, location=[-0.09, 0, 0.5])),
             ("E2E_ArmL", "cylinder", dict(radius=0.05, depth=0.62, segments=12, location=[0.48, 0, 1.43], rotation=[0, 90, 0])),
             ("E2E_ArmR", "cylinder", dict(radius=0.05, depth=0.62, segments=12, location=[-0.48, 0, 1.43], rotation=[0, 90, 0]))]
    for name, kind, kw in parts:
        m.call("create_primitive", kind=kind, name=name, collection=C, **kw)
    m.call("join_objects", objects=[p[0] for p in parts], newName="E2E_Body")
    m.call("apply_transform", objects=["E2E_Body"])
    _, pw = m.call("parent_with_weights", objects=["E2E_Body"], armature="E2E_Rig", mode="auto")
    check("auto weights: every vertex weighted (misses filled from nearest bone)", pw and pw["meshes"][0]["unweighted_vertices"] == 0, pw)
    _, vr = m.call("validate_rig", armature="E2E_Rig")
    check("validate_rig ok for a clean humanoid", vr and vr["ok"] and vr["root"] == "root" and vr["deform_bones"] == 23,
          vr and [i for i in vr["issues"] if i["level"] != "info"])

    # clips
    m.call("set_action", object="E2E_Rig", action="Walk")
    for f, a in ((1, 30), (16, -30), (31, 30)):
        m.call("pose_bones", armature="E2E_Rig", frame=f,
               pose={"thigh_l": {"rotation": [a, 0, 0]}, "thigh_r": {"rotation": [-a, 0, 0]},
                     "upperarm_l": {"rotation": [0, 0, -60]}, "upperarm_r": {"rotation": [0, 0, 60]}})
    m.call("insert_keyframes", object="E2E_Rig", bone="root", interpolation="LINEAR",
           keys=[{"frame": 1, "location": [0, 0, 0]}, {"frame": 31, "location": [0, -1.5, 0]}])
    _, ga = m.call("get_animation", object="E2E_Rig")
    rm = (ga or {}).get("root_motion", {})
    check("root motion measured (1.5 m forward)", rm.get("root_bone") == "root" and abs(rm["root_travel_m"][1] + 1.5) < 1e-3, rm)
    m.call("set_action", object="E2E_Rig", action="Idle")
    m.call("pose_bones", armature="E2E_Rig", frame=1, pose={"spine_01": {"rotation": [0, 0, 0]}})
    m.call("pose_bones", armature="E2E_Rig", frame=40, pose={"spine_01": {"rotation": [5, 0, 0]}})
    _, la = m.call("list_actions")
    acts = {a["name"]: a for a in (la or {}).get("actions", [])}
    check("list_actions: clips with ranges", acts.get("Walk", {}).get("frame_range") == [1, 31]
          and acts.get("Idle", {}).get("frame_range") == [1, 40] and acts["Walk"]["fake_user"], {k: v.get("frame_range") for k, v in acts.items()})

    m.call("set_action", object="E2E_Rig", action="Walk")
    s, fr = m.call("render_animation_frames", objects=["E2E_Rig"], count=6, view="right", size=192, columns=6)
    check("render_animation_frames contact sheet", s.get("images") == 1, s.get("text"))
    _, sk = m.call("set_shape_key", object="E2E_Body", name="Breathe", value=0.5, frame=1)
    check("set_shape_key creates Basis + key", sk and sk["created"] and sk["keys"] == ["Basis", "Breathe"], sk)

    # Unreal round trip through FBX
    folder = os.path.join(OUT, "unreal_anim")
    _, ex = m.call("export_animation_for_unreal", armature="E2E_Rig", folder=folder, actions=["Walk", "Idle"], assetName="Hero")
    files = (ex or {}).get("files", [])
    names = [os.path.basename(f["path"]) for f in files]
    check("export: SK + one FBX per clip", names == ["SK_Hero.fbx", "A_Hero_Walk.fbx", "A_Hero_Idle.fbx"]
          and all(f["bytes"] for f in files), names)
    check("export reports root motion on Walk", files and files[1].get("hint", "").startswith("root bone moves"), files[1:2])
    st = py(m, "o=bpy.data.objects.get('E2E_Rig'); s=bpy.context.scene\n"
               "result=[o is not None, o.animation_data.action.name, s.name, s.frame_start, s.frame_end, 'Armature' in bpy.data.objects]")
    check("export restores name, action, scene and range", st == [True, "Walk", "Scene", 1, 31, False] or st == [True, "Walk", "Scene", 1, 60, False], st)
    check("export returns an Unreal import script", "import_asset_tasks" in ((ex or {}).get("unreal_import", {}).get("python", "")))

    _, sk_in = m.call("import_animation", path=os.path.join(folder, "SK_Hero.fbx"), keepImported=True)
    check("skeleton in FBX: 23 bones, no extra root, no leaf bones", sk_in and sk_in.get("bones") == 23, sk_in)
    roots = py(m, f"a=bpy.data.objects['{(sk_in or {}).get('armature')}']\nresult=[b.name for b in a.data.bones if b.parent is None]") if sk_in else None
    check("FBX root bone is 'root'", roots == ["root"], roots)
    h = py(m, "import mathutils\nms=[bpy.data.objects[n] for n in %r if bpy.data.objects[n].type=='MESH']\n"
              "result=round(max((o.matrix_world @ mathutils.Vector(c)).z for o in ms for c in o.bound_box),3)" % (sk_in or {}).get("imported_objects", []))
    bl = py(m, f"a=bpy.data.objects['{(sk_in or {}).get('armature')}']; t=bpy.data.objects['E2E_Rig']\n"
               "result=round(max(((a.matrix_world @ a.data.bones[b.name].head_local)-(t.matrix_world @ b.head_local)).length for b in t.data.bones),5)") if sk_in else None
    check("bone positions survive the cm export (connected chains too)", bl is not None and bl < 1e-3, bl)
    check("FBX is in centimetres but the character keeps its size (1.78 m)", sk_in and abs(sk_in["armature_scale"][0] - 0.01) < 1e-4
          and h is not None and abs(h - 1.78) < 0.01, (sk_in and sk_in.get("armature_scale"), h))
    names_ok = py(m, "result=[n for n in ('E2E_Body','E2E_Rig') if n in bpy.data.objects] + [o.name for o in bpy.data.objects if '__mcp' in o.name]")
    check("export leaves no temp objects and original names", names_ok == ["E2E_Body", "E2E_Rig"], names_ok)
    if sk_in:
        m.call("delete_objects", names=sk_in["imported_objects"])
    _, rt = m.call("import_animation", path=os.path.join(folder, "A_Hero_Walk.fbx"), target="E2E_Rig", prefix="RT_")
    check("import onto rig: full bone match, clip named after the take", rt and rt.get("bone_match") == 1.0
          and [a["name"] for a in rt["actions"]] == ["RT_Walk"], rt)
    cmp = py(m, "import mathutils\n"
                "o=bpy.data.objects['E2E_Rig']; s=bpy.context.scene; ad=o.animation_data\n"
                "def pos(act, f):\n"
                "    ad.action=bpy.data.actions[act]\n"
                "    if hasattr(ad,'action_slot') and ad.action_slot is None and ad.action_suitable_slots: ad.action_slot=ad.action_suitable_slots[0]\n"
                "    s.frame_set(f); return [(o.matrix_world @ o.pose.bones[b].head) for b in ('foot_l','hand_r','root')]\n"
                "err=0\n"
                "for f in (1, 9, 16, 24, 31):\n"
                "    a=pos('Walk', f); b=pos('RT_Walk', f); err=max(err, max((x-y).length for x,y in zip(a,b)))\n"
                "ad.action=bpy.data.actions['Walk']; s.frame_set(1)\nresult=round(err,5)")
    check("round trip: re-imported clip matches the original pose (< 1 mm)", cmp is not None and cmp < 0.001, cmp)

    sc_err = py(m, "import sys\nA=[v for k,v in sys.modules.items() if k.endswith('blender_mcp_bridge.animation')][0]\n"
                   "a=bpy.data.actions['RT_Walk']\n"
                   "keys=max((abs(kp.co.y-1) for _,fc in A._fcurves(a) if fc.data_path.endswith('.scale') for kp in fc.keyframe_points), default=0)\n"
                   "pose=max(abs(v-1) for pb in bpy.data.objects['E2E_Rig'].pose.bones for v in pb.scale)\nresult=[round(keys,5), round(pose,5)]")
    check("imported clip has no stray bone scale (cm file onto m rig) and the rig's pose is untouched",
          sc_err is not None and sc_err[0] < 1e-3 and sc_err[1] < 1e-4, sc_err)
    _, du = m.call("manage_action", action="Walk", operation="duplicate", newName="Run")
    _, rn = m.call("manage_action", action="Run", operation="rename", newName="Run2")
    _, de = m.call("manage_action", action="Run2", operation="delete")
    check("manage_action duplicate/rename/delete", du and rn and de and de["deleted"] == "Run2", (du, rn, de))
    m.call("set_timeline", fps=24)


def regressions(m, C):
    """Checks for bugs found in code review (each failed before its fix)."""
    # convex hull must be a clean closed convex mesh: tris == 2V - 4
    _, hull = m.call("generate_collision", object="Crate", kind="convex", maxVerts=40)
    check("convex hull is a clean hull (tris == 2V-4)", hull and hull["tris"] == 2 * hull["verts"] - 4, hull)

    # angle arrays are degrees
    m.call("create_primitive", kind="cube", name="E2E_Rot", collection=C, location=[-9, 0, 1])
    _, sp = m.call("set_property", object="E2E_Rot", path="rotation_euler", value=[0, 0, 90])
    check("set_property rotation_euler takes degrees", sp and abs(sp["value"][2] - 1.5708) < 1e-3, sp)

    # summaries reflect the change just made
    _, tr = m.call("transform_object", name="E2E_Rot", scale=[2, 1, 1], rotation=[0, 0, 0])
    check("transform_object reports updated dimensions", tr and abs(tr["dimensions"][0] - 4.0) < 1e-3, tr and tr["dimensions"])

    # partial apply (location + scale, keep rotation) must not move the geometry
    m.call("transform_object", name="E2E_Rot", location=[-9, 2, 1], rotation=[0, 0, 30], scale=[2, 1, 0.5])
    before = py(m, "o=bpy.data.objects['E2E_Rot']\nresult=[list(o.matrix_world @ v.co) for v in o.data.vertices]")
    m.call("apply_transform", objects=["E2E_Rot"], location=True, rotation=False, scale=True)
    after = py(m, "o=bpy.data.objects['E2E_Rot']\nresult=[list(o.matrix_world @ v.co) for v in o.data.vertices]")
    moved = max(abs(a - b) for va, vb in zip(before or [], after or []) for a, b in zip(va, vb)) if before and after else 99
    check("apply_transform(location+scale) keeps geometry in place", moved < 1e-4, moved)

    # torus gets real UVs
    m.call("create_primitive", kind="torus", name="E2E_Torus", collection=C, location=[-9, -3, 1])
    uv_span = py(m, "me=bpy.data.objects['E2E_Torus'].data\nus=[d.uv[0] for d in me.uv_layers[0].data]\nresult=max(us)-min(us)")
    check("torus has non-degenerate UVs", uv_span and uv_span > 0.9, uv_span)

    # lightmap unwrap keeps the texturing UV active
    _, uvr = m.call("uv_unwrap", object="E2E_Torus", method="lightmap", uvLayer="Lightmap")
    check("uv_unwrap into Lightmap keeps UVMap active", uvr and uvr["active"] == "UVMap" and uvr["written"] == "Lightmap", uvr)

    # collision prefix must not match other assets that merely share a prefix
    m.call("create_primitive", kind="cube", name="E2E_Rot2", collection=C, location=[-12, 0, 1])
    m.call("generate_collision", object="E2E_Rot2", kind="box")
    _, va = m.call("validate_asset", object="E2E_Rot", requireCollision=True)
    check("validate does not borrow UBX_E2E_Rot2 for E2E_Rot", va and va["collision"] == [], va and va["collision"])

    # a batch with a bad step changes nothing
    s, _ = m.call("batch", commands=[{"cmd": "create_primitive", "params": {"kind": "cube", "name": "E2E_BatchNo"}},
                                     {"cmd": "no_such_command"}])
    _, lst = m.call("list_objects", nameContains="E2E_BatchNo")
    check("invalid batch is rejected before running", s["isError"] and lst and lst["count"] == 0, s.get("text"))

    # bad render view names are rejected instead of silently rendering iso
    s, _ = m.call("render_views", objects=["Crate"], views=["isometric"])
    check("unknown view name rejected", s["isError"] and "Valid" in (s["text"] or ""), s.get("text"))

    # cycles sample count is restored after render_image
    smp0 = py(m, "result=bpy.context.scene.cycles.samples")
    m.call("render_image", engine="CYCLES", samples=1, width=64, height=64, objects=["Crate"])
    smp1 = py(m, "result=bpy.context.scene.cycles.samples")
    check("render_image restores cycles samples", smp0 == smp1, (smp0, smp1))

    # malformed requests must not kill the bridge
    import socket as _s
    try:
        k = _s.create_connection(("127.0.0.1", PORT), timeout=5)
        f = k.makefile("rb")
        k.sendall(b"[1,2,3]\n")
        bad = json.loads(f.readline())
        k.sendall(b'{"id":7,"cmd":"ping"}\n')
        pong = json.loads(f.readline())
        k.close()
        check("bridge survives non-object JSON", not bad["ok"] and pong["ok"], (bad, pong))
    except Exception as ex:
        check("bridge survives non-object JSON", False, ex)

    # a second listener cannot steal the port
    try:
        t = _s.socket()
        t.setsockopt(_s.SOL_SOCKET, _s.SO_REUSEADDR, 1)
        t.bind(("127.0.0.1", PORT))
        t.close()
        check("port is bound exclusively", False, "second bind succeeded")
    except OSError:
        check("port is bound exclusively", True)

    # Blender-side restart: the server must reconnect on the next call
    m.call("execute_python", code=(
        "import sys\n"
        "S=[v for k,v in sys.modules.items() if k.endswith('blender_mcp_bridge.server')][0]\n"
        f"def _bounce():\n    S.stop(); S.start('127.0.0.1', {PORT})\n"
        "bpy.app.timers.register(_bounce, first_interval=0.3)"))
    time.sleep(1.5)
    s, st = m.call("bridge_status")
    check("reconnects after bridge restart", st and st.get("connected"), s.get("text"))
    s, lst = m.call("list_objects", nameContains="E2E_Rot")
    check("tool calls work after reconnect", lst and lst["count"] >= 1, s.get("text"))


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        import traceback
        report["exception"] = traceback.format_exc()
    report["passed"] = sum(c["pass"] for c in report["checks"])
    report["total"] = len(report["checks"])
    with open(os.path.join(OUT, "report.json"), "w") as f:
        json.dump(report, f, indent=1)
    print(f"{report['passed']}/{report['total']} checks passed")
    sys.exit(0 if report["passed"] == report["total"] and "exception" not in report else 1)
