namespace BlenderMcp.Server.Bridge;

/// <summary>Ports and folders shared by the bridge service, the per-session servers and the Blender add-on.</summary>
public static class BridgeDefaults
{
    /// <summary>The one bridge service per PC (BLENDER_MCP_BRIDGE_PORT). Not 9876: other Blender MCP add-ons use it.</summary>
    public const int ServicePort = 9870;

    /// <summary>Ports a Blender add-on listens on: 9877 first, then 9880-9899 (9878 is UnrealMcp's, 9879 the e2e test Blender's).
    /// Keep in sync with PORTS in blender_addon/blender_mcp_bridge/server.py.</summary>
    public static readonly int[] BlenderPorts = [9877, .. Enumerable.Range(9880, 20)];

    public static string DataDir =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "BlenderMcp");

    /// <summary>Each Blender writes instances\&lt;port&gt;.json (port, pid, open file) while its add-on listens.</summary>
    public static string InstancesDir => Path.Combine(DataDir, "instances");

    public static string LogFile => Path.Combine(DataDir, "bridge.log");

    public static string Version { get; } = typeof(BridgeDefaults).Assembly.GetName().Version?.ToString(3) ?? "1.0.0";
}
