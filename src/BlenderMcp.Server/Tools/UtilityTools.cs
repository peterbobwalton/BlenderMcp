using System.ComponentModel;
using System.Text.Json;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

[McpServerToolType]
public static class UtilityTools
{
    [McpServerTool(Name = "batch", Title = "Run several commands at once")]
    [Description("Run many bridge commands in ONE round trip and ONE undo step - much faster than separate tool calls when building something from many parts. Each item is {\"cmd\": \"<command>\", \"params\": {...}} using the snake_case command and parameter names (e.g. create_primitive with kind/name/location/rotation/scale, add_modifier with object/type/props, create_material with name/base_color/assign_to, transform_object, set_origin, generate_collision...). Call list_commands for the full list.")]
    public static async Task<string> Batch(
        BlenderConnection blender,
        [Description("Array of {cmd, params} objects.")] JsonElement commands,
        [Description("Stop at the first failure (default true).")] bool? stopOnError = null,
        [Description("Label for the undo history entry.")] string? undoLabel = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("batch", new Args
        {
            ["commands"] = commands, ["stop_on_error"] = stopOnError, ["undo_label"] = undoLabel,
        }, TimeSpan.FromMinutes(5), ct));

    [McpServerTool(Name = "list_commands", ReadOnly = true, Title = "List bridge commands")]
    [Description("Names of all commands the Blender bridge understands (for use inside batch).")]
    public static async Task<string> ListCommands(BlenderConnection blender, CancellationToken ct)
        => Results.Text(await blender.CallAsync("list_commands", ct: ct));

    [McpServerTool(Name = "execute_python", Destructive = true, OpenWorld = true, Title = "Run Python in Blender")]
    [Description("Escape hatch: run arbitrary bpy Python in Blender. Prefer the typed tools; use this only for things they cannot do. Assign to a variable named `result` to return JSON data; print() output is captured. bpy, bmesh, math, Vector, Matrix, Euler are pre-imported. Runs as one undo step.")]
    public static async Task<string> ExecutePython(BlenderConnection blender, string code, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("execute_python", new Args { ["code"] = code }, TimeSpan.FromMinutes(5), ct));

    [McpServerTool(Name = "undo", Title = "Undo")]
    [Description("Undo the last N steps in Blender. Every mutating tool call is its own undo step.")]
    public static async Task<string> Undo(BlenderConnection blender, int steps = 1, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("undo", new Args { ["steps"] = steps }, ct: ct));

    [McpServerTool(Name = "redo", Title = "Redo")]
    [Description("Redo the last N undone steps.")]
    public static async Task<string> Redo(BlenderConnection blender, int steps = 1, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("redo", new Args { ["steps"] = steps }, ct: ct));

    [McpServerTool(Name = "save_file", Title = "Save .blend")]
    [Description("Save the .blend file. With path: Save As (or save a copy when copy=true).")]
    public static async Task<string> SaveFile(BlenderConnection blender, string? path = null, bool? copy = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("save_file", new Args { ["path"] = path, ["copy"] = copy }, ct: ct));

    [McpServerTool(Name = "bridge_status", ReadOnly = true, Title = "Bridge status")]
    [Description("Check the connection to Blender and round-trip latency.")]
    public static async Task<string> BridgeStatus(BlenderConnection blender, CancellationToken ct)
    {
        var sw = System.Diagnostics.Stopwatch.StartNew();
        var pong = await blender.CallAsync("ping", ct: ct);
        return JsonSerializer.Serialize(new
        {
            connected = true,
            endpoint = $"{blender.Options.Host}:{blender.Options.Port}",
            blender = pong?["blender"]?.GetValue<string>(),
            roundTripMs = Math.Round(sw.Elapsed.TotalMilliseconds, 2),
        });
    }
}
