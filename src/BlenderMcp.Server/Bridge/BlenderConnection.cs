using System.Collections.Concurrent;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using ModelContextProtocol;

namespace BlenderMcp.Server.Bridge;

public sealed class BlenderOptions
{
    public string Host { get; set; } = "127.0.0.1";
    public int Port { get; set; } = 9877;
    public TimeSpan DefaultTimeout { get; set; } = TimeSpan.FromSeconds(60);
    public TimeSpan ConnectTimeout { get; set; } = TimeSpan.FromSeconds(3);
}

/// <summary>Raised when Blender reports a failure; surfaced to the model as a tool error.</summary>
public sealed class BlenderException(string message) : McpException(message);

/// <summary>
/// One persistent, multiplexed TCP connection to the Blender add-on.
/// Requests carry an id; responses are matched back by id, so many tool calls
/// can be in flight at once. Reconnects transparently if Blender restarts.
/// </summary>
public sealed class BlenderConnection(BlenderOptions options, ILogger<BlenderConnection> log) : IAsyncDisposable
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = null,
    };

    /// <summary>State of one socket. Pending requests belong to the link they were sent on,
    /// so a dying old link can never fail requests that were sent on its replacement.</summary>
    private sealed class Link(TcpClient client)
    {
        public TcpClient Client { get; } = client;
        public NetworkStream Stream { get; } = client.GetStream();
        public ConcurrentDictionary<long, TaskCompletionSource<JsonObject>> Pending { get; } = new();
        public CancellationTokenSource ReaderCts { get; } = new();
        public volatile bool Dead;

        public void Kill(Exception reason)
        {
            Dead = true;
            ReaderCts.Cancel();
            Stream.Dispose();
            Client.Dispose();
            foreach (var (_, tcs) in Pending)
            {
                tcs.TrySetException(reason);
            }
        }
    }

    /// <summary>The request never reached Blender, so it is safe to send again.</summary>
    private sealed class NotSentException(Exception inner) : Exception("send failed", inner);

    private readonly SemaphoreSlim _connectLock = new(1, 1);
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private volatile Link? _link;
    private long _nextId;

    public BlenderOptions Options => options;

    public bool IsConnected => _link is { Dead: false };

    public async Task<JsonNode?> CallAsync(string cmd, object? parameters = null, TimeSpan? timeout = null, CancellationToken ct = default)
    {
        // A stale socket (Blender restarted) fails at write time: the command never reached Blender,
        // so it is safe to reconnect and send once more. Commands that WERE delivered are never
        // retried, because they may not be idempotent (create, export...).
        for (var attempt = 0; ; attempt++)
        {
            try
            {
                return await SendOnceAsync(cmd, parameters, timeout, ct).ConfigureAwait(false);
            }
            catch (NotSentException) when (attempt == 0 && !ct.IsCancellationRequested)
            {
            }
            catch (NotSentException ex)
            {
                throw new BlenderException($"Could not send '{cmd}' to Blender: {ex.InnerException?.Message}");
            }
        }
    }

    private async Task<JsonNode?> SendOnceAsync(string cmd, object? parameters, TimeSpan? timeout, CancellationToken ct)
    {
        var link = await EnsureConnectedAsync(ct).ConfigureAwait(false);
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        link.Pending[id] = tcs;

        try
        {
            var body = JsonSerializer.SerializeToUtf8Bytes(new { id, cmd, @params = parameters ?? new { } }, Json);
            var payload = new byte[body.Length + 1];
            body.CopyTo(payload, 0);
            payload[^1] = (byte)'\n';

            await _writeLock.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                if (link.Dead)
                {
                    throw new NotSentException(new IOException("connection closed"));
                }
                // Not cancellable: a write abandoned half-way would leave a partial line on a live socket.
                await link.Stream.WriteAsync(payload, CancellationToken.None).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is IOException or ObjectDisposedException or SocketException)
            {
                link.Kill(new IOException("Connection to Blender lost", ex));
                throw new NotSentException(ex);
            }
            finally
            {
                _writeLock.Release();
            }

            var limit = timeout ?? options.DefaultTimeout;
            using var timeoutCts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeoutCts.CancelAfter(limit);
            JsonObject response;
            try
            {
                response = await tcs.Task.WaitAsync(timeoutCts.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (!ct.IsCancellationRequested)
            {
                throw new BlenderException(
                    $"Blender did not answer '{cmd}' within {limit.TotalSeconds:0}s. " +
                    "It may be busy (long render/export) or showing a modal dialog.");
            }
            catch (IOException)
            {
                throw new BlenderException(
                    $"Connection to Blender was lost while '{cmd}' was running; it may or may not have completed. " +
                    "Check the scene (e.g. get_scene_info) before retrying.");
            }

            if (response["ok"] is JsonValue okNode && okNode.TryGetValue<bool>(out var ok) && ok)
            {
                return response["result"];
            }

            var error = response["error"]?.ToString() ?? "Unknown Blender error";
            var trace = response["trace"]?.ToString();
            if (trace is not null)
            {
                log.LogWarning("Blender traceback for {Cmd}: {Trace}", cmd, trace);
            }
            throw new BlenderException(error);
        }
        finally
        {
            link.Pending.TryRemove(id, out _);
        }
    }

    private async Task<Link> EnsureConnectedAsync(CancellationToken ct)
    {
        var link = _link;
        if (link is { Dead: false })
        {
            return link;
        }

        await _connectLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            link = _link;
            if (link is { Dead: false })
            {
                return link;
            }

            link?.Kill(new IOException("Connection to Blender closed"));
            var client = new TcpClient { NoDelay = true, ReceiveBufferSize = 1 << 20 };
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(options.ConnectTimeout);
            try
            {
                await client.ConnectAsync(options.Host, options.Port, cts.Token).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                client.Dispose();
                if (ct.IsCancellationRequested)
                {
                    throw;
                }
                throw new BlenderException(
                    $"Cannot reach Blender at {options.Host}:{options.Port} ({ex.GetType().Name}). Open Blender and make sure the " +
                    "'Blender MCP Bridge (C#)' add-on is enabled and started (View3D > Sidebar > MCP).");
            }

            link = new Link(client);
            _ = Task.Run(() => ReadLoopAsync(link));
            await VerifyPeerAsync(link, ct).ConfigureAwait(false);
            _link = link;
            log.LogInformation("Connected to Blender bridge at {Host}:{Port}", options.Host, options.Port);
            return link;
        }
        finally
        {
            _connectLock.Release();
        }
    }

    /// <summary>Make sure the socket is the Blender bridge and not another app (e.g. another MCP bridge
    /// listening on the same port) before any command - especially execute_python - is sent to it.</summary>
    private async Task VerifyPeerAsync(Link link, CancellationToken ct)
    {
        var id = Interlocked.Increment(ref _nextId);
        var tcs = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        link.Pending[id] = tcs;
        string? blender = null;
        var timedOut = false;
        try
        {
            var ping = JsonSerializer.SerializeToUtf8Bytes(new { id, cmd = "ping", @params = new { } }, Json);
            await link.Stream.WriteAsync(ping.Append((byte)'\n').ToArray(), ct).ConfigureAwait(false);
            var reply = await tcs.Task.WaitAsync(options.ConnectTimeout, ct).ConfigureAwait(false);
            blender = reply["result"]?["blender"]?.ToString();
        }
        catch (TimeoutException)
        {
            timedOut = true;
        }
        catch (Exception ex) when (ex is IOException or SocketException)
        {
        }
        catch
        {
            link.Kill(new IOException("connect cancelled"));  // don't leak the socket and its read loop
            throw;
        }
        finally
        {
            link.Pending.TryRemove(id, out _);
        }

        if (timedOut)
        {
            link.Kill(new IOException("no reply to ping"));
            throw new BlenderException(
                $"Something is listening on {options.Host}:{options.Port} but did not answer within {options.ConnectTimeout.TotalSeconds:0}s. " +
                "Blender may be busy (loading a file, rendering, a modal dialog) - try again shortly. If it keeps happening, " +
                "another app may be using that port; pick a free port in Blender (View3D > Sidebar > MCP) and pass the same --port to the server.");
        }

        if (string.IsNullOrEmpty(blender))
        {
            link.Kill(new IOException("not a Blender bridge"));
            throw new BlenderException(
                $"Something other than the Blender MCP bridge is listening on {options.Host}:{options.Port}. " +
                "Pick a free port in Blender (View3D > Sidebar > MCP) and pass the same --port to the server.");
        }
    }

    private async Task ReadLoopAsync(Link link)
    {
        var buffer = new byte[1 << 16];
        var line = new MemoryStream();
        Exception reason = new IOException("Connection to Blender closed");
        try
        {
            while (!link.ReaderCts.IsCancellationRequested)
            {
                var n = await link.Stream.ReadAsync(buffer, link.ReaderCts.Token).ConfigureAwait(false);
                if (n == 0)
                {
                    break;
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
                    Dispatch(link, line.GetBuffer().AsSpan(0, (int)line.Length));
                    line.SetLength(0);
                }

                line.Write(buffer, start, n - start);
            }
        }
        catch (Exception ex) when (ex is IOException or ObjectDisposedException or OperationCanceledException or SocketException)
        {
            reason = new IOException("Connection to Blender closed", ex);
        }
        catch (Exception ex)
        {
            log.LogError(ex, "Blender reader crashed");
            reason = new IOException("Connection to Blender failed", ex);
        }

        // Mark dead so the next call reconnects instead of writing into a half-closed socket.
        link.Kill(reason);
    }

    private void Dispatch(Link link, ReadOnlySpan<byte> json)
    {
        if (json.IsEmpty)
        {
            return;
        }

        try
        {
            if (JsonNode.Parse(json) is not JsonObject obj)
            {
                return;
            }

            if (obj["id"] is JsonValue idNode && idNode.TryGetValue<long>(out var key) && link.Pending.TryGetValue(key, out var tcs))
            {
                tcs.TrySetResult(obj);
            }
            else
            {
                // Late reply to a timed-out call, or a protocol error with no id.
                log.LogWarning("Unmatched message from Blender: {Msg}", Encoding.UTF8.GetString(json[..Math.Min(json.Length, 300)]));
            }
        }
        catch (JsonException ex)
        {
            log.LogWarning(ex, "Bad JSON from Blender");
            // Fail the request now rather than letting it wait for its timeout, if the id can still be read.
            if (TryReadLeadingId(json, out var key) && link.Pending.TryGetValue(key, out var tcs))
            {
                tcs.TrySetResult(new JsonObject { ["ok"] = false, ["error"] = $"Blender sent a reply that is not valid JSON: {ex.Message}" });
            }
        }
    }

    /// <summary>Reads the "id" when it is the first property, which still works if the JSON is broken later on.</summary>
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

    public ValueTask DisposeAsync()
    {
        _link?.Kill(new ObjectDisposedException(nameof(BlenderConnection)));
        _link = null;
        return ValueTask.CompletedTask;
    }
}
