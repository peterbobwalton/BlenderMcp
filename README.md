# BlenderMcp — C# MCP server for Blender

A .NET 10 MCP server (official `ModelContextProtocol` C# SDK, stdio) that drives a live Blender
session through a small Python add-on. Built for game-asset work (Unreal-friendly defaults).

```
Claude / any MCP client ──stdio──▶ BlenderMcp.Server (C#) ──TCP 127.0.0.1:9877──▶ blender_mcp_bridge add-on ──▶ bpy (main thread)
```

## Why it's better than a raw-Python bridge

| | |
|---|---|
| **91 typed tools** | create/transform objects, modifiers, PBR materials, UVs, origin, shading, cleanup — no hand-written `bpy` for everyday work (an `execute_python` escape hatch is still there) |
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

Close Blender first, restart Claude Desktop afterwards. Silent: `BlenderMcpSetup-1.6.0.exe /VERYSILENT /TASKS=claude [/PORT=9877]`.
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
other Blender MCP add-ons). Status, request counts and a start/stop button are in
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
      "args": ["--port", "9877"]
    }
  }
}
```

Claude Code: `claude mcp add blender-csharp -- C:\source\Claude\BlenderMcp\publish\BlenderMcp.Server.exe`

Options: `--host`, `--port`, `--timeout <seconds>` or env `BLENDER_MCP_HOST/PORT/TIMEOUT`.

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
$env:BLENDER_MCP_PORT = 9879
& $blender --factory-startup --python tests\start_test_blender.py     # leave it open
& "C:\Program Files\Blender Foundation\Blender 5.2\5.2\python\bin\python.exe" tests\mcp_e2e.py "$env:APPDATA\BlenderMcp\server\BlenderMcp.Server.exe"
```

It builds and bakes a game asset, makes LODs/collision/sockets, exports for Unreal, renders views, and runs
animates and rigs a test character and round-trips its clips through FBX, and runs
regression checks (bad requests, reconnects, exclusive port, convex hulls, degrees, AO occlusion…).
Report and images go to `tests\out`. Avoid port 9878 - the UnrealMcp bridge uses it.

## Adding a tool

1. Python: add a function in `handlers.py` decorated with `@command("my_cmd", undo=True)`.
2. C#: add a static method with `[McpServerTool]` that calls `blender.CallAsync("my_cmd", new Args { ... })`.
3. Reload the add-on (disable/enable) and rebuild.

## Wire protocol

Newline-delimited JSON over TCP.
Request `{"id":1,"cmd":"list_objects","params":{"type":"MESH"}}` →
response `{"id":1,"ok":true,"result":{...},"ms":0.4}` or `{"id":1,"ok":false,"error":"..."}`.
Only localhost is bound by default; the bridge executes code (`execute_python`), so don't expose the port.
