using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

[McpServerToolType]
public static class SceneTools
{
    [McpServerTool(Name = "get_scene_info", ReadOnly = true, Title = "Scene overview")]
    [Description("Overview of the open Blender file: version, units, render engine, object counts by type, collection tree, selection, active camera, total triangles. Call this first to orient yourself.")]
    public static async Task<string> GetSceneInfo(BlenderConnection blender, CancellationToken ct)
        => Results.Text(await blender.CallAsync("get_scene_info", ct: ct));

    [McpServerTool(Name = "list_objects", ReadOnly = true, Title = "List objects")]
    [Description("List objects with location, dimensions (metres) and triangle count. Filter by type, collection or name.")]
    public static async Task<string> ListObjects(
        BlenderConnection blender,
        [Description("Object type filter: MESH, LIGHT, CAMERA, EMPTY, CURVE, ARMATURE...")] string? type = null,
        [Description("Only objects in this collection (recursive).")] string? collection = null,
        [Description("Case-insensitive substring match on the name.")] string? nameContains = null,
        [Description("Maximum rows to return (default 200).")] int? limit = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("list_objects", new Args
        {
            ["type"] = type, ["collection"] = collection, ["name_contains"] = nameContains, ["limit"] = limit,
        }, ct: ct));

    [McpServerTool(Name = "get_object", ReadOnly = true, Title = "Object details")]
    [Description("Full details for one object: transform, world bounds, mesh stats (base and with modifiers), UV layers, materials, modifiers with all settings, vertex groups, shape keys, custom properties.")]
    public static async Task<string> GetObject(BlenderConnection blender, [Description("Object name.")] string name, CancellationToken ct)
        => Results.Text(await blender.CallAsync("get_object", new Args { ["name"] = name }, ct: ct));

    [McpServerTool(Name = "select_objects", Title = "Select objects")]
    [Description("Select objects in Blender (replaces the selection unless extend=true) and set the active object.")]
    public static async Task<string> SelectObjects(
        BlenderConnection blender,
        [Description("Object names to select. Empty list clears the selection.")] string[] names,
        [Description("Add to the current selection instead of replacing it.")] bool? extend = null,
        [Description("Object to make active (defaults to the first name).")] string? active = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("select_objects", new Args
        {
            ["names"] = names, ["extend"] = extend, ["active"] = active,
        }, ct: ct));

    [McpServerTool(Name = "list_materials", ReadOnly = true, Title = "List materials")]
    [Description("All materials with base colour, metallic, roughness, texture images and user count.")]
    public static async Task<string> ListMaterials(BlenderConnection blender, CancellationToken ct)
        => Results.Text(await blender.CallAsync("list_materials", ct: ct));
}
