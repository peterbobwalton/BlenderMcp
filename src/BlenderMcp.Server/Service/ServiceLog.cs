namespace BlenderMcp.Server.Service;

/// <summary>The service has no console: it logs to %LOCALAPPDATA%\BlenderMcp\bridge.log (kept under 2 MB).</summary>
internal static class ServiceLog
{
    private static readonly object Lock = new();

    /// <summary>Null: write to stderr (--list, tests).</summary>
    public static string? File { get; set; }

    public static void Write(string message)
    {
        var line = $"[BlenderBridge {DateTime.Now:HH:mm:ss}] {message}";
        lock (Lock)
        {
            try
            {
                if (File is null)
                {
                    Console.Error.WriteLine(line);
                    return;
                }
                Directory.CreateDirectory(Path.GetDirectoryName(File)!);
                if (System.IO.File.Exists(File) && new FileInfo(File).Length > 2 * 1024 * 1024)
                {
                    System.IO.File.Move(File, File + ".old", overwrite: true);
                }
                System.IO.File.AppendAllText(File, line + Environment.NewLine);
            }
            catch (Exception)
            {
                // nowhere to log
            }
        }
    }
}
