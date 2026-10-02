"""TCP bridge between the C# MCP server and Blender.

Wire protocol: newline-delimited JSON (one object per line, UTF-8).
  request : {"id": 1, "cmd": "list_objects", "params": {...}}
  response: {"id": 1, "ok": true,  "result": {...}, "ms": 3.2}
            {"id": 1, "ok": false, "error": "message", "trace": "..."}

Sockets are handled on background threads. Every command is executed on
Blender's main thread through a bpy.app.timers callback, because bpy is
not thread-safe. Responses are handed back to a per-client writer thread
so large payloads (images) never block Blender's UI while being sent.
"""

import json
import queue
import socket
import threading
import time
import traceback

import bpy

from . import handlers

_MAX_LINE_BYTES = 64 << 20  # refuse absurd requests instead of buffering forever

# Work budget per timer tick so the UI stays responsive under load.
_TICK_BUDGET_S = 0.050
_IDLE_INTERVAL_S = 0.025   # nothing happened for a while
_ACTIVE_INTERVAL_S = 0.002  # recent traffic: poll fast for low latency
_ACTIVE_WINDOW_S = 2.0
_BUSY_INTERVAL_S = 0.0

_state = {
    "server": None,
    "last_activity": 0.0,
    "stats": {"requests": 0, "errors": 0, "clients": 0, "last_cmd": ""},
}


class _Client:
    def __init__(self, sock, addr, server):
        self.sock = sock
        self.addr = addr
        self.server = server
        self.out = queue.Queue()
        self.alive = True

    def start(self):
        threading.Thread(target=self._reader, daemon=True, name="mcp-read").start()
        threading.Thread(target=self._writer, daemon=True, name="mcp-write").start()

    def _reader(self):
        buf = b""
        try:
            while self.alive and self.server.running:
                chunk = self.sock.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > _MAX_LINE_BYTES and b"\n" not in buf:
                    self.send({"id": None, "ok": False, "error": "Request too large"})
                    break
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line.decode("utf-8"))
                    except Exception as ex:  # malformed line
                        self.send({"id": None, "ok": False, "error": f"Bad JSON: {ex}"})
                        continue
                    if not isinstance(req, dict) or not isinstance(req.get("cmd"), str):
                        rid = req.get("id") if isinstance(req, dict) else None
                        self.send({"id": rid, "ok": False, "error": "Request must be an object with a string 'cmd'"})
                        continue
                    if not isinstance(req.get("params") or {}, dict):
                        self.send({"id": req.get("id"), "ok": False, "error": "'params' must be an object"})
                        continue
                    self.server.inbox.put((self, req))
        except OSError:
            pass
        finally:
            self.close()

    def _writer(self):
        while self.alive:
            try:
                msg = self.out.get(timeout=0.5)
            except queue.Empty:
                continue
            if msg is None:
                break
            try:
                self.sock.sendall(msg)
            except OSError:
                break
        self.close()

    def send(self, obj):
        data = (json.dumps(obj, separators=(",", ":"), default=_json_default) + "\n").encode("utf-8")
        self.out.put(data)

    def close(self):
        if not self.alive:
            return
        self.alive = False
        self.out.put(None)
        try:
            self.sock.close()
        except OSError:
            pass
        self.server.forget(self)


def _json_default(o):
    # mathutils types, bpy_prop_array, sets, etc.
    try:
        return list(o)
    except TypeError:
        return str(o)


class BridgeServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.running = False
        self.inbox = queue.Queue()
        self.clients = set()
        self._lock = threading.Lock()
        self._sock = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: SO_REUSEADDR would let a second Blender bind the same port
            # and steal connections; exclusive use makes the second bind fail loudly.
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        self._sock.settimeout(0.5)
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True, name="mcp-accept").start()
        if not bpy.app.timers.is_registered(_pump):
            bpy.app.timers.register(_pump, first_interval=0.1, persistent=True)

    def _accept_loop(self):
        while self.running:
            try:
                s, addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            c = _Client(s, addr, self)
            with self._lock:
                self.clients.add(c)
                _state["stats"]["clients"] = len(self.clients)
            c.start()

    def forget(self, client):
        with self._lock:
            self.clients.discard(client)
            _state["stats"]["clients"] = len(self.clients)

    def stop(self):
        self.running = False
        try:
            self._sock.close()
        except Exception:
            pass
        with self._lock:
            clients = list(self.clients)
        for c in clients:
            c.close()


def _pump():
    """Main-thread timer: drain queued requests within a time budget."""
    srv = _state["server"]
    if srv is None or not srv.running:
        return None  # unregister
    deadline = time.perf_counter() + _TICK_BUDGET_S
    did_work = False
    while time.perf_counter() < deadline:
        try:
            client, req = srv.inbox.get_nowait()
        except queue.Empty:
            break
        did_work = True
        if not client.alive:
            continue  # caller is gone; don't run its (possibly mutating) command
        try:
            _execute(client, req)
        except Exception:  # never let an exception unregister the timer
            traceback.print_exc()
    now = time.perf_counter()
    if did_work:
        _state["last_activity"] = now
        if not srv.inbox.empty():
            return _BUSY_INTERVAL_S
    return _ACTIVE_INTERVAL_S if now - _state["last_activity"] < _ACTIVE_WINDOW_S else _IDLE_INTERVAL_S


def _execute(client, req):
    rid = req.get("id")
    cmd = req.get("cmd", "")
    params = req.get("params") or {}
    stats = _state["stats"]
    stats["requests"] += 1
    stats["last_cmd"] = cmd
    t0 = time.perf_counter()
    try:
        result = handlers.dispatch(cmd, params)
        client.send({"id": rid, "ok": True, "result": result, "ms": round((time.perf_counter() - t0) * 1000, 2)})
    except handlers.BridgeError as ex:
        stats["errors"] += 1
        client.send({"id": rid, "ok": False, "error": str(ex)})
    except Exception as ex:
        stats["errors"] += 1
        client.send({"id": rid, "ok": False, "error": f"{type(ex).__name__}: {ex}", "trace": traceback.format_exc(limit=6)})


def start(host="127.0.0.1", port=9877):
    stop()
    srv = BridgeServer(host, port)
    srv.start()
    _state["server"] = srv
    print(f"[BlenderMCP] bridge listening on {host}:{port}")


def stop():
    srv = _state["server"]
    if srv is not None:
        srv.stop()
        _state["server"] = None
        print("[BlenderMCP] bridge stopped")
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)


def is_running():
    srv = _state["server"]
    return srv is not None and srv.running


def stats():
    return dict(_state["stats"])
