using System.ComponentModel;
using System.Text.Json;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

[McpServerToolType]
public static class ObjectTools
{
    [McpServerTool(Name = "create_primitive", Title = "Create mesh primitive")]
    [Description("Create a mesh primitive with UVs, without touching the user's selection. Units are metres, rotations are degrees (XYZ Euler).")]
    public static async Task<string> CreatePrimitive(
        BlenderConnection blender,
        [Description("cube | uv_sphere | ico_sphere | cylinder | cone | plane | grid | circle | monkey | torus")] string kind,
        [Description("Object name (Blender appends .001 if taken).")] string? name = null,
        [Description("Overall size in metres (cube edge, plane width, sphere diameter). Default 2.")] double? size = null,
        [Description("Radius override for spheres/cylinders/cones/circles.")] double? radius = null,
        [Description("Height for cylinder/cone.")] double? depth = null,
        [Description("Radial segments (spheres, cylinders, cones, circle, torus major). Default 32.")] int? segments = null,
        [Description("Rings (uv_sphere) or minor segments (torus). Default 16.")] int? rings = null,
        [Description("Ico sphere subdivisions. Default 2.")] int? subdivisions = null,
        [Description("Grid X/Y segments.")] int? xSegments = null,
        [Description("Grid Y segments.")] int? ySegments = null,
        [Description("Torus major radius.")] double? majorRadius = null,
        [Description("Torus minor radius.")] double? minorRadius = null,
        [Description("Cone top radius (0 = pointed).")] double? radiusTop = null,
        [Description("[x, y, z] location.")] double[]? location = null,
        [Description("[x, y, z] rotation in degrees.")] double[]? rotation = null,
        [Description("[x, y, z] scale.")] double[]? scale = null,
        [Description("Collection to put it in (created if missing). Default: active collection.")] string? collection = null,
        [Description("Smooth shading.")] bool? smooth = null,
        [Description("Existing material name to assign.")] string? material = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_primitive", new Args
        {
            ["kind"] = kind, ["name"] = name, ["size"] = size, ["radius"] = radius, ["depth"] = depth,
            ["segments"] = segments, ["rings"] = rings, ["subdivisions"] = subdivisions,
            ["x_segments"] = xSegments, ["y_segments"] = ySegments, ["major_radius"] = majorRadius,
            ["minor_radius"] = minorRadius, ["radius_top"] = radiusTop, ["location"] = location,
            ["rotation"] = rotation, ["scale"] = scale, ["collection"] = collection, ["smooth"] = smooth,
            ["material"] = material,
        }, ct: ct));

    [McpServerTool(Name = "create_empty", Title = "Create empty")]
    [Description("Create an empty (useful as a pivot, parent, or socket).")]
    public static async Task<string> CreateEmpty(
        BlenderConnection blender,
        string? name = null,
        [Description("PLAIN_AXES | ARROWS | SINGLE_ARROW | CIRCLE | CUBE | SPHERE | CONE")] string? display = null,
        double? size = null,
        double[]? location = null,
        double[]? rotation = null,
        string? collection = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_empty", new Args
        {
            ["name"] = name, ["display"] = display, ["size"] = size, ["location"] = location,
            ["rotation"] = rotation, ["collection"] = collection,
        }, ct: ct));

    [McpServerTool(Name = "create_light", Title = "Create light")]
    [Description("Create a light. Energy is watts for point/spot/area, irradiance (W/m2) for sun.")]
    public static async Task<string> CreateLight(
        BlenderConnection blender,
        [Description("POINT | SUN | SPOT | AREA")] string type = "POINT",
        string? name = null,
        double? energy = null,
        [Description("[r, g, b] 0..1")] double[]? color = null,
        double[]? location = null,
        double[]? rotation = null,
        [Description("[x, y, z] point to aim at (overrides rotation).")] double[]? lookAt = null,
        string? collection = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_light", new Args
        {
            ["type"] = type, ["name"] = name, ["energy"] = energy, ["color"] = color, ["location"] = location,
            ["rotation"] = rotation, ["look_at"] = lookAt, ["collection"] = collection,
        }, ct: ct));

    [McpServerTool(Name = "create_camera", Title = "Create camera")]
    [Description("Create a camera, optionally aimed at a point and made the scene camera.")]
    public static async Task<string> CreateCamera(
        BlenderConnection blender,
        string? name = null,
        [Description("Focal length in mm (default 50).")] double? lens = null,
        double[]? location = null,
        double[]? rotation = null,
        [Description("[x, y, z] point to aim at.")] double[]? lookAt = null,
        [Description("Make it the active scene camera (default true).")] bool? setActive = null,
        string? collection = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_camera", new Args
        {
            ["name"] = name, ["lens"] = lens, ["location"] = location, ["rotation"] = rotation,
            ["look_at"] = lookAt, ["set_active"] = setActive, ["collection"] = collection,
        }, ct: ct));

    [McpServerTool(Name = "transform_object", Title = "Transform object")]
    [Description("Set or offset an object's location/rotation(degrees)/scale, set absolute dimensions, or aim it at a point.")]
    public static async Task<string> TransformObject(
        BlenderConnection blender,
        string name,
        double[]? location = null,
        [Description("Degrees.")] double[]? rotation = null,
        double[]? scale = null,
        [Description("[x, y, z] absolute size in metres (adjusts scale).")] double[]? dimensions = null,
        [Description("[x, y, z] point to face (-Z axis, like cameras/lights).")] double[]? lookAt = null,
        [Description("'set' (default) or 'delta' (add location/rotation, multiply scale).")] string? mode = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("transform_object", new Args
        {
            ["name"] = name, ["location"] = location, ["rotation"] = rotation, ["scale"] = scale,
            ["dimensions"] = dimensions, ["look_at"] = lookAt, ["mode"] = mode,
        }, ct: ct));

    [McpServerTool(Name = "set_object_properties", Title = "Set object properties")]
    [Description("Rename, re-parent (keeps world transform), move to a collection, hide/show, set display type, custom properties, or any other Object RNA property via 'props'.")]
    public static async Task<string> SetObjectProperties(
        BlenderConnection blender,
        string name,
        string? newName = null,
        [Description("Parent object name, or empty string to clear the parent.")] string? parent = null,
        string? collection = null,
        bool? hide = null,
        bool? hideRender = null,
        [Description("TEXTURED | SOLID | WIRE | BOUNDS")] string? displayType = null,
        [Description("Custom properties to set, e.g. {\"lod_group\":\"crate\"}.")] Dictionary<string, JsonElement>? customProps = null,
        [Description("Raw Object properties, e.g. {\"show_in_front\":true}.")] Dictionary<string, JsonElement>? props = null,
        CancellationToken ct = default)
    {
        var args = new Args
        {
            ["name"] = name, ["new_name"] = newName, ["collection"] = collection, ["hide"] = hide,
            ["hide_render"] = hideRender, ["display_type"] = displayType, ["custom_props"] = customProps,
            ["props"] = props,
        };
        if (parent is not null)
        {
            args.Add("parent", parent.Length == 0 ? null : parent);
        }
        return Results.Text(await blender.CallAsync("set_object_properties", args, ct: ct));
    }

    [McpServerTool(Name = "set_property", Title = "Set any property")]
    [Description("Set any RNA property on an object by data path, e.g. path='data.lens', 'modifiers[\"Bevel\"].width', 'data.materials[0].diffuse_color'. Angles in degrees; enums as strings; objects/materials by name.")]
    public static async Task<string> SetProperty(
        BlenderConnection blender,
        [Description("Object name.")] string @object,
        [Description("Data path relative to the object.")] string path,
        [Description("New value (number, string, bool, or array).")] JsonElement value,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_property", new Args
        {
            ["object"] = @object, ["path"] = path, ["value"] = value,
        }, ct: ct));

    [McpServerTool(Name = "delete_objects", Destructive = true, Title = "Delete objects")]
    [Description("Delete objects (and their now-unused mesh/light/camera data). Undoable with the undo tool.")]
    public static async Task<string> DeleteObjects(BlenderConnection blender, string[] names, CancellationToken ct)
        => Results.Text(await blender.CallAsync("delete_objects", new Args { ["names"] = names }, ct: ct));

    [McpServerTool(Name = "duplicate_object", Title = "Duplicate object")]
    [Description("Duplicate an object one or more times, each copy offset by 'offset' from the previous. linked=true shares mesh data (instances: cheaper memory, edits affect all).")]
    public static async Task<string> DuplicateObject(
        BlenderConnection blender,
        string name,
        int? count = null,
        [Description("[x, y, z] offset per copy.")] double[]? offset = null,
        bool? linked = null,
        [Description("Name for the copy (only when count = 1).")] string? newName = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("duplicate_object", new Args
        {
            ["name"] = name, ["count"] = count, ["offset"] = offset, ["linked"] = linked, ["new_name"] = newName,
        }, ct: ct));

    [McpServerTool(Name = "create_collection", Title = "Create collection")]
    [Description("Create a collection (idempotent), optionally nested under a parent collection.")]
    public static async Task<string> CreateCollection(BlenderConnection blender, string name, string? parent = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_collection", new Args { ["name"] = name, ["parent"] = parent }, ct: ct));
}
