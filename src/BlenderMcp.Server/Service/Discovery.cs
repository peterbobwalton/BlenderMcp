using System.Diagnostics;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using BlenderMcp.Server.Bridge;

namespace BlenderMcp.Server.Service;

/// <summary>One running Blender with the add-on listening. File is the open .blend ("" = unsaved scene).</summary>
internal sealed record BlenderInstance(int Port, int Pid, string File, bool Dirty, string Blender, bool Announced, bool Answered)
{
    public string FileLabel => File.Length > 0 ? File : "(unsaved scene)";

    public JsonObject ToJson() => new()
    {
        ["port"] = Port,
        ["pid"] = Pid,
        ["file"] = FileLabel,
        ["unsaved_changes"] = Dirty,
        ["blender"] = Blender,
        ["answered"] = Answered,
    };
}

/// <summary>
/// Finds the running Blenders: reads the add-on's announcement files (instances\&lt;port&gt;.json), then asks every
/// candidate port which file it has open (ping returns pid and file). A Blender too busy to answer (rendering, modal
/// dialog) keeps what its announcement says. Cached for 2 s.
/// </summary>
internal static class Discovery
{
    private static readonly SemaphoreSlim Gate = new(1, 1);
    private static List<BlenderInstance>? _cache;
    private static DateTime _cacheAt;

    public static async Task<List<BlenderInstance>> ListAsync(bool fresh = false)
    {
        await Gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (!fresh && _cache is not null && DateTime.UtcNow - _cacheAt < TimeSpan.FromSeconds(2))
            {
                return _cache;
            }
            _cache = await ScanAsync().ConfigureAwait(false);
            _cacheAt = DateTime.UtcNow;
            return _cache;
        }
        finally
        {
            Gate.Release();
        }
    }

    private sealed record Announcement(int Port, int Pid, string File, string Blender);

    private static async Task<List<BlenderInstance>> ScanAsync()
    {
        var announced = ReadAnnouncements();
        var ports = BridgeDefaults.BlenderPorts.Union(announced.Keys).Distinct().ToList();
        var pings = await Task.WhenAll(ports.Select(PingAsync)).ConfigureAwait(false);

        var found = new List<BlenderInstance>();
        for (var i = 0; i < ports.Count; i++)
        {
            var port = ports[i];
            var pong = pings[i];
            announced.TryGetValue(port, out var a);
            if (pong is { } p)
            {
                found.Add(p with { Announced = a is not null, Pid = p.Pid != 0 ? p.Pid : a?.Pid ?? 0 });
            }
            else if (a is not null && IsBlender(a.Pid))
            {
                // listening but busy: trust the announcement
                found.Add(new BlenderInstance(port, a.Pid, a.File, false, a.Blender, true, false));
            }
        }
        return found;
    }

    private static Dictionary<int, Announcement> ReadAnnouncements()
    {
        var result = new Dictionary<int, Announcement>();
        try
        {
            if (!Directory.Exists(BridgeDefaults.InstancesDir))
            {
                return result;
            }
            foreach (var path in Directory.EnumerateFiles(BridgeDefaults.InstancesDir, "*.json"))
            {
                try
                {
                    var o = JsonNode.Parse(File.ReadAllText(path)) as JsonObject;
                    var port = (int?)o?["port"] ?? 0;
                    var pid = (int?)o?["pid"] ?? 0;
                    if (port <= 0 || !IsBlender(pid))
                    {
                        File.Delete(path);  // left behind by a Blender that crashed
                        continue;
                    }
                    result[port] = new Announcement(port, pid, (string?)o?["file"] ?? "", (string?)o?["blender"] ?? "");
                }
                catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or System.Text.Json.JsonException)
                {
                }
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
        }
        return result;
    }

    public static bool IsBlender(int pid)
    {
        if (pid <= 0)
        {
            return false;
        }
        try
        {
            using var p = Process.GetProcessById(pid);
            return !p.HasExited && p.ProcessName.Contains("blender", StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception)
        {
            return false;
        }
    }

    /// <summary>Asks the add-on on a port for its pid and open file; null when nothing (or not the add-on) answers.</summary>
    private static async Task<BlenderInstance?> PingAsync(int port)
    {
        using var client = new TcpClient { NoDelay = true };
        try
        {
            using (var cts = new CancellationTokenSource(300))
            {
                await client.ConnectAsync("127.0.0.1", port, cts.Token).ConfigureAwait(false);
            }
            var stream = client.GetStream();
            await stream.WriteAsync("{\"id\":1,\"cmd\":\"ping\",\"params\":{}}\n"u8.ToArray()).ConfigureAwait(false);
            using var read = new CancellationTokenSource(1500);
            var buffer = new byte[16384];
            var line = new MemoryStream();
            while (true)
            {
                var n = await stream.ReadAsync(buffer, read.Token).ConfigureAwait(false);
                if (n == 0)
                {
                    return null;
                }
                var nl = Array.IndexOf(buffer, (byte)'\n', 0, n);
                line.Write(buffer, 0, nl < 0 ? n : nl);
                if (nl >= 0 || line.Length > 1 << 20)
                {
                    break;
                }
            }
            var reply = JsonNode.Parse(Encoding.UTF8.GetString(line.ToArray()));
            var r = reply?["result"];
            if (r?["blender"] is null)
            {
                return null;
            }
            return new BlenderInstance(port, (int?)r["pid"] ?? 0, (string?)r["file"] ?? "", (bool?)r["dirty"] ?? false,
                (string?)r["blender"] ?? "", false, true);
        }
        catch (Exception)
        {
            return null;
        }
    }
}
