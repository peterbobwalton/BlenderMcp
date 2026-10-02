using System.ComponentModel;
using BlenderMcp.Server.Bridge;
using Microsoft.Extensions.AI;
using ModelContextProtocol.Server;

namespace BlenderMcp.Server.Tools;

/// <summary>Visual feedback: lets the model actually look at what it built.</summary>
[McpServerToolType]
public static class ViewTools
{
    [McpServerTool(Name = "viewport_screenshot", ReadOnly = true, Title = "Viewport screenshot")]
    [Description("Image of the user's 3D viewport exactly as framed and shaded right now (overlays included). Use to see what the user sees.")]
    public static async Task<IEnumerable<AIContent>> ViewportScreenshot(
        BlenderConnection blender, [Description("Longest side in pixels (default 1280).")] int? maxSize = null, CancellationToken ct = default)
    {
        var img = await blender.CallAsync("viewport_screenshot", new Args { ["max_size"] = maxSize }, ct: ct);
        return Results.ImageWithCaption(img, $"Viewport ({img?["shading"]}, {img?["view_perspective"]})");
    }

    [McpServerTool(Name = "render_views", ReadOnly = true, Title = "Look at objects")]
    [Description("Fast offscreen OpenGL renders of specific objects (isolated) or the whole scene from preset angles, auto-framed. No camera needed, doesn't move the user's view. Ideal for checking your own modelling work. Views: front, back, left, right, top, bottom, iso, iso_back, iso_left, iso_right_back. Reports triangle counts; wireframe=true overlays topology.")]
    public static async Task<IEnumerable<AIContent>> RenderViews(
        BlenderConnection blender,
        [Description("Objects to frame (default: all visible geometry).")] string[]? objects = null,
        [Description("Angles to render (default ['iso']).")] string[]? views = null,
        [Description("Square image size in pixels (default 512).")] int? size = null,
        [Description("Hide everything else (default true when objects are given).")] bool? isolate = null,
        [Description("SOLID | MATERIAL | RENDERED | WIREFRAME (default: viewport's current mode).")] string? shading = null,
        [Description("Overlay the mesh wireframe on the shaded model to judge topology / poly budget.")] bool? wireframe = null,
        CancellationToken ct = default)
    {
        var res = await blender.CallAsync("render_views", new Args
        {
            ["objects"] = objects, ["views"] = views, ["size"] = size, ["isolate"] = isolate, ["shading"] = shading,
            ["wireframe"] = wireframe,
        }, ct: ct);
        var list = new List<AIContent> { new TextContent($"Triangles: {res?["total_tris"]} {Results.Text(res?["tris"])}") };
        foreach (var v in res?["views"]?.AsArray() ?? new System.Text.Json.Nodes.JsonArray())
        {
            list.Add(new TextContent($"View: {v?["view"]}"));
            list.Add(Results.Image(v));
        }
        return list;
    }

    [McpServerTool(Name = "render_image", ReadOnly = true, Title = "Render image")]
    [Description("Full engine render (BLENDER_EEVEE, CYCLES, BLENDER_WORKBENCH) through the scene camera, or an automatic framing camera when there is none or objects are given. Render settings are restored afterwards.")]
    public static async Task<IEnumerable<AIContent>> RenderImage(
        BlenderConnection blender,
        string? engine = null,
        int? width = null,
        int? height = null,
        [Description("Cycles samples.")] int? samples = null,
        [Description("Objects to frame with an automatic camera; others are hidden from the render unless isolate=false.")] string[]? objects = null,
        [Description("Angle for the automatic camera (default iso).")] string? view = null,
        [Description("Force an automatic framing camera even if the scene has one.")] bool? autoCamera = null,
        bool? isolate = null,
        CancellationToken ct = default)
    {
        var img = await blender.CallAsync("render_image", new Args
        {
            ["engine"] = engine, ["width"] = width, ["height"] = height, ["samples"] = samples, ["objects"] = objects,
            ["view"] = view, ["auto_camera"] = autoCamera ?? (objects is { Length: > 0 } ? true : null), ["isolate"] = isolate,
        }, TimeSpan.FromMinutes(10), ct);
        return Results.ImageWithCaption(img, "Render");
    }

    [McpServerTool(Name = "focus_view", Title = "Frame in viewport")]
    [Description("Point the user's 3D viewport at objects (or everything) and optionally change the view angle (FRONT, BACK, LEFT, RIGHT, TOP, BOTTOM) and shading (SOLID, MATERIAL, RENDERED, WIREFRAME).")]
    public static async Task<string> FocusView(
        BlenderConnection blender, string[]? objects = null, string? view = null, string? shading = null, CancellationToken ct = default)
        => Results.Text(await blender.CallAsync("focus_view", new Args
        {
            ["objects"] = objects, ["view"] = view, ["shading"] = shading,
        }, ct: ct));
}
