# BlenderMcp — C# MCP server for Blender

A .NET 10 MCP server (official `ModelContextProtocol` C# SDK, stdio) that drives a live Blender
session through a small Python add-on. Built for game-asset work (Unreal-friendly defaults).

```
Claude session ──stdio──▶ BlenderMcp.Server (C#) ──TCP──▶ bridge service (127.0.0.1:9870) ──TCP 9877 / 9850-9869──▶ blender_mcp_bridge add-on ──▶ bpy (main thread)
```

Several Blenders and several Claude sessions can run at once: the bridge service gives each session its own Blender,
picked by the .blend it has open (see [Several Blenders](#several-blenders-17)).

## Why it's better than a raw-Python bridge

| | |
|---|---|
| **92 typed tools** | create/transform objects, modifiers, PBR materials, UVs, origin, shading, cleanup — no hand-written `bpy` for everyday work (an `execute_python` escape hatch is still there) |
| **Animation → Unreal** | `validate_rig` (single root, extra root bone, leaf bones, weights, fps…), `export_animation_for_unreal` (SK_ mesh + one A_ FBX per clip in centimetres — root bone scale 1, no extra root, verified bone-for-bone in UE 5.8 — plus an Unreal import script), `import_animation` (clips baked back onto your rig), keyframes, presets, armatures, `render_animation_frames` |
| **Game-asset pipeline** | `mesh_stats`, `validate_asset` (scale, UVs, lightmap UVs, n-gons, non-manifold, budget, collision…), `decimate` to a triangle target, `generate_lods`, `generate_collision` (UCX_/UBX_/USP_), `export_asset` / `batch_export` with an Unreal FBX preset |
| **Eyes** | `render_views` = offscreen OpenGL renders of just the objects you name, auto-framed from any angle (~10 ms/view); `viewport_screenshot`; `render_image` with an automatic framing camera |
| **Fast & reliable** | one persistent multiplexed socket (100 pipelined calls ≈ 25 ms), main-thread work queue with a per-tick time budget so Blender's UI stays responsive, per-call timeouts, auto-reconnect, `batch` = many commands in one round trip |
| **Safe** | every mutating call is exactly one Blender undo step; `undo`/`redo` tools; selection and render settings are restored after exports/renders; errors come back as clear messages ("Object 'Crat' not found. Did you mean: Crate?") |

## Install (recommended)

Run `BlenderMcpSetup-<version>.exe` (build it with `installer\build-installer.ps1`). Per-user, no admin rights:

- server → `%APPDATA%\BlenderMcp\server\BlenderMcp.Server.exe` (self-contained, no .NET needed)
- add-on → installed **and enabled** in each Blender 4.2+ you tick, under
  `%APPDATA%\Blender Foundation\Blender\<ver>\extensions\user_default\blender_mcp_bridge`
- Claude Desktop → `blender-csharp` added to `claude_desktop_config.json` (classic and Microsoft Store builds)

Close Blender first, restart Claude Desktop afterwards. Silent: `BlenderMcpSetup-1.7.0.exe /VERYSILENT /TASKS=claude`
(`/PORT=9877` ties the entry to that one Blender instead of using the bridge).
Uninstall (Settings ▸ Apps) removes the add-on and the Claude config entry.

## Manual setup

### 1. Blender add-on (Blender 4.2+, tested on 5.2)

In Blender: **Edit ▸ Preferences ▸ Get Extensions ▸ ⌄ ▸ Install from Disk…** and pick
`blender_mcp_bridge.zip` from the repo root.

To rebuild the zip after changing the add-on (the zip must contain the folder):

```powershell
Compress-Archive -Path blender_addon\blender_mcp_bridge -DestinationPath blender_mcp_bridge.zip -Force
```

It starts listening on `127.0.0.1:9877` automatically (port 9877 so it can run alongside
other Blender MCP add-ons); a second Blender takes the next free port of 9850-9869. Status, request counts and a start/stop button are in
**3D Viewport ▸ Sidebar (N) ▸ MCP**.

### 2. Build the server

Open `BlenderMcp.slnx` in Visual Studio 2026 and build, or:

```powershell
dotnet publish src\BlenderMcp.Server -c Release -o publish
```

Check it can reach Blender:

```powershell
publish\BlenderMcp.Server.exe --selftest
```

### 3. Register it with your MCP client

Claude Desktop / Cowork (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "blender-csharp": {
      "command": "C:\\source\\Claude\\BlenderMcp\\publish\\BlenderMcp.Server.exe",
      "args": []
    }
  }
}
```

Claude Code: `claude mcp add blender-csharp -- C:\source\Claude\BlenderMcp\publish\BlenderMcp.Server.exe`

Options: `--timeout <seconds>` (`BLENDER_MCP_TIMEOUT`). `--port` / `--host` (`BLENDER_MCP_PORT` / `BLENDER_MCP_HOST`)
skip the bridge and talk to that one Blender directly. Bridge settings (env of the MCP entry): `BLENDER_MCP_WORKDIR`
(`;`-separated folders to route by instead of the working folder), `BLENDER_MCP_OWNER` (the name other sessions see),
`BLENDER_MCP_BRIDGE_PORT` (service port, default 9870), `BLENDER_MCP_BLENDER` (the blender.exe it starts).

## Several Blenders (1.7)

Modelled on VsMcp's bridge. Three kinds of process:

- **Blender** with the add-on listens on 9877, or the next free port of 9850-9869 when another Blender has it
  (up to 21 Blenders). It announces itself in `%LOCALAPPDATA%\BlenderMcp\instances\<port>.json` (port, process id,
  open .blend; rewritten when a file is loaded or saved, deleted when Blender exits) and `ping` returns the same.
- **The bridge service** (`BlenderMcp.Server.exe --service`) is the one routing table on the PC, on `127.0.0.1:9870`.
  The first session starts it (detached, so it holds none of Claude's pipes); a second copy cannot bind the port and
  exits; it exits itself after 10 minutes without sessions. Log: `%LOCALAPPDATA%\BlenderMcp\bridge.log` (with the
  Microsoft Store build of Claude Desktop: `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Local\BlenderMcp\`).
- **Sessions**: each Claude session starts its own `BlenderMcp.Server.exe` (stdio, in the session's working folder)
  with all the tools. It connects to the service, says who it is, and its commands are forwarded to its Blender.
  The session ends when that process exits (Claude closed the session), and its Blender is free again.

To find each session's Blender the service asks every Blender which file it has open (ping on the ports, plus the
announcement files for a Blender too busy to answer), then:

1. keeps the session's Blender while it runs (sticky, also when Claude saves the scene under another name);
2. picks the Blender whose open .blend is in the session's folder (or up to three levels below it, or the session's
   folder is up to three levels below the .blend's);
3. **one session per Blender**: when that Blender is routed to another session the call is refused with
   `Already in use: the Blender with X (port P) is routed to another Claude session ('<owner>', folder F, since hh:mm)`;
4. no Blender has a matching file: it **starts Blender** on the one .blend in the folder (then up to three folders up,
   then two down); several .blend files give an error naming them;
5. no .blend near the folder (e.g. Claude Desktop, which has no project folder): it uses the only Blender no other
   session is using (or the only free one with an unsaved scene), else names the open ones, else starts an empty Blender.

The `blender_session` tool shows and changes this: `status` (this session's Blender and why, every Blender with its
file and who uses it, every session), `select target=<port or file name>`, `open path=<.blend or folder>` (uses the
Blender that has it open, else starts one), `auto` (back to matching by folder), `release` (give the Blender back; the session then has none until `auto`, `select` or `open`).
`BlenderMcp.Server.exe --list` prints the same status from the current folder. `--port` (or `BLENDER_MCP_PORT`) still
talks to one Blender directly, without the bridge (the e2e test does).

## Tools

**Scene** get_scene_info · list_objects · get_object · select_objects · list_materials
**Objects** create_primitive · create_empty · create_light · create_camera · transform_object · set_object_properties · set_property · delete_objects · duplicate_object · create_collection
**Modifiers** add_modifier · set_modifier · remove_modifier · apply_modifiers
**Materials** create_material · set_material · assign_material · add_image_texture
**Mesh** mesh_cleanup · set_shading · apply_transform · set_origin · uv_unwrap
**Modelling** join_objects · separate · boolean · face_info · extrude_faces · inset_faces · bevel_edges · loop_cut · delete_faces
**Low-poly colour** create_palette · paint_faces · get_palette
**Layout** scatter_instances · align_objects · distribute_objects · drop_to_floor · scene_changes
**Review** turntable · texel_density (and render_views wireframe=true)
**Geometry Nodes** list_node_groups · add_geometry_nodes · set_geometry_nodes_inputs (incl. Blender's Essentials: Scatter on Surface, Array, Randomize Transforms…)
**Your context** get_user_context — press **Send to Claude** in the MCP sidebar (with an optional note) and ask Claude to look at it
**Animation** insert_keyframes · delete_keyframes · set_interpolation · animate_preset (spin, bob, sway, pulse, bounce, follow_path) · set_timeline · set_action · manage_action · list_actions · get_animation · render_animation_frames
**Rigging** create_armature (humanoid template with Unreal mannequin bone names) · parent_with_weights (auto / rigid…) · pose_bones · set_shape_key
**Unreal animation** validate_rig · export_animation_for_unreal · import_animation
**Baking** bake_maps
**Unreal** export_for_unreal · add_socket · check_naming
**Game assets** mesh_stats · validate_asset · decimate · generate_lods · generate_collision · export_asset · batch_export · import_file
**Visuals** viewport_screenshot · render_views · render_image · focus_view
**Utility** batch · list_commands · execute_python · undo · redo · save_file · bridge_status

## Tests

The end-to-end test drives the installed server over real MCP stdio. Run it against a throw-away
Blender so it never touches your working scene:

```powershell
$blender = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
$env:BLENDER_MCP_PORT = 9849
& $blender --factory-startup --python tests\start_test_blender.py     # leave it open
& "C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe" tests\mcp_e2e.py "$env:APPDATA\BlenderMcp\server\BlenderMcp.Server.exe"
```

It builds and bakes a game asset, makes LODs/collision/sockets, exports for Unreal, renders views, and runs
animates and rigs a test character and round-trips its clips through FBX, and runs
regression checks (bad requests, reconnects, exclusive port, convex hulls, degrees, AO occlusion…).
Report and images go to `tests\out`. Avoid ports 9878-9899: the UnrealMcp bridges use them.

The bridge test starts two throw-away Blenders on temp .blend files and several sessions against its own service
port (9871), and checks routing by file, "Already in use", status, select and release (`--launch` also checks that the
bridge starts Blender on a .blend; that uses your normal Blender profile, so install the 1.7 add-on first):

```powershell
$env:BLENDER = "C:\Program Files\Blender Foundation\Blender 5.2\blender.exe"
python tests\bridge_test.py publish\BlenderMcp.Server.exe [--launch]
```

## Adding a tool

1. Python: add a function in `handlers.py` decorated with `@command("my_cmd", undo=True)`.
2. C#: add a static method with `[McpServerTool]` that calls `blender.CallAsync("my_cmd", new Args { ... })`.
3. Reload the add-on (disable/enable) and rebuild.

## Wire protocol

Newline-delimited JSON over TCP.
Request `{"id":1,"cmd":"list_objects","params":{"type":"MESH"}}` →
response `{"id":1,"ok":true,"result":{...},"ms":0.4}` or `{"id":1,"ok":false,"error":"..."}`.
Only localhost is bound by default; the bridge executes code (`execute_python`), so don't expose the port.
The bridge service speaks the same protocol, plus `bridge.hello` (first line: session, folders, owner) and
`bridge.status` / `bridge.select` / `bridge.open` / `bridge.auto` / `bridge.release`.
