using System.ComponentModel;
using System.Text.Json;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

[McpServerToolType]
public static class ModifierTools
{
    [McpServerTool(Name = "add_modifier", Title = "Add modifier")]
    [Description("Add a modifier and set its properties in one call. Examples: BEVEL {width:0.02, segments:2, limit_method:'ANGLE', angle_limit:30}; ARRAY {count:5, relative_offset_displace:[1.1,0,0]}; MIRROR {use_axis:[true,false,false], mirror_object:'Empty'}; SOLIDIFY {thickness:0.05}; BOOLEAN {operation:'DIFFERENCE', object:'Cutter'}; WEIGHTED_NORMAL {keep_sharp:true}. Angles in degrees; object references by name.")]
    public static async Task<string> AddModifier(
        BlenderConnection blender,
        [Description("Target object.")] string @object,
        [Description("Modifier type, e.g. BEVEL, ARRAY, MIRROR, SOLIDIFY, SUBSURF, DECIMATE, BOOLEAN, WEIGHTED_NORMAL, TRIANGULATE, REMESH, WELD, DISPLACE, NODES.")] string type,
        [Description("Modifier name (defaults to the type).")] string? name = null,
        [Description("Modifier properties to set.")] Dictionary<string, JsonElement>? props = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("add_modifier", new Args
        {
            ["object"] = @object, ["type"] = type, ["name"] = name, ["props"] = props,
        }, ct: ct));

    [McpServerTool(Name = "set_modifier", Title = "Edit modifier")]
    [Description("Change properties of an existing modifier.")]
    public static async Task<string> SetModifier(
        BlenderConnection blender, string @object, [Description("Modifier name.")] string name,
        Dictionary<string, JsonElement> props, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_modifier", new Args
        {
            ["object"] = @object, ["name"] = name, ["props"] = props,
        }, ct: ct));

    [McpServerTool(Name = "remove_modifier", Title = "Remove modifier")]
    [Description("Remove a modifier without applying it.")]
    public static async Task<string> RemoveModifier(BlenderConnection blender, string @object, string name, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("remove_modifier", new Args { ["object"] = @object, ["name"] = name }, ct: ct));

    [McpServerTool(Name = "apply_modifiers", Title = "Apply modifiers")]
    [Description("Bake a single named modifier, or the whole stack when name is omitted, into the mesh. Returns triangle counts before/after.")]
    public static async Task<string> ApplyModifiers(BlenderConnection blender, string @object, string? name = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("apply_modifiers", new Args { ["object"] = @object, ["name"] = name }, ct: ct));
}

[McpServerToolType]
public static class MaterialTools
{
    [McpServerTool(Name = "create_material", Title = "Create PBR material")]
    [Description("Create (or update, if it exists) a Principled BSDF material and optionally assign it. Colours are linear [r,g,b] 0..1. alpha < 1 switches to blended transparency.")]
    public static async Task<string> CreateMaterial(
        BlenderConnection blender,
        string name,
        double[]? baseColor = null,
        double? metallic = null,
        double? roughness = null,
        double? alpha = null,
        double[]? emissionColor = null,
        double? emissionStrength = null,
        [Description("Any other Principled BSDF inputs by socket name, e.g. {\"Coat Weight\":0.5}.")] Dictionary<string, JsonElement>? inputs = null,
        [Description("Objects to assign it to (slot 0).")] string[]? assignTo = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_material", new Args
        {
            ["name"] = name, ["base_color"] = baseColor, ["metallic"] = metallic, ["roughness"] = roughness,
            ["alpha"] = alpha, ["emission_color"] = emissionColor, ["emission_strength"] = emissionStrength,
            ["inputs"] = inputs, ["assign_to"] = assignTo,
        }, ct: ct));

    [McpServerTool(Name = "set_material", Title = "Edit material")]
    [Description("Change Principled BSDF values on an existing material.")]
    public static async Task<string> SetMaterial(
        BlenderConnection blender,
        string name,
        double[]? baseColor = null,
        double? metallic = null,
        double? roughness = null,
        double? alpha = null,
        double[]? emissionColor = null,
        double? emissionStrength = null,
        Dictionary<string, JsonElement>? inputs = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_material", new Args
        {
            ["name"] = name, ["base_color"] = baseColor, ["metallic"] = metallic, ["roughness"] = roughness,
            ["alpha"] = alpha, ["emission_color"] = emissionColor, ["emission_strength"] = emissionStrength,
            ["inputs"] = inputs,
        }, ct: ct));

    [McpServerTool(Name = "assign_material", Title = "Assign material")]
    [Description("Assign a material to objects (slot 0 by default). With slot + faceIndices, assigns only those faces to that slot.")]
    public static async Task<string> AssignMaterial(
        BlenderConnection blender,
        string[] objects,
        string material,
        int? slot = null,
        int[]? faceIndices = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("assign_material", new Args
        {
            ["objects"] = objects, ["material"] = material, ["slot"] = slot, ["face_indices"] = faceIndices,
        }, ct: ct));

    [McpServerTool(Name = "add_image_texture", Title = "Add image texture")]
    [Description("Load an image file from disk and wire it into a material. Colour space and normal-map nodes are set up automatically.")]
    public static async Task<string> AddImageTexture(
        BlenderConnection blender,
        string material,
        [Description("Absolute path to the image on the Blender machine.")] string path,
        [Description("base_color | roughness | metallic | normal | emission | alpha")] string target = "base_color",
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("add_image_texture", new Args
        {
            ["material"] = material, ["path"] = path, ["target"] = target,
        }, ct: ct));
}

[McpServerToolType]
public static class MeshTools
{
    [McpServerTool(Name = "mesh_cleanup", Title = "Clean up mesh")]
    [Description("Merge by distance, dissolve degenerate geometry, delete loose verts/edges, recalculate normals, optionally triangulate. Works without entering edit mode.")]
    public static async Task<string> MeshCleanup(
        BlenderConnection blender,
        string @object,
        [Description("Merge distance in metres (default 0.0001, 0 disables).")] double? mergeDistance = null,
        bool? deleteLoose = null,
        bool? dissolveDegenerate = null,
        bool? recalcNormals = null,
        bool? triangulate = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("mesh_cleanup", new Args
        {
            ["object"] = @object, ["merge_distance"] = mergeDistance, ["delete_loose"] = deleteLoose,
            ["dissolve_degenerate"] = dissolveDegenerate, ["recalc_normals"] = recalcNormals, ["triangulate"] = triangulate,
        }, ct: ct));

    [McpServerTool(Name = "set_shading", Title = "Set shading")]
    [Description("flat | smooth | auto. 'auto' smooths everything and marks edges sharper than angleDeg as sharp (the modern replacement for auto-smooth; exports cleanly to game engines).")]
    public static async Task<string> SetShading(
        BlenderConnection blender, string[] objects, string mode = "auto", double? angleDeg = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_shading", new Args
        {
            ["objects"] = objects, ["mode"] = mode, ["angle_deg"] = angleDeg,
        }, ct: ct));

    [McpServerTool(Name = "apply_transform", Title = "Apply transform")]
    [Description("Bake rotation and/or scale (and optionally location) into mesh data, like Ctrl+A. Children keep their world position.")]
    public static async Task<string> ApplyTransform(
        BlenderConnection blender, string[] objects, bool? location = null, bool? rotation = null, bool? scale = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("apply_transform", new Args
        {
            ["objects"] = objects, ["location"] = location, ["rotation"] = rotation, ["scale"] = scale,
        }, ct: ct));

    [McpServerTool(Name = "set_origin", Title = "Set origin")]
    [Description("Move an object's origin without moving its geometry. 'bottom_center' is the usual choice for props placed on the floor in a game engine.")]
    public static async Task<string> SetOrigin(
        BlenderConnection blender,
        string @object,
        [Description("bounds_center | bottom_center | median | world_origin | cursor")] string? where = null,
        [Description("Explicit world-space [x,y,z] (overrides 'where').")] double[]? point = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("set_origin", new Args
        {
            ["object"] = @object, ["where"] = (object?)point ?? where,
        }, ct: ct));

    [McpServerTool(Name = "uv_unwrap", Title = "UV unwrap")]
    [Description("Unwrap UVs. smart = Smart UV Project for texturing; lightmap = non-overlapping lightmap pack (use uvLayer='Lightmap' to create UV channel 1 for Unreal lightmaps); cube = box projection.")]
    public static async Task<string> UvUnwrap(
        BlenderConnection blender,
        string @object,
        [Description("smart | lightmap | cube")] string method = "smart",
        [Description("UV layer to write (created if missing). Default: active layer.")] string? uvLayer = null,
        [Description("Island margin 0..1 (default 0.02).")] double? margin = null,
        [Description("Smart project angle limit in degrees (default 66).")] double? angleDeg = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("uv_unwrap", new Args
        {
            ["object"] = @object, ["method"] = method, ["uv_layer"] = uvLayer, ["margin"] = margin, ["angle_deg"] = angleDeg,
        }, ct: ct));
}
