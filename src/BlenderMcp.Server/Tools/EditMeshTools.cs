using System.ComponentModel;
using System.Text.Json;
using BlenderMcp.Server.Bridge;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Component-level modelling with face selectors (no edit-mode state needed).</summary>
[McpServerToolType]
public static class EditMeshTools
{
    internal const string SelectorHelp =
        "Face selector object (keys combine with AND): {\"all\":true} | {\"side\":\"top|bottom|front|back|left|right\"} | " +
        "{\"normal\":[x,y,z],\"angle_deg\":15} | {\"material\":\"M_Metal\"} | {\"indices\":[0,4]} | " +
        "{\"region\":{\"min\":[x,y,z],\"max\":[x,y,z]}} (world space) | add {\"largest\":N} to keep the N biggest. Default: all faces.";

    [McpServerTool(Name = "face_info", ReadOnly = true, Title = "Preview face selector")]
    [Description("Show which faces a selector matches (count, area, centre, indices) without changing anything. Use before extrude/inset/delete to check the selection.")]
    public static async Task<string> FaceInfo(
        BlenderConnection blender, string @object, [Description(SelectorHelp)] JsonElement? selector = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("face_info", new Args { ["object"] = @object, ["selector"] = selector }, ct: ct));

    [McpServerTool(Name = "extrude_faces", Title = "Extrude faces")]
    [Description("Extrude the selected faces along their normal by 'distance' metres (negative = push in). individual=true extrudes each face separately (studs, greebles).")]
    public static async Task<string> ExtrudeFaces(
        BlenderConnection blender,
        string @object,
        [Description(SelectorHelp)] JsonElement? selector = null,
        [Description("Metres along the normal (default 0.2).")] double? distance = null,
        bool? individual = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("extrude_faces", new Args
        {
            ["object"] = @object, ["selector"] = selector, ["distance"] = distance, ["individual"] = individual,
        }, ct: ct));

    [McpServerTool(Name = "inset_faces", Title = "Inset faces")]
    [Description("Inset the selected faces by 'thickness' metres and optionally push the new inner face by 'depth' (panels, windows, vents, recesses). individual=true insets each face on its own.")]
    public static async Task<string> InsetFaces(
        BlenderConnection blender,
        string @object,
        [Description(SelectorHelp)] JsonElement? selector = null,
        [Description("Border width in metres (default 0.05).")] double? thickness = null,
        [Description("Push in (-) or out (+) in metres (default 0).")] double? depth = null,
        bool? individual = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("inset_faces", new Args
        {
            ["object"] = @object, ["selector"] = selector, ["thickness"] = thickness, ["depth"] = depth, ["individual"] = individual,
        }, ct: ct));

    [McpServerTool(Name = "bevel_edges", Title = "Bevel edges")]
    [Description("Bevel edges into the mesh (real geometry, unlike the modifier): every edge sharper than angleDeg (default 30), or only the border edges of a face selector.")]
    public static async Task<string> BevelEdges(
        BlenderConnection blender,
        string @object,
        [Description("Bevel width in metres (default 0.02).")] double? width = null,
        [Description("Segments (default 1 = chamfer; 2-3 = rounded).")] int? segments = null,
        double? angleDeg = null,
        [Description(SelectorHelp + " If given, bevels the outline of those faces instead of sharp edges.")] JsonElement? selector = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("bevel_edges", new Args
        {
            ["object"] = @object, ["width"] = width, ["segments"] = segments, ["angle_deg"] = angleDeg, ["selector"] = selector,
        }, TimeSpan.FromMinutes(5), ct));

    [McpServerTool(Name = "loop_cut", Title = "Loop cut")]
    [Description("Add edge loops by slicing the mesh with planes across a local axis: 'cuts' evenly spaced, or explicit 'positions' as 0..1 fractions of the object's extent.")]
    public static async Task<string> LoopCut(
        BlenderConnection blender,
        string @object,
        [Description("x | y | z (local).")] string axis = "z",
        int? cuts = null,
        double[]? positions = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("loop_cut", new Args
        {
            ["object"] = @object, ["axis"] = axis, ["cuts"] = cuts, ["positions"] = positions,
        }, ct: ct));

    [McpServerTool(Name = "delete_faces", Destructive = true, Title = "Delete faces")]
    [Description("Delete the faces a selector matches - e.g. the never-seen bottom of a prop to save triangles.")]
    public static async Task<string> DeleteFaces(
        BlenderConnection blender, string @object, [Description(SelectorHelp)] JsonElement selector, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("delete_faces", new Args { ["object"] = @object, ["selector"] = selector }, ct: ct));
}

/// <summary>Flat-colour texturing for low-poly assets.</summary>
[McpServerToolType]
public static class PaletteTools
{
    [McpServerTool(Name = "create_palette", Title = "Create colour palette")]
    [Description("Create (or update) a shared palette texture T_<name> (8 swatches per row, nearest filtering, saved as PNG) and material M_<name>. Then paint_faces assigns faces to swatches: a whole prop set can share one material, one tiny texture and one draw call, with no UV unwrapping.")]
    public static async Task<string> CreatePalette(
        BlenderConnection blender,
        [Description("Colours as '#RRGGBB' (sRGB) or [r,g,b] 0..1.")] JsonElement colors,
        string name = "Palette",
        [Description("Pixels per swatch (default 8).")] int? swatch = null,
        [Description("Folder for the PNG (default: 'textures' next to the .blend).")] string? folder = null,
        double? roughness = null,
        double? metallic = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("create_palette", new Args
        {
            ["name"] = name, ["colors"] = colors, ["swatch"] = swatch, ["folder"] = folder,
            ["roughness"] = roughness, ["metallic"] = metallic,
        }, ct: ct));

    [McpServerTool(Name = "paint_faces", Title = "Paint faces")]
    [Description("Give selected faces a flat colour. mode=palette (default): assigns the palette material and collapses the faces' UVs onto that colour's swatch (colours not yet in the palette are added). mode=vertex: writes a 'Col' vertex-colour attribute and uses M_VertexColor.")]
    public static async Task<string> PaintFaces(
        BlenderConnection blender,
        string @object,
        [Description("'#RRGGBB' or [r,g,b] 0..1 (or use index).")] JsonElement? color = null,
        [Description("Palette swatch index instead of a colour.")] int? index = null,
        [Description(EditMeshTools.SelectorHelp)] JsonElement? selector = null,
        [Description("palette | vertex")] string mode = "palette",
        [Description("Palette name (default 'Palette').")] string? palette = null,
        CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("paint_faces", new Args
        {
            ["object"] = @object, ["color"] = color, ["index"] = index, ["selector"] = selector, ["mode"] = mode,
            ["palette"] = palette,
        }, ct: ct));

    [McpServerTool(Name = "get_palette", ReadOnly = true, Title = "Get palette")]
    [Description("List a palette's colours by swatch index.")]
    public static async Task<string> GetPalette(BlenderConnection blender, string name = "Palette", CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("get_palette", new Args { ["name"] = name }, ct: ct));
}
