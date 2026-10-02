using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.AI;

namespace BlenderMcp.Server.Bridge;

/// <summary>Parameter bag that silently drops nulls, so optional tool args fall back to Blender-side defaults.</summary>
public sealed class Args : Dictionary<string, object?>
{
    public new object? this[string key]
    {
        get => TryGetValue(key, out var v) ? v : null;
        set
        {
            if (value is null)
            {
                Remove(key);
            }
            else
            {
                base[key] = value;
            }
        }
    }
}

public static class Results
{
    private static readonly JsonSerializerOptions Compact = new()
    {
        WriteIndented = false,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>Compact JSON text for the model.</summary>
    public static string Text(JsonNode? node) => node?.ToJsonString(Compact) ?? "null";

    /// <summary>Convert a bridge image payload {mime, width, height, base64} into MCP image content.</summary>
    public static DataContent Image(JsonNode? img)
    {
        var b64 = img?["base64"]?.GetValue<string>() ?? throw new BlenderException("Blender returned no image data");
        var mime = img["mime"]?.GetValue<string>() ?? "image/png";
        return new DataContent(Convert.FromBase64String(b64), mime);
    }

    /// <summary>Image result plus a caption describing it.</summary>
    public static IEnumerable<AIContent> ImageWithCaption(JsonNode? img, string caption) =>
    [
        new TextContent($"{caption} ({img?["width"]}x{img?["height"]})"),
        Image(img),
    ];
}
