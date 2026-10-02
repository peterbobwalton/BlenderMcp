using System.Text.Json;
using System.Text.Json.Nodes;

namespace BlenderMcp.Server;

/// <summary>
/// Adds/removes this server in Claude Desktop's claude_desktop_config.json.
/// Used by the installer: <c>BlenderMcp.Server.exe --register</c> / <c>--unregister</c>.
/// Handles both the classic install (%APPDATA%\Claude) and the Microsoft Store/MSIX install
/// (%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude). Other servers and settings are preserved.
/// </summary>
internal static class ClaudeConfig
{
    public const string DefaultName = "blender-csharp";
    private const string FileName = "claude_desktop_config.json";

    private static readonly JsonSerializerOptions Pretty = new()
    {
        WriteIndented = true,
        Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    private static readonly JsonDocumentOptions Lenient = new()
    {
        AllowTrailingCommas = true,
        CommentHandling = JsonCommentHandling.Skip,
    };

    /// <summary>Config files to update: every one that exists, else the one Claude would create.</summary>
    public static List<string> FindConfigFiles()
    {
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var classic = Path.Combine(appData, "Claude", FileName);

        var packaged = new List<string>();
        var packages = Path.Combine(localAppData, "Packages");
        if (Directory.Exists(packages))
        {
            foreach (var pkg in Directory.EnumerateDirectories(packages, "Claude_*"))
            {
                packaged.Add(Path.Combine(pkg, "LocalCache", "Roaming", "Claude", FileName));
            }
        }

        var existing = packaged.Append(classic).Where(File.Exists).ToList();
        if (existing.Count > 0)
        {
            return existing;
        }

        // Nothing yet: create where the installed Claude will look.
        return packaged.Count > 0 ? [packaged[0]] : [classic];
    }

    public static int Run(string[] args)
    {
        var register = args.Contains("--register");
        var name = ArgValue(args, "--name") ?? DefaultName;
        var port = ArgValue(args, "--port");
        var exe = ArgValue(args, "--exe") ?? Environment.ProcessPath
            ?? throw new InvalidOperationException("Cannot determine executable path");

        List<string> paths;
        try
        {
            paths = FindConfigFiles();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"FAILED: cannot locate Claude config: {ex.Message}");
            return 1;
        }

        var failures = 0;
        foreach (var path in paths)
        {
            try
            {
                var changed = register ? Register(path, name, exe, port) : Unregister(path, name);
                Console.WriteLine($"{(changed ? "updated" : "unchanged")}: {path}");
            }
            catch (Exception ex)
            {
                failures++;
                Console.Error.WriteLine($"FAILED: {path}: {ex.Message}");
            }
        }
        return failures == 0 ? 0 : 1;
    }

    public static bool Register(string path, string name, string exe, string? port)
    {
        var root = Load(path);
        var servers = Servers(root);

        // Update only what we own (command, --port) so the user's own additions (env, extra args) survive upgrades.
        var current = servers[name] as JsonObject;
        var entry = current?.DeepClone() as JsonObject ?? new JsonObject();
        entry["command"] = exe;
        if (port is not null)
        {
            var args = entry["args"] as JsonArray ?? new JsonArray();
            var i = args.Select(a => a?.ToString()).ToList().IndexOf("--port");
            if (i < 0)
            {
                args.Add("--port");
                args.Add(port);
            }
            else if (i + 1 < args.Count)
            {
                args[i + 1] = port;
            }
            else
            {
                args.Add(port);
            }
            entry["args"] = args;
        }

        if (current is not null && JsonNode.DeepEquals(current, entry))
        {
            return false;
        }
        servers[name] = entry;
        Save(path, root);
        return true;
    }

    public static bool Unregister(string path, string name)
    {
        if (!File.Exists(path))
        {
            return false;
        }
        var root = Load(path);
        if (root["mcpServers"] is not JsonObject servers || !servers.Remove(name))
        {
            return false;
        }
        Save(path, root);
        return true;
    }

    private static JsonObject Load(string path)
    {
        if (!File.Exists(path))
        {
            return new JsonObject();
        }
        var text = File.ReadAllText(path);
        if (string.IsNullOrWhiteSpace(text))
        {
            return new JsonObject();
        }
        return JsonNode.Parse(text, documentOptions: Lenient) as JsonObject
            ?? throw new InvalidDataException("config root is not a JSON object");
    }

    /// <summary>The top-level mcpServers object, repairing a common hand-editing mistake
    /// (a second "mcpServers" nested inside the first, which Claude ignores).</summary>
    private static JsonObject Servers(JsonObject root)
    {
        if (root["mcpServers"] is not JsonObject servers)
        {
            servers = new JsonObject();
            root["mcpServers"] = servers;
        }
        if (servers["mcpServers"] is JsonObject nested)
        {
            servers.Remove("mcpServers");
            foreach (var (key, value) in nested.ToList())
            {
                nested.Remove(key);
                if (!servers.ContainsKey(key))
                {
                    servers[key] = value;
                }
            }
        }
        return servers;
    }

    private static void Save(string path, JsonObject root)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        if (File.Exists(path))
        {
            File.Copy(path, $"{path}.bak-{DateTime.Now:yyyyMMdd-HHmmss}", overwrite: true);
        }
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, root.ToJsonString(Pretty));
        File.Move(tmp, path, overwrite: true);
    }

    private static string? ArgValue(string[] args, string key)
    {
        var i = Array.IndexOf(args, key);
        return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
    }
}
