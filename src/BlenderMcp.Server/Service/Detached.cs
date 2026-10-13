using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;

namespace BlenderMcp.Server.Service;

/// <summary>Starts the bridge service so that it outlives the session that started it and holds none of Claude's pipes.</summary>
internal static class Detached
{
    public static int StartService(int port)
    {
        var exe = Environment.ProcessPath ?? throw new InvalidOperationException("cannot determine this executable's path");
        var args = $"--service --bridge-port {port}";
        if (!OperatingSystem.IsWindows())
        {
            var psi = new ProcessStartInfo(exe, args) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(exe)! };
            using var p = Process.Start(psi) ?? throw new InvalidOperationException("process did not start");
            return p.Id;
        }
        return StartWindows(exe, args, Path.GetDirectoryName(exe)!);
    }

    // Process.Start always lets the child inherit every inheritable handle, and this process's stdin/stdout/stderr are
    // Claude's pipes: a service started that way keeps Claude's end of them open for as long as it runs, so Claude never
    // sees this session's output close when it exits. CreateProcess with bInheritHandles = false instead; outside
    // Claude's job object where the job allows it, so ending Claude's process tree spares the service.
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct STARTUPINFO
    {
        public int cb;
        public string? lpReserved, lpDesktop, lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CreateProcess(string lpApplicationName, StringBuilder lpCommandLine, IntPtr lpProcessAttributes,
        IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory,
        ref STARTUPINFO lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);

    private const uint CREATE_NEW_PROCESS_GROUP = 0x00000200;
    private const uint CREATE_UNICODE_ENVIRONMENT = 0x00000400;
    private const uint CREATE_BREAKAWAY_FROM_JOB = 0x01000000;
    private const uint CREATE_NO_WINDOW = 0x08000000;

    private static int StartWindows(string exe, string args, string workingDir)
    {
        const uint flags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT;
        foreach (var extra in new[] { CREATE_BREAKAWAY_FROM_JOB, 0u })
        {
            var si = new STARTUPINFO { cb = Marshal.SizeOf<STARTUPINFO>() };
            var cmd = new StringBuilder($"\"{exe}\" {args}");
            if (CreateProcess(exe, cmd, IntPtr.Zero, IntPtr.Zero, false, flags | extra, IntPtr.Zero, workingDir, ref si, out var pi))
            {
                CloseHandle(pi.hThread);
                CloseHandle(pi.hProcess);
                return pi.dwProcessId;
            }
            var error = Marshal.GetLastWin32Error();
            // ERROR_ACCESS_DENIED: the job does not allow breakaway; start it inside the job then
            if (extra == 0 || error != 5)
            {
                throw new Win32Exception(error);
            }
        }
        return 0;
    }
}
