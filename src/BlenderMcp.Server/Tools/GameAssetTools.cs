using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Game-engine asset pipeline: budgets, validation, LODs, collision, export.</summary>
[McpServerToolType]
public static class GameAssetTools
{
    private static readonly TimeSpan LongOp = TimeSpan.FromMinutes(5);

    [McpServerTool(Name = "mesh_stats", ReadOnly = true, Title = "Triangle budget report")]
    [Description("Vertex/triangle counts per mesh, before and after modifiers, sorted heaviest first, plus scene totals. Use it to find what blows the poly budget.")]
    public static async Task<string> MeshStats(
        BlenderConnection blender, [Description("Objects to report (default: all meshes in the scene).")] string[]? objects = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("mesh_stats", new Args { ["objects"] = objects }, ct: ct));

    [McpServerTool(Name = "validate_asset", ReadOnly = true, Title = "Validate game asset")]
    [Description("Game-readiness check: unapplied/negative scale, missing UVs or lightmap UVs, missing materials, too many material slots (draw calls), n-gons, non-manifold/loose/duplicate geometry, zero-area faces, triangle budget, missing UCX_ collision. Returns issues with severity and a code you can act on.")]
    public static async Task<string> ValidateAsset(
        BlenderConnection blender,
        string @object,
        [Description("Maximum triangles (after modifiers).")] int? triBudget = null,
        [Description("Max material slots before warning (default 4).")] int? maxMaterials = null,
        [Description("Warn when there is no second UV channel.")] bool? requireLightmapUv = null,
        [Description("Warn when there is no UCX_/UBX_/USP_ collision mesh.")] bool? requireCollision = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("validate_asset", new Args
        {
            ["object"] = @object, ["tri_budget"] = triBudget, ["max_materials"] = maxMaterials,
            ["require_lightmap_uv"] = requireLightmapUv, ["require_collision"] = requireCollision,
        }, ct: ct));

    [McpServerTool(Name = "decimate", Title = "Decimate to budget")]
    [Description("Reduce a mesh to a target triangle count (or ratio) using collapse decimation. apply=false leaves a live Decimate modifier instead.")]
    public static async Task<string> Decimate(
        BlenderConnection blender,
        string @object,
        [Description("Desired triangle count after decimation.")] int? targetTris = null,
        [Description("Ratio 0..1 (used if targetTris is not given).")] double? ratio = null,
        [Description("Keep symmetry on axis X, Y or Z.")] string? symmetry = null,
        bool? apply = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("decimate", new Args
        {
            ["object"] = @object, ["target_tris"] = targetTris, ["ratio"] = ratio, ["symmetry"] = symmetry, ["apply"] = apply,
        }, LongOp, ct));

    [McpServerTool(Name = "generate_lods", Title = "Generate LODs")]
    [Description("Create <name>_LOD0..N copies at the given decimation ratios (modifiers baked) in a '<name>_LODs' collection. Export each LOD with export_asset, then import them as LODs in the engine (e.g. Unreal's Static Mesh Editor > LOD Import).")]
    public static async Task<string> GenerateLods(
        BlenderConnection blender,
        string @object,
        [Description("Ratio per LOD, e.g. [1, 0.5, 0.25, 0.1]. LOD0 = 1.")] double[]? ratios = null,
        [Description("Target collection name.")] string? collection = null,
        [Description("Offset each LOD along X by this many metres so they can be compared side by side.")] double? spacing = null,
        bool? hideSource = null,
        [Description("Re-mark edges sharper than this angle on each decimated LOD so shading matches LOD0 (default 35, 0 = leave as is).")] double? sharpAngleDeg = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("generate_lods", new Args
        {
            ["object"] = @object, ["ratios"] = ratios, ["collection"] = collection, ["spacing"] = spacing, ["hide_source"] = hideSource,
            ["sharp_angle_deg"] = sharpAngleDeg,
        }, LongOp, ct));

    [McpServerTool(Name = "generate_collision", Title = "Generate collision")]
    [Description("Create an Unreal-convention collision mesh next to the object: box -> UBX_<name>_NN, sphere -> USP_<name>_NN, convex -> UCX_<name>_NN (convex hull, simplified to maxVerts). Exported automatically with the object.")]
    public static async Task<string> GenerateCollision(
        BlenderConnection blender,
        string @object,
        [Description("box | sphere | convex")] string kind = "convex",
        [Description("Suffix index for multiple hulls (default 0).")] int? index = null,
        [Description("Max hull vertices for convex (default 64).")] int? maxVerts = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("generate_collision", new Args
        {
            ["object"] = @object, ["kind"] = kind, ["index"] = index, ["max_verts"] = maxVerts,
        }, ct: ct));

    [McpServerTool(Name = "export_asset", Title = "Export asset")]
    [Description("Export objects (plus their children and UCX_/UBX_/USP_ collision) to one file. The 'unreal' FBX preset uses FBX unit scaling (1 m = 100 UE units), face smoothing, tangent space, no leaf bones. Leaves the user's selection untouched.")]
    public static async Task<string> ExportAsset(
        BlenderConnection blender,
        [Description("Output file path on the Blender machine. Extension is added if missing.")] string path,
        [Description("Objects to export. If omitted uses 'collection', then the current selection.")] string[]? objects = null,
        string? collection = null,
        [Description("fbx | glb | gltf | obj | usd")] string format = "fbx",
        [Description("unreal | unity | none (FBX only).")] string preset = "unreal",
        bool? applyModifiers = null,
        bool? includeCollision = null,
        bool? triangulate = null,
        bool? animation = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("export_asset", new Args
        {
            ["path"] = path, ["objects"] = objects, ["collection"] = collection, ["format"] = format, ["preset"] = preset,
            ["apply_modifiers"] = applyModifiers, ["include_collision"] = includeCollision,
            ["triangulate"] = triangulate, ["animation"] = animation,
        }, LongOp, ct));

    [McpServerTool(Name = "batch_export", Title = "Batch export")]
    [Description("Export each object to its own file '<prefix><name>.<ext>' in a folder, temporarily moved to the world origin so pivots land correctly in the engine. Collision meshes ride along.")]
    public static async Task<string> BatchExport(
        BlenderConnection blender,
        [Description("Output folder on the Blender machine.")] string folder,
        [Description("Objects to export. If omitted: root meshes in 'collection', else the selection.")] string[]? objects = null,
        string? collection = null,
        string format = "fbx",
        [Description("File name prefix (default 'SM_').")] string? prefix = null,
        [Description("Move each object to the origin while exporting (default true).")] bool? center = null,
        string preset = "unreal",
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("batch_export", new Args
        {
            ["folder"] = folder, ["objects"] = objects, ["collection"] = collection, ["format"] = format,
            ["prefix"] = prefix, ["center"] = center, ["preset"] = preset,
        }, LongOp, ct));

    [McpServerTool(Name = "import_file", Title = "Import model")]
    [Description("Import FBX, glTF/GLB, OBJ, USD, STL or PLY. Returns the names of the new objects.")]
    public static async Task<string> ImportFile(BlenderConnection blender, string path, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("import_file", new Args { ["path"] = path }, LongOp, ct));
}
