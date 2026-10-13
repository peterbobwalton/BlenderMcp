using BlenderMcp.Server.Bridge;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;

// Usage:
//   BlenderMcp.Server [--timeout 60]                 (MCP server over stdio; routed to a Blender by the bridge service)
//   BlenderMcp.Server --port 9877 [--host 127.0.0.1] (MCP server talking to that one Blender directly, no bridge)
//   BlenderMcp.Server --service [--bridge-port 9870] (the bridge service; the first session starts it)
//   BlenderMcp.Server --list                         (the Blenders and sessions the bridge sees from this folder)
//   BlenderMcp.Server --selftest        (connect to Blender, print info, exit)
//   BlenderMcp.Server --register [--port 9877] [--name blender-csharp]   (add to Claude Desktop config; no --port = bridge)
//   BlenderMcp.Server --unregister [--name blender-csharp]               (remove from Claude Desktop config)
// Environment: BLENDER_MCP_HOST, BLENDER_MCP_PORT (either one = direct), BLENDER_MCP_TIMEOUT,
//   BLENDER_MCP_WORKDIR (';'-separated folders to route by instead of the working folder), BLENDER_MCP_OWNER,
//   BLENDER_MCP_BRIDGE_PORT, BLENDER_MCP_BLENDER (blender.exe the bridge starts)

var directHost = Arg("--host") ?? Environment.GetEnvironmentVariable("BLENDER_MCP_HOST");
var directPort = Arg("--port") ?? Environment.GetEnvironmentVariable("BLENDER_MCP_PORT");
var folders = (Environment.GetEnvironmentVariable("BLENDER_MCP_WORKDIR") ?? "")
    .Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
    .Append(Environment.CurrentDirectory)
    .ToList();
var owner = Environment.GetEnvironmentVariable("BLENDER_MCP_OWNER");
var options = new BlenderOptions
{
    Host = directHost ?? "127.0.0.1",
    Port = int.TryParse(directPort, out var port) ? port : 9877,
    DefaultTimeout = TimeSpan.FromSeconds(
        double.TryParse(Arg("--timeout") ?? Environment.GetEnvironmentVariable("BLENDER_MCP_TIMEOUT"), out var t) ? t : 60),
    UseBridge = directHost is null && directPort is null,
    BridgePort = int.TryParse(Arg("--bridge-port") ?? Environment.GetEnvironmentVariable("BLENDER_MCP_BRIDGE_PORT"), out var bp)
        ? bp : BridgeDefaults.ServicePort,
    Folders = folders,
    Owner = string.IsNullOrWhiteSpace(owner) ? $"Claude session in {folders[^1]} (pid {Environment.ProcessId})" : owner,
};

if (args.Contains("--service"))
{
    return BlenderMcp.Server.Service.BridgeService.Run(options.BridgePort);
}

if (args.Contains("--list"))
{
    return await ListCommand.RunAsync(options);
}

if (args.Contains("--register") || args.Contains("--unregister"))
{
    // Installer hooks: add/remove this server in Claude Desktop's config, then exit.
    return BlenderMcp.Server.ClaudeConfig.Run(args);
}

if (args.Contains("--selftest"))
{
    return await SelfTest.RunAsync(options);
}

var builder = Host.CreateApplicationBuilder(args);

// stdout carries the MCP protocol, so every log line must go to stderr.
builder.Logging.ClearProviders();
builder.Logging.AddConsole(o => o.LogToStandardErrorThreshold = LogLevel.Trace);
builder.Logging.SetMinimumLevel(LogLevel.Information);

builder.Services.AddSingleton(options);
builder.Services.AddSingleton<BlenderConnection>();

builder.Services
    .AddMcpServer(o =>
    {
        o.ServerInfo = new() { Name = "blender-csharp", Version = typeof(SelfTest).Assembly.GetName().Version?.ToString(3) ?? "1.0.0" };
        o.ServerInstructions =
            "Controls a live Blender session through typed tools. Units are metres, rotations degrees. " +
            "Start with get_scene_info. Prefer typed tools over execute_python. Use batch to build " +
            "multi-part objects in one round trip. After modelling, call render_views on the objects to " +
            "check the result visually, and validate_asset before exporting game assets. " +
            "For characters: validate_rig before export_animation_for_unreal; render_animation_frames to see motion. " +
            "Every mutating call is one Blender undo step (see undo). " +
            "Several Blenders can be open: this session gets its own (matched by the open .blend and this session's folder); " +
            "blender_session shows which one and why, and can select or open another.";
    })
    .WithStdioServerTransport()
    .WithToolsFromAssembly();

await builder.Build().RunAsync();
return 0;

string? Arg(string name)
{
    var i = Array.IndexOf(args, name);
    return i >= 0 && i + 1 < args.Length ? args[i + 1] : null;
}

internal static class SelfTest
{
    public static async Task<int> RunAsync(BlenderOptions options)
    {
        await using var conn = new BlenderConnection(options, NullLogger<BlenderConnection>.Instance);
        try
        {
            var sw = System.Diagnostics.Stopwatch.StartNew();
            var pong = await conn.CallAsync("ping");
            var first = sw.Elapsed.TotalMilliseconds;

            sw.Restart();
            const int n = 50;
            await Task.WhenAll(Enumerable.Range(0, n).Select(_ => conn.CallAsync("ping")));
            var avg = sw.Elapsed.TotalMilliseconds / n;

            var info = await conn.CallAsync("get_scene_info");
            var cmds = await conn.CallAsync("list_commands");
            var shot = await conn.CallAsync("render_views", new Args { ["views"] = new[] { "iso" }, ["size"] = 256 });
            var png = shot?["views"]?[0]?["base64"]?.GetValue<string>()?.Length ?? 0;

            Console.WriteLine($"Connected to Blender {pong?["blender"]} ({pong?["file"]}) via {options.Endpoint}");
            Console.WriteLine($"  first ping          : {first:0.0} ms");
            Console.WriteLine($"  {n} concurrent pings : {avg:0.00} ms avg");
            Console.WriteLine($"  scene               : {info?["scene"]}, objects {info?["object_counts"]?.ToJsonString()}");
            Console.WriteLine($"  bridge commands     : {cmds?["commands"]?.AsArray().Count}");
            Console.WriteLine($"  offscreen render    : {png / 1024} KB base64");
            Console.WriteLine("SELFTEST OK");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"SELFTEST FAILED: {ex.Message}");
            return 1;
        }
    }
}

internal static class ListCommand
{
    /// <summary>What the bridge service sees from this folder: Blenders, sessions, and where this folder would be routed.</summary>
    public static async Task<int> RunAsync(BlenderOptions options)
    {
        options.UseBridge = true;
        options.Owner = "BlenderMcp.Server --list";
        await using var conn = new BlenderConnection(options, NullLogger<BlenderConnection>.Instance);
        try
        {
            var status = await conn.CallAsync("bridge.status");
            Console.WriteLine(status?.ToJsonString(new System.Text.Json.JsonSerializerOptions { WriteIndented = true }));
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"FAILED: {ex.Message}");
            return 1;
        }
    }
}
