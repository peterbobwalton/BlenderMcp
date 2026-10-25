using System.Text.Json;
using System.Text.Json.Nodes;

namespace BlenderMcp.Server;

/// <summary>
/// Adds/removes this server in Claude's MCP config. Used by the installer:
/// <c>BlenderMcp.Server.exe --register</c> / <c>--unregister</c>.
/// Registers in Claude Code's user config (~\.claude.json) when Claude Code is set up: every Claude Code session
/// (including the desktop app's Code tab) then starts its own connector in its own folder, so the bridge can give
/// each session its own Blender. An entry in Claude Desktop's claude_desktop_config.json is one process the app
/// shares between all its sessions (and it shadows the Claude Code entry), so ours is removed from there.
/// <c>--desktop</c> registers in Claude Desktop's config instead (the only choice when there is no ~\.claude.json).
/// Handles both the classic install (%APPDATA%\Claude) and the Microsoft Store/MSIX install
/// (%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude). Other servers and settings are preserved.
/// </summary>
internal static class ClaudeConfig
{
    public const string DefaultName = "blender-csharp";
    private const string FileName = "claude_desktop_config.json";
    private const string ExeName = "BlenderMcp.Server";

    /// <summary>Claude Code's user-scope config, or null when Claude Code has never run (creating it would make
    /// Claude Code think it has been set up).</summary>
    public static string? FindClaudeCodeConfig()
    {
        var path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".claude.json");
        return File.Exists(path) ? path : null;
    }

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

        List<string> desktop;
        try
        {
            desktop = FindConfigFiles();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"FAILED: cannot locate Claude config: {ex.Message}");
            return 1;
        }
        var code = args.Contains("--desktop") ? null : FindClaudeCodeConfig();

        var failures = 0;
        void Apply(string path, Func<bool> change)
        {
            try
            {
                Console.WriteLine($"{(change() ? "updated" : "unchanged")}: {path}");
            }
            catch (Exception ex)
            {
                failures++;
                Console.Error.WriteLine($"FAILED: {path}: {ex.Message}");
            }
        }

        if (!register)
        {
            foreach (var path in desktop.Where(File.Exists))
            {
                Apply(path, () => Unregister(path, name));
            }
            if (FindClaudeCodeConfig() is { } codePath)
            {
                Apply(codePath, () => Unregister(codePath, name, onlyOurs: true));
            }
        }
        else if (code is not null)
        {
            Apply(code, () => Register(code, name, exe, port));
            // A desktop entry of the same name shadows this one and is shared by every session: drop ours.
            foreach (var path in desktop.Where(File.Exists))
            {
                Apply(path, () => Unregister(path, name, onlyOurs: true));
            }
        }
        else
        {
            foreach (var path in desktop)
            {
                Apply(path, () => Register(path, name, exe, port));
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
        if (port is null)
        {
            // no port: the bridge picks the Blender, so drop a --port left by an older install
            if (entry["args"] is JsonArray old)
            {
                var i = old.Select(a => a?.ToString()).ToList().IndexOf("--port");
                if (i >= 0)
                {
                    old.RemoveAt(i);
                    if (i < old.Count)
                    {
                        old.RemoveAt(i);
                    }
                }
            }
        }
        else
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

    /// <param name="onlyOurs">leave an entry of that name alone unless it runs BlenderMcp.Server</param>
    public static bool Unregister(string path, string name, bool onlyOurs = false)
    {
        if (!File.Exists(path))
        {
            return false;
        }
        var root = Load(path);
        if (root["mcpServers"] is not JsonObject servers || servers[name] is not JsonNode entry
            || (onlyOurs && !IsOurs(entry)) || !servers.Remove(name))
        {
            return false;
        }
        Save(path, root);
        return true;
    }

    private static bool IsOurs(JsonNode entry) =>
        entry["command"]?.GetValueKind() == JsonValueKind.String
        && Path.GetFileNameWithoutExtension(entry["command"]!.GetValue<string>())
            .Equals(ExeName, StringComparison.OrdinalIgnoreCase);

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
