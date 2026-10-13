"""Throw-away Blender for tests/bridge_test.py: loads the repo's add-on, listens on the first free bridge port
(9877, 9880-9899) and announces itself, then saves its empty scene as the .blend given after '--'.

    blender --factory-startup --python tests/bridge_blender.py -- C:\\temp\\a\\a.blend
"""
import os
import sys

import bpy

repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blender_addon")
sys.path.insert(0, repo)

import blender_mcp_bridge.server as server  # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
if argv:
    os.makedirs(os.path.dirname(os.path.abspath(argv[0])), exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(argv[0]))
port = server.start("127.0.0.1", 9877, fallback=True)
print(f"[BlenderMCP bridge test] {bpy.data.filepath or 'unsaved'} on port {port}")
