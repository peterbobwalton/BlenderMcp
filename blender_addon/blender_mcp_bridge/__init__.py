"""Blender MCP Bridge - socket bridge for the BlenderMcp C# MCP server."""

bl_info = {
    "name": "Blender MCP Bridge (C#)",
    "author": "Pete",
    "version": (1, 6, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > MCP",
    "description": "Local TCP bridge used by the BlenderMcp C# MCP server",
    "category": "Interface",
}

import bpy

from . import handlers, imaging, server, user_context  # noqa: F401


def _prefs():
    return bpy.context.preferences.addons[__package__].preferences


class MCPBRIDGE_Prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    host: bpy.props.StringProperty(name="Host", default="127.0.0.1")
    port: bpy.props.IntProperty(name="Port", default=9877, min=1024, max=65535)
    auto_start: bpy.props.BoolProperty(name="Start automatically", default=True)

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "host")
        col.prop(self, "port")
        col.prop(self, "auto_start")


class MCPBRIDGE_OT_start(bpy.types.Operator):
    bl_idname = "mcpbridge.start"
    bl_label = "Start MCP Bridge"

    def execute(self, context):
        p = _prefs()
        try:
            server.start(p.host, p.port)
        except OSError as ex:
            self.report({"ERROR"}, f"Could not listen on {p.host}:{p.port}: {ex}")
            return {"CANCELLED"}
        return {"FINISHED"}


class MCPBRIDGE_OT_stop(bpy.types.Operator):
    bl_idname = "mcpbridge.stop"
    bl_label = "Stop MCP Bridge"

    def execute(self, context):
        server.stop()
        return {"FINISHED"}


class MCPBRIDGE_OT_send(bpy.types.Operator):
    """Hand Claude your current selection, viewport image and note (ask Claude to 'look at what I sent')"""
    bl_idname = "mcpbridge.send_to_claude"
    bl_label = "Send to Claude"

    def execute(self, context):
        n = user_context.capture(context, context.window_manager.mcp_note)
        self.report({"INFO"}, f"Sent {n} selected object(s) + viewport to Claude - now ask Claude to look at it")
        return {"FINISHED"}


class MCPBRIDGE_PT_panel(bpy.types.Panel):
    bl_label = "MCP Bridge"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "MCP"

    def draw(self, context):
        lay = self.layout
        p = _prefs()
        running = server.is_running()
        row = lay.row()
        row.label(text=f"{'Running' if running else 'Stopped'}  {p.host}:{p.port}",
                  icon="CHECKMARK" if running else "X")
        lay.operator("mcpbridge.stop" if running else "mcpbridge.start",
                     icon="PAUSE" if running else "PLAY")
        if running:
            s = server.stats()
            col = lay.column(align=True)
            col.label(text=f"Clients: {s['clients']}")
            col.label(text=f"Requests: {s['requests']}  Errors: {s['errors']}")
            col.label(text=f"Last: {s['last_cmd']}")
        box = lay.box()
        box.label(text="Ask Claude about this", icon="EYEDROPPER")
        box.prop(context.window_manager, "mcp_note", text="")
        box.operator("mcpbridge.send_to_claude", icon="EXPORT")
        lay.prop(p, "port")
        lay.prop(p, "auto_start")


_classes = (MCPBRIDGE_Prefs, MCPBRIDGE_OT_start, MCPBRIDGE_OT_stop, MCPBRIDGE_OT_send, MCPBRIDGE_PT_panel)


def _auto_start():
    try:
        p = _prefs()
        if p.auto_start and not server.is_running():
            server.start(p.host, p.port)
    except Exception as ex:  # port in use, etc.
        print(f"[BlenderMCP] auto-start failed: {ex}")
    return None


def register():
    for c in _classes:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.mcp_note = bpy.props.StringProperty(
        name="Note for Claude", description="What should Claude look at or change?", default="")
    bpy.app.timers.register(_auto_start, first_interval=1.0)


def unregister():
    if bpy.app.timers.is_registered(_auto_start):
        bpy.app.timers.unregister(_auto_start)  # don't start a server after we're gone
    server.stop()
    for c in reversed(_classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.WindowManager, "mcp_note"):
        del bpy.types.WindowManager.mcp_note
