"""Start a throw-away Blender for the e2e tests, so they never touch your working scene.

    blender --factory-startup --python tests/start_test_blender.py
    $env:BLENDER_MCP_PORT=9879; python tests/mcp_e2e.py

Loads the add-on as installed in your user extensions folder (falls back to the repo copy;
set BLENDER_MCP_FROM_REPO=1 to test the repo copy before installing)
and starts the bridge on port 9879 (or $BLENDER_MCP_PORT).
"""
import os
import sys

import bpy

port = int(os.environ.get("BLENDER_MCP_PORT", "9879"))
ver = ".".join(bpy.app.version_string.split(".")[:2])
installed = os.path.join(os.environ.get("APPDATA", ""), "Blender Foundation", "Blender", ver, "extensions", "user_default")
repo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "blender_addon")
use_repo = os.environ.get("BLENDER_MCP_FROM_REPO") or not os.path.isdir(os.path.join(installed, "blender_mcp_bridge"))
sys.path.insert(0, repo if use_repo else installed)

import blender_mcp_bridge.server as server  # noqa: E402

server.start("127.0.0.1", port)
print(f"[BlenderMCP test] bridge from {sys.path[0]} on port {port}")
