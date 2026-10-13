using System.Diagnostics;

namespace BlenderMcp.Server.Service;

/// <summary>Finds .blend files near a session's folder and starts Blender on one (or on an empty scene).</summary>
internal static class Launcher
{
    private static readonly HashSet<string> SkipDirs = new(StringComparer.OrdinalIgnoreCase)
    {
        "node_modules", "Intermediate", "Binaries", "Saved", "DerivedDataCache", "bin", "obj", "__pycache__",
    };

    public static StringComparison PathComparison =>
        OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;

    public static string Normalize(string path)
    {
        try
        {
            return Path.TrimEndingDirectorySeparator(Path.GetFullPath(path));
        }
        catch (Exception)
        {
            return path;
        }
    }

    /// <summary>Levels from ancestor down to path (0 = same folder), or -1 when path is not inside ancestor.</summary>
    public static int Depth(string ancestor, string path)
    {
        ancestor = Normalize(ancestor);
        path = Normalize(path);
        for (var depth = 0; path.Length > 0; depth++)
        {
            if (string.Equals(path, ancestor, PathComparison))
            {
                return depth;
            }
            var parent = Path.GetDirectoryName(path);
            if (parent is null || parent == path)
            {
                break;
            }
            path = parent;
        }
        return -1;
    }

    /// <summary>Folders not worth searching for .blend files: drive roots, Windows, Program Files, AppData (except
    /// folders inside %TEMP%), the bare user profile (Claude Desktop sessions start in such places).</summary>
    public static bool IsSearchable(string folder)
    {
        folder = Normalize(folder);
        if (!Directory.Exists(folder) || Path.GetDirectoryName(folder) is null)
        {
            return false;
        }
        // a folder inside %TEMP% (which is under LocalAppData) is a real working folder; %TEMP% itself is not
        if (Depth(Path.GetTempPath(), folder) > 0)
        {
            return true;
        }
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (home.Length > 0 && string.Equals(folder, Normalize(home), PathComparison))
        {
            return false;
        }
        foreach (var special in new[]
                 {
                     Environment.SpecialFolder.Windows, Environment.SpecialFolder.ProgramFiles, Environment.SpecialFolder.ProgramFilesX86,
                     Environment.SpecialFolder.ApplicationData, Environment.SpecialFolder.LocalApplicationData,
                 })
        {
            var root = Environment.GetFolderPath(special);
            if (root.Length > 0 && Depth(root, folder) >= 0)
            {
                return false;
            }
        }
        return true;
    }

    /// <summary>.blend files in the folder, else in up to three parents (that folder only), else up to two levels down.</summary>
    public static List<string> FindBlends(IEnumerable<string> folders)
    {
        foreach (var folder in folders.Select(Normalize).Where(IsSearchable))
        {
            var here = BlendsIn(folder);
            if (here.Count > 0)
            {
                return here;
            }
            var up = folder;
            for (var i = 0; i < 3; i++)
            {
                up = Path.GetDirectoryName(up);
                if (up is null || !IsSearchable(up))
                {
                    break;
                }
                var found = BlendsIn(up);
                if (found.Count > 0)
                {
                    return found;
                }
            }
            var below = new List<string>();
            foreach (var sub in SubDirs(folder))
            {
                below.AddRange(BlendsIn(sub));
                foreach (var sub2 in SubDirs(sub))
                {
                    below.AddRange(BlendsIn(sub2));
                }
            }
            if (below.Count > 0)
            {
                return below;
            }
        }
        return [];
    }

    private static List<string> BlendsIn(string dir)
    {
        try
        {
            // "*.blend" also matches .blend1 backups on Windows (8.3 rules); keep exact extensions only
            return Directory.EnumerateFiles(dir, "*.blend")
                .Where(f => string.Equals(Path.GetExtension(f), ".blend", StringComparison.OrdinalIgnoreCase))
                .Select(Normalize).OrderBy(f => f, StringComparer.OrdinalIgnoreCase).ToList();
        }
        catch (Exception)
        {
            return [];
        }
    }

    private static IEnumerable<string> SubDirs(string dir)
    {
        try
        {
            return Directory.EnumerateDirectories(dir)
                .Where(d => !Path.GetFileName(d).StartsWith('.') && !SkipDirs.Contains(Path.GetFileName(d)))
                .ToList();
        }
        catch (Exception)
        {
            return [];
        }
    }

    /// <summary>blender.exe: the session's BLENDER_MCP_BLENDER (sent in bridge.hello), else the service's own, else the
    /// one a running Blender uses, else the .blend file association, else the newest under Program Files\Blender Foundation.</summary>
    public static string? FindBlender(string? preferred = null)
    {
        foreach (var exe in new[] { preferred, Environment.GetEnvironmentVariable("BLENDER_MCP_BLENDER") })
        {
            if (!string.IsNullOrWhiteSpace(exe) && File.Exists(exe))
            {
                return Fix(exe);
            }
        }
        var running = Process.GetProcessesByName("blender");
        try
        {
            foreach (var p in running)
            {
                try
                {
                    if (p.MainModule?.FileName is { } path && File.Exists(path))
                    {
                        return Fix(path);
                    }
                }
                catch (Exception)
                {
                    // access denied etc.
                }
            }
        }
        finally
        {
            foreach (var p in running)
            {
                p.Dispose();
            }
        }
        if (OperatingSystem.IsWindows())
        {
            try
            {
                using var key = Microsoft.Win32.Registry.ClassesRoot.OpenSubKey(@"blendfile\shell\open\command");
                if (key?.GetValue(null) is string cmd)
                {
                    var exe = cmd.StartsWith('"') ? cmd[1..cmd.IndexOf('"', 1)] : cmd.Split(' ')[0];
                    if (File.Exists(exe))
                    {
                        return Fix(exe);
                    }
                }
            }
            catch (Exception)
            {
            }
            var foundation = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "Blender Foundation");
            if (Directory.Exists(foundation))
            {
                var newest = Directory.EnumerateDirectories(foundation)
                    .Select(d => Path.Combine(d, "blender.exe"))
                    .Where(File.Exists)
                    .OrderByDescending(f => VersionOf(Path.GetDirectoryName(f)!))
                    .FirstOrDefault();
                if (newest is not null)
                {
                    return newest;
                }
            }
        }
        return null;
    }

    private static Version VersionOf(string dir)
    {
        var digits = new string(Path.GetFileName(dir).SkipWhile(c => !char.IsDigit(c)).ToArray());
        return Version.TryParse(digits.Contains('.') ? digits : digits + ".0", out var v) ? v : new Version(0, 0);
    }

    /// <summary>blender-launcher.exe hands over to blender.exe and exits; start blender.exe so the process id is Blender's.</summary>
    private static string Fix(string exe)
    {
        if (Path.GetFileName(exe).Equals("blender-launcher.exe", StringComparison.OrdinalIgnoreCase))
        {
            var real = Path.Combine(Path.GetDirectoryName(exe)!, "blender.exe");
            if (File.Exists(real))
            {
                return real;
            }
        }
        return exe;
    }

    /// <summary>Starts Blender on the file (or an empty scene); returns its process id.</summary>
    public static int Start(string blenderExe, string? file)
    {
        var psi = new ProcessStartInfo(blenderExe)
        {
            UseShellExecute = false,
            WorkingDirectory = file is not null ? Path.GetDirectoryName(file)! : Path.GetDirectoryName(blenderExe)!,
        };
        if (file is not null)
        {
            psi.ArgumentList.Add(file);
        }
        using var p = Process.Start(psi) ?? throw new InvalidOperationException("Blender did not start");
        return p.Id;
    }
}
