using System.ComponentModel;
using System.Text.Json;
using System.Text.Json.Nodes;
using BlenderMcp.Server.Bridge;
using Microsoft.Extensions.AI;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Geometry Nodes as procedural, non-destructive modifiers.</summary>
[McpServerToolType]
public static class GeoNodesTools
{
    [McpServerTool(Name = "list_node_groups", ReadOnly = true, Title = "List Geometry Nodes groups")]
    [Description("Geometry Nodes groups in the file (with their inputs) plus the ones bundled in Blender's Essentials library, e.g. 'Scatter on Surface', 'Randomize Transforms', 'Array', 'Displace Geometry', 'Smooth by Angle', 'Curve to Tube'.")]
    public static async Task<string> ListNodeGroups(BlenderConnection blender, bool? includeEssentials = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("list_node_groups", new Args { ["include_essentials"] = includeEssentials }, ct: ct));

    [McpServerTool(Name = "add_geometry_nodes", Title = "Add Geometry Nodes")]
    [Description("Add a Geometry Nodes modifier to an object using a node group from the file or Blender's Essentials (appended automatically), and set its inputs by display name, e.g. group='Scatter on Surface', inputs={\"Density\": 5, \"Instance\": \"SM_Rock\"}. Objects/collections/materials are given by name. Returns all inputs with current values. Keep it live (procedural) or bake it with apply_modifiers.")]
    public static async Task<string> AddGeometryNodes(
        BlenderConnection blender,
        string @object,
        [Description("Node group name.")] string group,
        [Description("Input values by input name. Angle and Rotation inputs are in radians (Blender's node units), unlike the degrees used by the other tools.")] Dictionary<string, JsonElement>? inputs = null,
        [Description("Modifier name (default: group name).")] string? name = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("add_geometry_nodes", new Args
        {
            ["object"] = @object, ["group"] = group, ["inputs"] = inputs, ["name"] = name,
        }, ct: ct));

    [McpServerTool(Name = "set_geometry_nodes_inputs", Title = "Set Geometry Nodes inputs")]
    [Description("Change inputs of an existing Geometry Nodes modifier by input name (first GN modifier if 'modifier' is omitted). Angle and Rotation inputs are in radians (Blender's node units), unlike the degrees used by the other tools.")]
    public static async Task<string> SetGeometryNodesInputs(
        BlenderConnection blender, string @object, Dictionary<string, JsonElement> inputs, string? modifier = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_geometry_nodes_inputs", new Args
        {
            ["object"] = @object, ["inputs"] = inputs, ["modifier"] = modifier,
        }, ct: ct));
}

/// <summary>Context the user hands over from Blender's sidebar ("Send to Claude").</summary>
[McpServerToolType]
public static class UserContextTools
{
    [McpServerTool(Name = "get_user_context", ReadOnly = true, Title = "What the user sent from Blender")]
    [Description("Fetch what the user sent with the 'Send to Claude' button in Blender's MCP sidebar: their note, selected objects (with details), active object, mode, and an image of their viewport at that moment. Call this when the user says things like 'look at what I sent', 'fix this', 'this one'.")]
    public static async Task<IEnumerable<AIContent>> GetUserContext(BlenderConnection blender, bool? clear = null, CancellationToken ct = default)
    {
        var res = await blender.CallAsync("get_user_context", new Args { ["clear"] = clear }, ct: ct) as JsonObject;
        if (res?["available"]?.GetValue<bool>() != true)
        {
            return [new TextContent(Results.Text(res))];
        }
        var image = res["image"];
        res.Remove("image");
        var list = new List<AIContent> { new TextContent(Results.Text(res)) };
        if (image is not null)
        {
            list.Add(Results.Image(image));
        }
        return list;
    }
}
