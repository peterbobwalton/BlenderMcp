using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Object-level modelling: join, separate, booleans.</summary>
[McpServerToolType]
public static class CombineTools
{
    [McpServerTool(Name = "join_objects", Title = "Join meshes")]
    [Description("Merge several meshes into one object (like Ctrl+J). The result keeps the target's name, origin and transform; materials and UVs are preserved.")]
    public static async Task<string> JoinObjects(
        BlenderConnection blender,
        [Description("Meshes to merge (at least two).")] string[] objects,
        [Description("Object that receives the others (default: the first).")] string? target = null,
        [Description("Rename the result.")] string? newName = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("join_objects", new Args
        {
            ["objects"] = objects, ["target"] = target, ["new_name"] = newName,
        }, ct: ct));

    [McpServerTool(Name = "separate", Title = "Separate mesh")]
    [Description("Split a mesh into separate objects by loose parts or by material.")]
    public static async Task<string> Separate(
        BlenderConnection blender, string @object, [Description("loose | material")] string by = "loose", CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("separate", new Args { ["object"] = @object, ["by"] = by }, ct: ct));

    [McpServerTool(Name = "boolean", Title = "Boolean")]
    [Description("Cut, merge or intersect a mesh with one or more cutter meshes. Applied by default (apply=false leaves live modifiers). Cutters are hidden afterwards by default; cutterAction=delete removes them.")]
    public static async Task<string> Boolean(
        BlenderConnection blender,
        [Description("Mesh to modify.")] string @object,
        [Description("Cutter meshes.")] string[] cutters,
        [Description("difference | union | intersect")] string operation = "difference",
        [Description("exact (robust, default) | float (fast) | manifold (fast, needs closed meshes)")] string? solver = null,
        bool? apply = null,
        [Description("hide | delete | keep")] string? cutterAction = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("boolean", new Args
        {
            ["object"] = @object, ["cutters"] = cutters, ["operation"] = operation, ["solver"] = solver,
            ["apply"] = apply, ["cutter_action"] = cutterAction,
        }, ct: ct));
}

/// <summary>Texture baking.</summary>
[McpServerToolType]
public static class BakeTools
{
    [McpServerTool(Name = "bake_maps", Title = "Bake texture maps")]
    [Description("Bake maps with Cycles onto a low-poly mesh's UVs and save them as PNGs named T_<asset>_<N|AO|BC|R|E>. With 'high', details are projected from the high-poly mesh(es) (selected-to-active). Normal/colour/roughness/emission maps are wired into the low-poly material automatically; AO is saved for use in the engine. The low mesh needs UVs (uv_unwrap). Other scene objects are hidden during the bake so they don't shadow it.")]
    public static async Task<string> BakeMaps(
        BlenderConnection blender,
        [Description("Low-poly target mesh (with UVs).")] string low,
        [Description("High-poly source mesh(es); required for normal maps.")] string[]? high = null,
        [Description("Maps to bake: normal, ao, color, roughness, emission (default normal + ao).")] string[]? maps = null,
        [Description("Texture size in pixels (default 1024).")] int? size = null,
        [Description("Output folder (default: 'textures' next to the .blend).")] string? folder = null,
        [Description("How far (m) rays start outside the low mesh to find the high mesh (default 0.05). Increase if the bake has holes.")] double? cageExtrusion = null,
        [Description("Max ray distance in metres (0 = unlimited).")] double? maxRayDistance = null,
        [Description("Edge padding in pixels (default 8).")] int? margin = null,
        [Description("Cycles samples (default 64 for AO, 1 otherwise).")] int? samples = null,
        [Description("Wire the baked images into the material (default true).")] bool? connect = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("bake_maps", new Args
        {
            ["low"] = low, ["high"] = high, ["maps"] = maps, ["size"] = size, ["folder"] = folder,
            ["cage_extrusion"] = cageExtrusion, ["max_ray_distance"] = maxRayDistance, ["margin"] = margin,
            ["samples"] = samples, ["connect"] = connect,
        }, TimeSpan.FromMinutes(20), ct));
}

/// <summary>Unreal Engine hand-off.</summary>
[McpServerToolType]
public static class UnrealTools
{
    [McpServerTool(Name = "add_socket", Title = "Add Unreal socket")]
    [Description("Add an attach point: an empty named SOCKET_<name> parented to the mesh. Unreal imports it as a static-mesh socket (for weapons, effects, pickups...). Location/rotation are relative to the object unless space='world'.")]
    public static async Task<string> AddSocket(
        BlenderConnection blender,
        string @object,
        [Description("Socket name (SOCKET_ prefix added automatically).")] string name,
        [Description("[x, y, z] in metres.")] double[]? location = null,
        [Description("[x, y, z] degrees.")] double[]? rotation = null,
        [Description("local (default) | world")] string? space = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("add_socket", new Args
        {
            ["object"] = @object, ["name"] = name, ["location"] = location, ["rotation"] = rotation, ["space"] = space,
        }, ct: ct));

    [McpServerTool(Name = "check_naming", Title = "Check Unreal naming")]
    [Description("Check Unreal naming conventions: SM_ static meshes, M_ materials, T_ textures. fix=true renames them and keeps LOD (_LODn) and collision (UCX_/UBX_/USP_) companions matched.")]
    public static async Task<string> CheckNaming(
        BlenderConnection blender,
        [Description("Meshes to check (default: all meshes in the scene).")] string[]? objects = null,
        bool? fix = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("check_naming", new Args { ["objects"] = objects, ["fix"] = fix }, ct: ct));

    [McpServerTool(Name = "export_for_unreal", Title = "Export for Unreal")]
    [Description("Export one game asset to a single FBX ready for Unreal: the mesh plus its <name>_LOD1.._LODn meshes as an FBX LOD group (one asset with LODs), UCX_/UBX_/USP_ collision and SOCKET_ empties, centred at the origin, triangulated, 1 m = 100 UE units. Returns the file path and ready-made arguments for the Unreal MCP's StaticMeshTools.import_file (combine_meshes must stay false) - call that next to bring it into the project. Note: that import keeps LOD0 only; the result explains how to get the LODs.")]
    public static async Task<string> ExportForUnreal(
        BlenderConnection blender,
        [Description("The asset's main (LOD0) mesh.")] string @object,
        [Description("Output folder (default: a temp folder).")] string? folder = null,
        [Description("Asset name (default SM_<object>).")] string? assetName = null,
        [Description("Unreal content folder for the import hint (default /Game/Meshes).")] string? unrealFolder = null,
        [Description("Include <name>_LODn meshes as a LOD group (default true).")] bool? lods = null,
        [Description("Move to the world origin for export (default true).")] bool? center = null,
        [Description("Triangulate on export (default true; required for correct tangents on n-gons).")] bool? triangulate = null,
        [Description("Also import materials/textures in Unreal (import hint, default true).")] bool? importMaterials = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("export_for_unreal", new Args
        {
            ["object"] = @object, ["folder"] = folder, ["asset_name"] = assetName, ["unreal_folder"] = unrealFolder,
            ["lods"] = lods, ["center"] = center, ["triangulate"] = triangulate, ["import_materials"] = importMaterials,
        }, TimeSpan.FromMinutes(5), ct));
}
