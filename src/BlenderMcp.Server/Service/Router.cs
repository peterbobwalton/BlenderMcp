using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using BlenderMcp.Server.Bridge;

namespace BlenderMcp.Server.Service;

/// <summary>Refusal or failure the service reports back to the session as a command error.</summary>
internal sealed class BridgeError(string message) : Exception(message);

/// <summary>A Blender this service started for a session, until its add-on answers.</summary>
internal sealed record PendingLaunch(int Pid, string? File, DateTime Started);

/// <summary>One Claude session (one BlenderMcp.Server process) connected to the service.</summary>
internal sealed class Session(string id, List<string> folders, string owner)
{
    public string Id { get; } = id;
    public List<string> Folders { get; } = folders;
    public string Owner { get; } = owner;
    public DateTime Since { get; } = DateTime.Now;
    public DateTime LastCall { get; set; } = DateTime.Now;

    /// <summary>The Blender this session is routed to (port + process id), sticky while that Blender runs.</summary>
    public int? Port { get; set; }
    public int Pid { get; set; }
    public string File { get; set; } = "";
    public string Reason { get; set; } = "";

    /// <summary>Set by blender_session select/open: this Blender, not folder matching.</summary>
    public bool Pinned { get; set; }
    public PendingLaunch? Launch { get; set; }

    /// <summary>Connection to the routed Blender; owned by BridgeService.</summary>
    public Upstream? Link { get; set; }

    /// <summary>Serialises routing and forwarding for this session so commands reach Blender in order.</summary>
    public SemaphoreSlim RouteLock { get; } = new(1, 1);

    public bool Holds(BlenderInstance i) => Port == i.Port && Pid == i.Pid;

    public string Label => $"'{Owner}', folder {Folders.LastOrDefault() ?? "?"}, since {Since:HH:mm}";

    public JsonObject ToJson() => new()
    {
        ["owner"] = Owner,
        ["folders"] = new JsonArray(Folders.Select(f => (JsonNode?)f).ToArray()),
        ["since"] = Since.ToString("HH:mm:ss"),
        ["last_call"] = LastCall.ToString("HH:mm:ss"),
        ["blender_port"] = Port,
        ["file"] = Port is null ? null : File.Length > 0 ? File : "(unsaved scene)",
        ["why"] = Port is null ? null : Reason,
        ["pinned"] = Pinned,
        ["starting_blender"] = Launch is null ? null : Launch.File ?? "(empty scene)",
    };
}

/// <summary>
/// The routing table: which session uses which Blender. Each session gets the Blender whose open .blend lies in (or
/// near) its folder; one session per Blender, so a second session asking for a Blender in use is refused with
/// "Already in use". With no match it starts Blender on the one .blend near the folder, else takes the only free
/// Blender, else starts Blender on an empty scene.
/// </summary>
internal sealed class Router
{
    private static readonly TimeSpan LaunchWaitPerCall = TimeSpan.FromSeconds(45);
    private static readonly TimeSpan LaunchGiveUp = TimeSpan.FromMinutes(3);

    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly ConcurrentDictionary<string, Session> _sessions = new();

    public DateTime IdleSince { get; private set; } = DateTime.UtcNow;

    public int SessionCount => _sessions.Count;

    public Session Add(string id, List<string> folders, string owner)
    {
        var s = new Session(id, folders, owner);
        _sessions[id + "/" + Guid.NewGuid().ToString("N")[..6]] = s;
        ServiceLog.Write($"session {Short(id)} connected: {owner}; folder(s) {string.Join(", ", folders)}");
        return s;
    }

    public void Remove(Session s)
    {
        foreach (var (key, value) in _sessions)
        {
            if (ReferenceEquals(value, s))
            {
                _sessions.TryRemove(key, out _);
            }
        }
        if (_sessions.IsEmpty)
        {
            IdleSince = DateTime.UtcNow;
        }
        ServiceLog.Write($"session {Short(s.Id)} ended{(s.Port is { } p ? $"; Blender on port {p} is free again" : "")}");
    }

    private static string Short(string id) => id.Length > 8 ? id[..8] : id;

    private Session? Holder(BlenderInstance i, Session except) =>
        _sessions.Values.FirstOrDefault(o => !ReferenceEquals(o, except) && o.Holds(i));

    private Session? LaunchingFile(string file, Session except) =>
        _sessions.Values.FirstOrDefault(o => !ReferenceEquals(o, except) && o.Launch?.File is { } f
            && string.Equals(f, file, Launcher.PathComparison));

    private static BridgeError InUse(BlenderInstance i, Session holder) => new(
        $"Already in use: the Blender with {i.FileLabel} (port {i.Port}) is routed to another Claude session ({holder.Label}). " +
        "Close that session, or have it call blender_session action=release; blender_session action=status lists the other Blenders.");

    private static void Assign(Session s, BlenderInstance i, string reason)
    {
        s.Port = i.Port;
        s.Pid = i.Pid;
        s.File = i.File;
        s.Reason = reason;
        s.Launch = null;
        ServiceLog.Write($"session {Short(s.Id)} -> Blender port {i.Port} ({i.FileLabel}): {reason}");
    }

    /// <summary>Score of a .blend's folder for a session folder: inside it (best), or the session is below it.</summary>
    private static int Score(Session s, string file)
    {
        if (file.Length == 0)
        {
            return 0;
        }
        var dir = Path.GetDirectoryName(Launcher.Normalize(file)) ?? "";
        var best = 0;
        foreach (var folder in s.Folders)
        {
            var down = Launcher.Depth(folder, dir);  // .blend at or below the session folder
            if (down is >= 0 and <= 3)
            {
                best = Math.Max(best, 100 - 10 * down);
            }
            var up = Launcher.Depth(dir, folder);  // session folder below the .blend's folder
            if (up is > 0 and <= 3)
            {
                best = Math.Max(best, 60 - 10 * up);
            }
        }
        return best;
    }

    /// <summary>The Blender this session's commands go to, routing (and starting Blender) when needed.</summary>
    public async Task<BlenderInstance> ChooseAsync(Session s)
    {
        if (s.Launch is null)
        {
            var all = await Discovery.ListAsync().ConfigureAwait(false);
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                var chosen = ChooseLocked(s, all);
                if (chosen is not null)
                {
                    return chosen;
                }
            }
            finally
            {
                _gate.Release();
            }
        }
        return await WaitForLaunchAsync(s).ConfigureAwait(false);
    }

    /// <summary>Null when it started Blender for the session (s.Launch set): wait for it then.</summary>
    private BlenderInstance? ChooseLocked(Session s, List<BlenderInstance> all)
    {
        if (s.Port is { } port)
        {
            var current = all.FirstOrDefault(s.Holds);
            if (current is not null)
            {
                s.File = current.File;
                return current;
            }
            ServiceLog.Write($"session {Short(s.Id)}: its Blender on port {port} is gone");
            s.Port = null;
            if (s.Pinned)
            {
                s.Pinned = false;
                throw new BridgeError(
                    $"The Blender this session was using (port {port}, {(s.File.Length > 0 ? s.File : "unsaved scene")}) has closed. " +
                    "Call blender_session action=open to start it again, or action=auto to route by folder.");
            }
        }

        var scored = all
            .Select(i => (i, score: Score(s, i.File)))
            .Where(x => x.score > 0)
            .OrderByDescending(x => x.score)
            .ToList();
        if (scored.Count > 0)
        {
            var free = scored.FirstOrDefault(x => Holder(x.i, s) is null);
            if (free.i is not null && free.score == scored[0].score)
            {
                Assign(s, free.i, $"its open file {free.i.File} is in or near {s.Folders.Last()}");
                return free.i;
            }
            throw InUse(scored[0].i, Holder(scored[0].i, s)!);
        }

        var blends = Launcher.FindBlends(s.Folders);
        if (blends.Count == 1)
        {
            if (LaunchingFile(blends[0], s) is { } other)
            {
                throw new BridgeError($"Already in use: Blender is being started on {blends[0]} for another Claude session ({other.Label}).");
            }
            StartLocked(s, all, blends[0]);
            return null;
        }
        if (blends.Count > 1)
        {
            throw new BridgeError(
                $"Several .blend files near {s.Folders.Last()}: {string.Join(", ", blends.Take(10))}. " +
                "Call blender_session action=open path=<the .blend to use>, or action=select for a Blender that is already open.");
        }

        var freeBlenders = all.Where(i => i.Answered && Holder(i, s) is null).ToList();
        if (freeBlenders.Count == 1)
        {
            Assign(s, freeBlenders[0], "the only open Blender no other session is using");
            return freeBlenders[0];
        }
        if (freeBlenders.Count > 1)
        {
            var unsaved = freeBlenders.Where(i => i.File.Length == 0).ToList();
            if (unsaved.Count == 1)
            {
                Assign(s, unsaved[0], "the only free Blender with an unsaved scene");
                return unsaved[0];
            }
            throw new BridgeError(
                "Several Blenders are open and none has a file in this session's folder: " +
                string.Join("; ", freeBlenders.Select(i => $"port {i.Port}: {i.FileLabel}")) +
                ". Call blender_session action=select target=<port or file name>, or action=open path=<.blend>.");
        }

        StartLocked(s, all, null);
        return null;
    }

    private void StartLocked(Session s, List<BlenderInstance> all, string? file)
    {
        var launching = _sessions.Values.Count(o => o.Launch is not null);
        if (all.Count + launching >= BridgeDefaults.BlenderPorts.Length)
        {
            throw new BridgeError($"{all.Count} Blenders are running (at most {BridgeDefaults.BlenderPorts.Length}); close one first.");
        }
        var exe = Launcher.FindBlender() ?? throw new BridgeError(
            "Cannot find blender.exe to start. Open Blender yourself, or set BLENDER_MCP_BLENDER to its path in the MCP entry's env.");
        int pid;
        try
        {
            pid = Launcher.Start(exe, file);
        }
        catch (Exception ex)
        {
            throw new BridgeError($"Could not start {exe}: {ex.Message}");
        }
        s.Launch = new PendingLaunch(pid, file, DateTime.UtcNow);
        s.Port = null;
        ServiceLog.Write($"session {Short(s.Id)}: started Blender (pid {pid}) on {file ?? "an empty scene"}");
    }

    private async Task<BlenderInstance> WaitForLaunchAsync(Session s)
    {
        var launch = s.Launch!;
        var until = DateTime.UtcNow + LaunchWaitPerCall;
        while (true)
        {
            var all = await Discovery.ListAsync(fresh: true).ConfigureAwait(false);
            await _gate.WaitAsync().ConfigureAwait(false);
            try
            {
                var up = all.FirstOrDefault(i => i.Pid == launch.Pid && Holder(i, s) is null)
                    ?? (launch.File is null ? null : all.FirstOrDefault(i => i.Answered && Holder(i, s) is null
                        && string.Equals(Launcher.Normalize(i.File), launch.File, Launcher.PathComparison)));
                if (up is not null)
                {
                    Assign(s, up, launch.File is null ? "started for this session (empty scene)" : $"started for this session on {launch.File}");
                    return up;
                }
                if (launch.Pid > 0 && !Discovery.IsBlender(launch.Pid) && DateTime.UtcNow - launch.Started > TimeSpan.FromSeconds(5))
                {
                    s.Launch = null;
                    throw new BridgeError("Blender exited while starting. Open it yourself to see why, then try again.");
                }
                if (DateTime.UtcNow - launch.Started > LaunchGiveUp)
                {
                    s.Launch = null;
                    throw new BridgeError(
                        "Blender started but its MCP add-on never answered. In Blender check that 'Blender MCP Bridge (C#)' is enabled " +
                        "with 'Start automatically' ticked (Preferences > Add-ons), then try again.");
                }
            }
            finally
            {
                _gate.Release();
            }
            if (DateTime.UtcNow > until)
            {
                throw new BridgeError($"Blender is still starting ({launch.File ?? "empty scene"}). Call again in a few seconds.");
            }
            await Task.Delay(1000).ConfigureAwait(false);
        }
    }

    // ---- blender_session ----------------------------------------------------------------------------------------------

    public async Task<JsonObject> StatusAsync(Session s)
    {
        var all = await Discovery.ListAsync(fresh: true).ConfigureAwait(false);
        var blenders = new JsonArray();
        foreach (var i in all)
        {
            var o = i.ToJson();
            var holder = _sessions.Values.FirstOrDefault(x => x.Holds(i));
            o["used_by"] = holder is null ? null : ReferenceEquals(holder, s) ? "this session" : holder.Label;
            blenders.Add(o);
        }
        return new JsonObject
        {
            ["service"] = $"Blender bridge {BridgeDefaults.Version} on 127.0.0.1",
            ["this_session"] = s.ToJson(),
            ["blenders"] = blenders,
            ["sessions"] = new JsonArray(_sessions.Values.Where(x => !ReferenceEquals(x, s)).Select(x => (JsonNode?)x.ToJson()).ToArray()),
        };
    }

    /// <summary>Pins the session to a running Blender by port or (part of) its file name.</summary>
    public async Task<BlenderInstance> SelectAsync(Session s, string target)
    {
        var all = await Discovery.ListAsync(fresh: true).ConfigureAwait(false);
        List<BlenderInstance> matches = int.TryParse(target, out var port)
            ? all.Where(i => i.Port == port).ToList()
            : all.Where(i => i.File.Contains(target, StringComparison.OrdinalIgnoreCase)).ToList();
        if (matches.Count == 0)
        {
            throw new BridgeError($"No open Blender matches '{target}'. Open ones: " +
                (all.Count == 0 ? "none" : string.Join("; ", all.Select(i => $"port {i.Port}: {i.FileLabel}"))));
        }
        if (matches.Count > 1)
        {
            throw new BridgeError($"'{target}' matches several Blenders: " +
                string.Join("; ", matches.Select(i => $"port {i.Port}: {i.FileLabel}")) + ". Use the port.");
        }
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            var i = matches[0];
            if (Holder(i, s) is { } holder)
            {
                throw InUse(i, holder);
            }
            s.Launch = null;
            Assign(s, i, "selected with blender_session");
            s.Pinned = true;
            return i;
        }
        finally
        {
            _gate.Release();
        }
    }

    /// <summary>Uses the Blender that has this .blend open, else starts one on it (path null: an empty scene).</summary>
    public async Task<BlenderInstance> OpenAsync(Session s, string? path)
    {
        string? file = null;
        if (!string.IsNullOrWhiteSpace(path))
        {
            var full = Launcher.Normalize(path);
            if (Directory.Exists(full))
            {
                var found = Launcher.FindBlends([full]);
                file = found.Count switch
                {
                    1 => found[0],
                    0 => throw new BridgeError($"No .blend file in or near {full}."),
                    _ => throw new BridgeError($"Several .blend files in {full}: {string.Join(", ", found.Take(10))}. Pass the one to open."),
                };
            }
            else if (File.Exists(full))
            {
                file = full;
            }
            else
            {
                throw new BridgeError($"{full} does not exist.");
            }
        }

        var all = await Discovery.ListAsync(fresh: true).ConfigureAwait(false);
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (file is not null)
            {
                var open = all.Where(i => string.Equals(Launcher.Normalize(i.File), file, Launcher.PathComparison)).ToList();
                var free = open.FirstOrDefault(i => Holder(i, s) is null);
                if (free is not null)
                {
                    Assign(s, free, $"opened {file} with blender_session (it was already open)");
                    s.Pinned = true;
                    return free;
                }
                if (open.Count > 0)
                {
                    throw InUse(open[0], Holder(open[0], s)!);
                }
                if (LaunchingFile(file, s) is { } other)
                {
                    throw new BridgeError($"Already in use: Blender is being started on {file} for another Claude session ({other.Label}).");
                }
            }
            s.Pinned = false;
            StartLocked(s, all, file);
        }
        finally
        {
            _gate.Release();
        }
        var started = await WaitForLaunchAsync(s).ConfigureAwait(false);
        s.Pinned = true;
        return started;
    }

    /// <summary>Back to routing by folder on the next call.</summary>
    public void Auto(Session s)
    {
        s.Pinned = false;
        s.Port = null;
        s.Launch = null;
    }
}
