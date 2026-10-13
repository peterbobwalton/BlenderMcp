"""End-to-end test of the bridge service: several Blenders, several Claude sessions, routing by open file.

Usage:  python tests/bridge_test.py [path\\to\\BlenderMcp.Server.exe] [--launch]

Needs blender.exe (BLENDER env var, else the newest under Program Files\\Blender Foundation). The test
  1. starts two throw-away Blenders (factory settings, the repo's add-on) on A.blend and B.blend in a temp folder;
  2. runs the server in folder A and in folder B (bridge mode, its own service port 9871 so a real service on 9870
     is not touched) and checks each is routed to its own Blender;
  3. checks a second session in folder A is refused with "Already in use", and gets A once the first session ends;
  4. checks blender_session status / select / release;
  5. with --launch: a session in folder C (C.blend, no Blender on it) makes the bridge start Blender on C.blend.
     This uses your normal Blender profile, so the installed add-on must be 1.7+ with "Start automatically" on.
Exit code 0 = all checks passed. Other Blenders you have open are left alone (they may show up in status).
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
EXE = os.path.abspath(ARGS[0] if ARGS else os.path.join(ROOT, "publish", "BlenderMcp.Server.exe"))
LAUNCH = "--launch" in sys.argv
SERVICE_PORT = "9871"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

checks = []


def check(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {str(detail)[:400]}"))


def find_blender():
    if os.environ.get("BLENDER"):
        return os.environ["BLENDER"]
    found = sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"))
    if not found:
        sys.exit("Set BLENDER to blender.exe")
    return found[-1]


class Session:
    """One Claude session: the server over MCP stdio, started in a folder."""

    def __init__(self, folder, owner):
        env = dict(os.environ, BLENDER_MCP_BRIDGE_PORT=SERVICE_PORT, BLENDER_MCP_OWNER=owner)
        for k in ("BLENDER_MCP_PORT", "BLENDER_MCP_HOST", "BLENDER_MCP_WORKDIR"):
            env.pop(k, None)
        self.p = subprocess.Popen([EXE], cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.i = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "bridge-test", "version": "1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, obj):
        self.p.stdin.write((json.dumps(obj) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method, params=None):
        self.i += 1
        self._send({"jsonrpc": "2.0", "id": self.i, "method": method, "params": params or {}})
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server exited")
            msg = json.loads(line)
            if msg.get("id") == self.i:
                return msg

    def call(self, tool, **args):
        """(is_error, parsed JSON or text)"""
        res = self.request("tools/call", {"name": tool, "arguments": args}).get("result", {})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        try:
            data = json.loads(text)
        except ValueError:
            data = text
        return bool(res.get("isError")), data

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(10)
        except Exception:
            self.p.kill()


def same(a, b):
    return a and b and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def main():
    blender = find_blender()
    tmp = tempfile.mkdtemp(prefix="blendermcp-bridge-")
    folders = {k: os.path.join(tmp, k) for k in "ABC"}
    files = {k: os.path.join(folders[k], f"{k}.blend") for k in "ABC"}
    for f in folders.values():
        os.makedirs(f)
    blenders = []
    sessions = []
    try:
        for k in "AB":
            blenders.append(subprocess.Popen([blender, "--factory-startup", "--python",
                                              os.path.join(ROOT, "tests", "bridge_blender.py"), "--", files[k]],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            # one at a time, so they don't race for the same free port
            for _ in range(60):
                if os.path.exists(files[k]):
                    break
                time.sleep(1)
            time.sleep(2)
        check("test Blenders saved A.blend and B.blend", all(os.path.exists(files[k]) for k in "AB"))

        a = Session(folders["A"], "session A")
        sessions.append(a)
        b = Session(folders["B"], "session B")
        sessions.append(b)
        err, st = a.call("bridge_status")
        check("session A routed to the Blender with A.blend", not err and same(st.get("file"), files["A"]), st)
        err, st = b.call("bridge_status")
        check("session B routed to the Blender with B.blend", not err and same(st.get("file"), files["B"]), st)
        port_a = a.call("bridge_status")[1].get("blenderPort")
        port_b = b.call("bridge_status")[1].get("blenderPort")
        check("A and B use different Blenders", port_a and port_b and port_a != port_b, (port_a, port_b))

        # commands really go to the session's own Blender
        a.call("create_primitive", kind="cube", name="OnlyInA")
        _, la = a.call("list_objects", nameContains="OnlyInA")
        _, lb = b.call("list_objects", nameContains="OnlyInA")
        check("A's object is in A's Blender only", la.get("count") == 1 and lb.get("count") == 0, (la, lb))

        a2 = Session(folders["A"], "session A2")
        sessions.append(a2)
        err, msg = a2.call("bridge_status")
        check("second session in folder A refused: Already in use", err and "Already in use" in str(msg), msg)

        err, status = a.call("blender_session", action="status")
        mine = status.get("this_session", {}) if isinstance(status, dict) else {}
        check("status shows this session's Blender and why", not err and mine.get("blender_port") == port_a and mine.get("why"), status)
        listed = {x["port"] for x in status.get("blenders", [])} if isinstance(status, dict) else set()
        check("status lists both Blenders", {port_a, port_b} <= listed, status)
        check("status lists the other sessions", isinstance(status, dict) and len(status.get("sessions", [])) >= 2, status)

        err, msg = a2.call("blender_session", action="select", target=str(port_b))
        check("select of B's Blender from another session refused", err and "Already in use" in str(msg), msg)

        a.close()
        sessions.remove(a)
        time.sleep(1)
        err, st = a2.call("bridge_status")
        check("A's Blender is free again once session A ends", not err and same(st.get("file"), files["A"]), st)

        err, rel = b.call("blender_session", action="release")
        check("release", not err, rel)
        err, msg = a2.call("blender_session", action="select", target="B.blend")
        check("select by file name once B is released", not err and msg.get("port") == port_b, msg)
        err, st = b.call("bridge_status")
        check("B's session then finds its Blender in use", err and "Already in use" in str(st), st)
    finally:
        for s in sessions:
            s.close()
        for p in blenders:
            p.kill()

    if LAUNCH:
        launch_test(blender, folders["C"], files["C"])

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    sys.exit(1 if failed else 0)


def launch_test(blender, folder, file):
    """A session in a folder whose .blend no Blender has open: the bridge starts Blender on it."""
    subprocess.run([blender, "--background", "--factory-startup", "--python-expr",
                    f"import bpy; bpy.ops.wm.save_as_mainfile(filepath={file!r})"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    check("C.blend created", os.path.exists(file))
    c = Session(folder, "session C")
    try:
        t = time.time()
        err, st = c.call("bridge_status")
        while err and "still starting" in str(st) and time.time() - t < 180:
            err, st = c.call("bridge_status")
        check("bridge started Blender on C.blend", not err and same(st.get("file"), file), st)
        err, status = c.call("blender_session", action="status")
        pid = next((x["pid"] for x in status.get("blenders", []) if same(x.get("file"), file)), None) if not err else None
    finally:
        c.close()
    if pid:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
