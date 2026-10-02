using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using Microsoft.Extensions.AI;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Scene layout: instancing/scatter, align, distribute, drop to floor, scene diff.</summary>
[McpServerToolType]
public static class LayoutTools
{
    [McpServerTool(Name = "scatter_instances", Title = "Scatter instances")]
    [Description("Place many copies of an object. linked=true (default) makes instances that share one mesh: cheap in memory and editing the source changes them all (exported as one mesh in engines). mode=grid (grid [nx,ny], spacing, origin, jitter), surface (random points on 'target' mesh, up_only, minDistance, alignToNormal) or box (count random points between min and max). Random yaw / scale per copy, repeatable with seed. Max 5000 per call.")]
    public static async Task<string> ScatterInstances(
        BlenderConnection blender,
        [Description("Object to copy.")] string source,
        [Description("grid | surface | box")] string mode = "grid",
        [Description("Grid size [nx, ny] (grid mode).")] int[]? grid = null,
        [Description("Grid spacing in metres: one number or [x, y].")] double[]? spacing = null,
        [Description("Grid start position [x,y,z] (default: source location).")] double[]? origin = null,
        [Description("Random XY offset per grid cell, metres.")] double? jitter = null,
        [Description("Mesh to scatter on (surface mode).")] string? target = null,
        [Description("Number of copies (surface/box).")] int? count = null,
        [Description("Only faces facing up (surface mode, default true).")] bool? upOnly = null,
        [Description("Minimum distance between copies (surface mode).")] double? minDistance = null,
        [Description("Tilt copies to the surface normal (surface mode).")] bool? alignToNormal = null,
        [Description("Box corner [x,y,z] (box mode).")] double[]? min = null,
        [Description("Box corner [x,y,z] (box mode).")] double[]? max = null,
        [Description("Random rotation around Z, +/- degrees.")] double? randomYawDeg = null,
        [Description("Random uniform scale [min, max].")] double[]? scaleRange = null,
        int? seed = null,
        [Description("Share mesh data (default true).")] bool? linked = null,
        [Description("Collection for the copies (default <source>_Instances).")] string? collection = null,
        CancellationToken ct = default)
    {
        object? sp = spacing is { Length: 1 } ? spacing[0] : spacing;
        return Results.Text(await blender.CallAsync("scatter_instances", new Args
        {
            ["source"] = source, ["mode"] = mode, ["grid"] = grid, ["spacing"] = sp, ["origin"] = origin,
            ["jitter"] = jitter, ["target"] = target, ["count"] = count, ["up_only"] = upOnly,
            ["min_distance"] = minDistance, ["align_to_normal"] = alignToNormal, ["min"] = min, ["max"] = max,
            ["random_yaw_deg"] = randomYawDeg, ["scale_range"] = scaleRange, ["seed"] = seed, ["linked"] = linked,
            ["collection"] = collection,
        }, TimeSpan.FromMinutes(2), ct));
    }

    [McpServerTool(Name = "align_objects", Title = "Align objects")]
    [Description("Line up objects' bounding boxes on one axis: to=min|center|max, relative to the first object (default), the group's combined bounds ('group'), or an absolute world coordinate (pass a number as value).")]
    public static async Task<string> AlignObjects(
        BlenderConnection blender,
        string[] objects,
        [Description("x | y | z")] string axis = "x",
        [Description("min | center | max")] string to = "min",
        [Description("first | group")] string? relativeTo = null,
        [Description("Absolute world coordinate to align to (overrides relativeTo).")] double? value = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("align_objects", new Args
        {
            ["objects"] = objects, ["axis"] = axis, ["to"] = to, ["relative_to"] = (object?)value ?? relativeTo,
        }, ct: ct));

    [McpServerTool(Name = "distribute_objects", Title = "Distribute objects")]
    [Description("Spread objects along an axis (sorted by position by default): with 'gap' they are packed with that many metres between bounding boxes starting at the first; without it they are spaced evenly between the first and last.")]
    public static async Task<string> DistributeObjects(
        BlenderConnection blender, string[] objects, string axis = "x", double? gap = null, bool? sort = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("distribute_objects", new Args
        {
            ["objects"] = objects, ["axis"] = axis, ["gap"] = gap, ["sort"] = sort,
        }, ct: ct));

    [McpServerTool(Name = "drop_to_floor", Title = "Drop to floor")]
    [Description("Move objects straight down so their bottom rests at floorZ (default 0), or with ontoObjects=true on whatever mesh is underneath (tables, terrain, other props).")]
    public static async Task<string> DropToFloor(
        BlenderConnection blender, string[] objects, double? floorZ = null, bool? ontoObjects = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("drop_to_floor", new Args
        {
            ["objects"] = objects, ["floor_z"] = floorZ, ["onto_objects"] = ontoObjects,
        }, ct: ct));

    [McpServerTool(Name = "scene_changes", ReadOnly = true, Title = "Scene diff")]
    [Description("Cheap change tracking. Call without 'since' to get a token; later call with since=<token> to learn which objects were added, removed or changed (transform, mesh, modifiers, materials, visibility) - e.g. to see what the user did by hand. Each call returns a fresh token.")]
    public static async Task<string> SceneChanges(BlenderConnection blender, string? since = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("scene_changes", new Args { ["since"] = since }, ct: ct));
}

/// <summary>Asset review: turntables and texel density.</summary>
[McpServerToolType]
public static class ReviewTools
{
    [McpServerTool(Name = "turntable", ReadOnly = true, Title = "Turntable contact sheet")]
    [Description("Render the object(s) from N angles all the way around (fixed elevation) and return ONE contact-sheet image - a quick all-sides review of an asset. wireframe=true overlays the topology.")]
    public static async Task<IEnumerable<AIContent>> Turntable(
        BlenderConnection blender,
        string[] objects,
        [Description("Number of angles, 2-24 (default 8).")] int? frames = null,
        [Description("Camera elevation in degrees (default 20).")] double? elevationDeg = null,
        [Description("Size of each tile in pixels (default 256).")] int? size = null,
        [Description("Tiles per row (default 4).")] int? columns = null,
        [Description("SOLID | MATERIAL | RENDERED | WIREFRAME")] string? shading = null,
        bool? wireframe = null,
        CancellationToken ct = default)
    {
        var img = await blender.CallAsync("turntable", new Args
        {
            ["objects"] = objects, ["frames"] = frames, ["elevation_deg"] = elevationDeg, ["size"] = size,
            ["columns"] = columns, ["shading"] = shading, ["wireframe"] = wireframe,
        }, TimeSpan.FromMinutes(2), ct);
        return Results.ImageWithCaption(img, $"Turntable, {img?["frames"]} angles, {img?["tris"]} tris");
    }

    [McpServerTool(Name = "texel_density", ReadOnly = true, Title = "Texel density / UV check")]
    [Description("For each mesh: texel density (pixels per metre) at a texture size, how evenly it is spread across faces, and how much of the UV square is used. Warns about stretched islands, wasted UV space, overlaps and assets off a target density - keeps a prop set visually consistent.")]
    public static async Task<string> TexelDensity(
        BlenderConnection blender,
        string[] objects,
        [Description("Texture resolution the asset will use (default 1024).")] int? textureSize = null,
        [Description("Desired px/m to compare against (e.g. 512).")] double? targetPxPerM = null,
        [Description("UV layer (default: active).")] string? uvLayer = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("texel_density", new Args
        {
            ["objects"] = objects, ["texture_size"] = textureSize, ["target_px_per_m"] = targetPxPerM, ["uv_layer"] = uvLayer,
        }, ct: ct));
}
