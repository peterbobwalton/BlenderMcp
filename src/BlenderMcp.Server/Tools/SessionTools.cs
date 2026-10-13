using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

[McpServerToolType]
public static class SessionTools
{
    [McpServerTool(Name = "blender_session", Title = "Which Blender this session uses")]
    [Description("Several Blenders can be open; the bridge gives each Claude session its own: the one whose open .blend is in or near this session's folder, else it starts Blender on the one .blend near the folder, else uses the only free Blender, else starts an empty one. A Blender another session uses is refused with 'Already in use'. " +
        "action=status: this session's Blender and why, every open Blender (file, port, who uses it) and every session. " +
        "action=select target=<port or part of the file name>: use that open Blender. " +
        "action=open path=<.blend or folder>: use the Blender that has it open, else start Blender on it (no path: an empty scene). " +
        "action=auto: back to matching by folder. action=release: give this session's Blender back so another session can use it.")]
    public static async Task<string> BlenderSession(
        BlenderConnection blender,
        [Description("status | select | open | auto | release")] string action = "status",
        [Description("select: a port (9877, 9850...) or part of the open file's name.")] string? target = null,
        [Description("open: a .blend file, or a folder holding one. Omit for an empty scene.")] string? path = null,
        CancellationToken ct = default)
    {
        if (!blender.Options.UseBridge)
        {
            return $"This server talks to one Blender directly ({blender.Options.Endpoint}, set by --port / BLENDER_MCP_PORT), " +
                "so there is nothing to choose. Remove --port from the MCP entry to let the bridge route between Blenders.";
        }
        var cmd = action.Trim().ToLowerInvariant() switch
        {
            "status" or "select" or "open" or "auto" or "release" => "bridge." + action.Trim().ToLowerInvariant(),
            _ => throw new BlenderException($"Unknown action '{action}': use status, select, open, auto or release."),
        };
        // open may start Blender and wait for it
        var timeout = cmd == "bridge.open" ? TimeSpan.FromSeconds(90) : (TimeSpan?)null;
        return Results.Text(await blender.CallAsync(cmd, new Args { ["target"] = target, ["path"] = path }, timeout, ct));
    }
}
