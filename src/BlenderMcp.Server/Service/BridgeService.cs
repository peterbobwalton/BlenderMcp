using System.Collections.Concurrent;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using BlenderMcp.Server.Bridge;

namespace BlenderMcp.Server.Service;

/// <summary>Connection from the service to the Blender a session is routed to.</summary>
internal sealed class Upstream(TcpClient client, BlenderInstance target)
{
    public TcpClient Client { get; } = client;
    public NetworkStream Stream { get; } = client.GetStream();
    public BlenderInstance Target { get; } = target;
    public SemaphoreSlim WriteLock { get; } = new(1, 1);

    /// <summary>Commands sent and not answered yet (id -> command), failed when the connection drops.</summary>
    public ConcurrentDictionary<long, string> InFlight { get; } = new();
    public volatile bool Dead;

    public void Close()
    {
        Dead = true;
        Stream.Dispose();
        Client.Dispose();
    }
}

/// <summary>
/// BlenderMcp.Server.exe --service: the one bridge service on the PC (127.0.0.1:9870). Every Claude session's
/// BlenderMcp.Server connects to it with the add-on's own line protocol, says who it is (bridge.hello: session id,
/// folders, owner), and the service forwards its commands to the Blender the Router picked for it. bridge.* commands
/// (status, select, open, auto, release) are answered here. The session ends when its connection closes; the Blender
/// is then free for other sessions. Exits after 10 minutes without sessions; the next session starts it again.
/// </summary>
internal static class BridgeService
{
    private static readonly TimeSpan IdleExit = TimeSpan.FromMinutes(10);
    private static readonly Router Router = new();

    public static int Run(int port)
    {
        ServiceLog.File = BridgeDefaults.LogFile;
        var listener = new TcpListener(IPAddress.Loopback, port) { ExclusiveAddressUse = true };
        try
        {
            listener.Start();
        }
        catch (SocketException ex)
        {
            ServiceLog.Write($"port {port} is taken ({ex.SocketErrorCode}); another bridge service is probably running");
            return 0;
        }
        ServiceLog.Write($"bridge service {BridgeDefaults.Version} listening on 127.0.0.1:{port} (process {Environment.ProcessId})");

        _ = Task.Run(async () =>
        {
            while (true)
            {
                await Task.Delay(TimeSpan.FromSeconds(30)).ConfigureAwait(false);
                if (Router.SessionCount == 0 && DateTime.UtcNow - Router.IdleSince > IdleExit)
                {
                    ServiceLog.Write("no sessions for 10 minutes; exiting");
                    Environment.Exit(0);
                }
            }
        });

        while (true)
        {
            var client = listener.AcceptTcpClient();
            client.NoDelay = true;
            _ = Task.Run(() => HandleClientAsync(client));
        }
    }

    private sealed class ClientWriter(NetworkStream stream)
    {
        private readonly SemaphoreSlim _lock = new(1, 1);

        public async Task WriteAsync(ReadOnlyMemory<byte> line)
        {
            await _lock.WaitAsync().ConfigureAwait(false);
            try
            {
                await stream.WriteAsync(line).ConfigureAwait(false);
                if (line.Length == 0 || line.Span[^1] != (byte)'\n')
                {
                    await stream.WriteAsync("\n"u8.ToArray()).ConfigureAwait(false);
                }
            }
            catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException)
            {
                // session gone; its read loop ends it
            }
            finally
            {
                _lock.Release();
            }
        }

        public Task ReplyAsync(JsonNode? id, JsonNode? result) => WriteAsync(Encode(new JsonObject
        {
            ["id"] = id?.DeepClone(), ["ok"] = true, ["result"] = result,
        }));

        public Task ErrorAsync(JsonNode? id, string error) => WriteAsync(Encode(new JsonObject
        {
            ["id"] = id?.DeepClone(), ["ok"] = false, ["error"] = error,
        }));

        private static byte[] Encode(JsonObject o) => Encoding.UTF8.GetBytes(o.ToJsonString() + "\n");
    }

    private static async Task HandleClientAsync(TcpClient client)
    {
        Session? session = null;
        using (client)
        {
            var stream = client.GetStream();
            var writer = new ClientWriter(stream);
            try
            {
                await foreach (var line in ReadLinesAsync(stream).ConfigureAwait(false))
                {
                    JsonObject? req;
                    try
                    {
                        req = JsonNode.Parse(line) as JsonObject;
                    }
                    catch (JsonException ex)
                    {
                        await writer.ErrorAsync(null, $"Bad JSON: {ex.Message}").ConfigureAwait(false);
                        continue;
                    }
                    var id = req?["id"];
                    string? cmd = null;
                    if (req?["cmd"] is JsonValue cmdNode && cmdNode.TryGetValue<string>(out var c))
                    {
                        cmd = c;
                    }
                    if (req is null || cmd is null)
                    {
                        await writer.ErrorAsync(id, "Request must be an object with a string 'cmd'").ConfigureAwait(false);
                        continue;
                    }

                    if (session is null)
                    {
                        if (cmd != "bridge.hello")
                        {
                            await writer.ErrorAsync(id,
                                "This is the Blender bridge service: say bridge.hello first, or connect to a Blender port directly.").ConfigureAwait(false);
                            continue;
                        }
                        session = Hello(req["params"] as JsonObject);
                        await writer.ReplyAsync(id, new JsonObject { ["bridge"] = BridgeDefaults.Version }).ConfigureAwait(false);
                        continue;
                    }

                    session.LastCall = DateTime.Now;
                    if (cmd.StartsWith("bridge.", StringComparison.Ordinal))
                    {
                        var s = session;
                        _ = Task.Run(() => BridgeCommandAsync(s, writer, id, cmd, req["params"] as JsonObject));
                        continue;
                    }
                    // in order, so pipelined commands reach Blender in the order they were sent
                    await ForwardAsync(session, writer, line, id, cmd).ConfigureAwait(false);
                }
            }
            catch (Exception ex) when (ex is IOException or SocketException or ObjectDisposedException)
            {
            }
            catch (Exception ex)
            {
                ServiceLog.Write($"session handler crashed: {ex}");
            }
            finally
            {
                if (session is not null)
                {
                    session.Link?.Close();
                    Router.Remove(session);
                }
            }
        }
    }

    private static Session Hello(JsonObject? p)
    {
        var id = (string?)p?["session"] ?? Guid.NewGuid().ToString("N");
        var folders = (p?["folders"] as JsonArray)?.Select(f => (string?)f).Where(f => !string.IsNullOrWhiteSpace(f))
            .Select(f => Launcher.Normalize(f!)).ToList() ?? [];
        var owner = (string?)p?["owner"];
        if (string.IsNullOrWhiteSpace(owner))
        {
            owner = $"Claude session in {folders.LastOrDefault() ?? "?"}";
        }
        return Router.Add(id, folders, owner);
    }

    private static async Task ForwardAsync(Session s, ClientWriter writer, byte[] line, JsonNode? id, string cmd)
    {
        Upstream link;
        await s.RouteLock.WaitAsync().ConfigureAwait(false);
        try
        {
            link = await EnsureUpstreamAsync(s, writer).ConfigureAwait(false);
        }
        catch (BridgeError ex)
        {
            await writer.ErrorAsync(id, ex.Message).ConfigureAwait(false);
            return;
        }
        finally
        {
            s.RouteLock.Release();
        }

        long? key = null;
        if (id is JsonValue idNode && idNode.TryGetValue<long>(out var k))
        {
            key = k;
        }
        if (key is { } tracked)
        {
            link.InFlight[tracked] = cmd;
        }
        await link.WriteLock.WaitAsync().ConfigureAwait(false);
        try
        {
            await link.Stream.WriteAsync(line).ConfigureAwait(false);
            await link.Stream.WriteAsync("\n"u8.ToArray()).ConfigureAwait(false);
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException)
        {
            if (key is { } tracked2)
            {
                link.InFlight.TryRemove(tracked2, out _);
            }
            link.Close();
            await writer.ErrorAsync(id, $"Lost the connection to Blender (port {link.Target.Port}) while sending '{cmd}'; " +
                "it was probably not run. Check the scene before retrying.").ConfigureAwait(false);
        }
        finally
        {
            link.WriteLock.Release();
        }
    }

    /// <summary>The session's live connection to its Blender, routing and connecting when there is none.</summary>
    private static async Task<Upstream> EnsureUpstreamAsync(Session s, ClientWriter writer)
    {
        if (s.Link is { Dead: false } live)
        {
            return live;
        }
        var target = await Router.ChooseAsync(s).ConfigureAwait(false);
        var client = new TcpClient { NoDelay = true, ReceiveBufferSize = 1 << 20 };
        try
        {
            using var cts = new CancellationTokenSource(3000);
            await client.ConnectAsync("127.0.0.1", target.Port, cts.Token).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            client.Dispose();
            throw new BridgeError($"Cannot connect to the Blender on port {target.Port} ({target.FileLabel}): {ex.Message}");
        }
        var link = new Upstream(client, target);
        s.Link = link;
        _ = Task.Run(() => PumpUpstreamAsync(link, writer));
        return link;
    }

    /// <summary>Copies Blender's replies back to the session; fails what was still running if Blender goes away.</summary>
    private static async Task PumpUpstreamAsync(Upstream link, ClientWriter writer)
    {
        try
        {
            await foreach (var line in ReadLinesAsync(link.Stream).ConfigureAwait(false))
            {
                if (TryReadLeadingId(line, out var id))
                {
                    link.InFlight.TryRemove(id, out _);
                }
                await writer.WriteAsync(line).ConfigureAwait(false);
            }
        }
        catch (Exception ex) when (ex is IOException or SocketException or ObjectDisposedException or OperationCanceledException)
        {
        }
        link.Close();
        foreach (var (id, cmd) in link.InFlight)
        {
            await writer.ErrorAsync(JsonValue.Create(id),
                $"Connection to Blender (port {link.Target.Port}) was lost while '{cmd}' was running; it may or may not have completed. " +
                "Check the scene (e.g. get_scene_info) before retrying.").ConfigureAwait(false);
        }
        link.InFlight.Clear();
    }

    private static async Task BridgeCommandAsync(Session s, ClientWriter writer, JsonNode? id, string cmd, JsonObject? p)
    {
        try
        {
            JsonNode? result;
            switch (cmd)
            {
                case "bridge.status":
                    result = await Router.StatusAsync(s).ConfigureAwait(false);
                    break;
                case "bridge.select":
                {
                    var target = (string?)p?["target"];
                    if (string.IsNullOrWhiteSpace(target))
                    {
                        throw new BridgeError("select needs target: a port or (part of) the open file's name.");
                    }
                    result = await SwitchAsync(s, () => Router.SelectAsync(s, target)).ConfigureAwait(false);
                    break;
                }
                case "bridge.open":
                    result = await SwitchAsync(s, () => Router.OpenAsync(s, (string?)p?["path"])).ConfigureAwait(false);
                    break;
                case "bridge.auto":
                case "bridge.release":
                    await s.RouteLock.WaitAsync().ConfigureAwait(false);
                    try
                    {
                        var was = s.Port;
                        s.Link?.Close();
                        s.Link = null;
                        Router.Auto(s);
                        result = new JsonObject
                        {
                            ["released_port"] = was,
                            ["next"] = "the next call routes by this session's folder again",
                        };
                    }
                    finally
                    {
                        s.RouteLock.Release();
                    }
                    break;
                default:
                    throw new BridgeError($"Unknown bridge command '{cmd}'");
            }
            await writer.ReplyAsync(id, result).ConfigureAwait(false);
        }
        catch (BridgeError ex)
        {
            await writer.ErrorAsync(id, ex.Message).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            ServiceLog.Write($"{cmd} failed: {ex}");
            await writer.ErrorAsync(id, $"{cmd} failed: {ex.Message}").ConfigureAwait(false);
        }
    }

    /// <summary>Re-routes the session (select/open), dropping its connection to the old Blender.</summary>
    private static async Task<JsonNode> SwitchAsync(Session s, Func<Task<BlenderInstance>> choose)
    {
        await s.RouteLock.WaitAsync().ConfigureAwait(false);
        try
        {
            var i = await choose().ConfigureAwait(false);
            if (s.Link is { } old && (old.Target.Port != i.Port || old.Target.Pid != i.Pid))
            {
                old.Close();
                s.Link = null;
            }
            var o = i.ToJson();
            o["why"] = s.Reason;
            return o;
        }
        finally
        {
            s.RouteLock.Release();
        }
    }

    private static async IAsyncEnumerable<byte[]> ReadLinesAsync(NetworkStream stream)
    {
        var buffer = new byte[1 << 16];
        var line = new MemoryStream();
        while (true)
        {
            var n = await stream.ReadAsync(buffer).ConfigureAwait(false);
            if (n == 0)
            {
                yield break;
            }
            var start = 0;
            for (var i = 0; i < n; i++)
            {
                if (buffer[i] != (byte)'\n')
                {
                    continue;
                }
                line.Write(buffer, start, i - start);
                start = i + 1;
                if (line.Length > 0)
                {
                    yield return line.ToArray();
                }
                line.SetLength(0);
            }
            line.Write(buffer, start, n - start);
        }
    }

    private static bool TryReadLeadingId(ReadOnlySpan<byte> json, out long id)
    {
        id = 0;
        try
        {
            var reader = new Utf8JsonReader(json);
            return reader.Read() && reader.TokenType == JsonTokenType.StartObject
                && reader.Read() && reader.TokenType == JsonTokenType.PropertyName && reader.ValueTextEquals("id"u8)
                && reader.Read() && reader.TokenType == JsonTokenType.Number && reader.TryGetInt64(out id);
        }
        catch (JsonException)
        {
            return false;
        }
    }
}
