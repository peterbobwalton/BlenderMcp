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

import atexit
import json
import math
import os
import queue
import socket
import threading
import time
import traceback

import bpy

from . import handlers

# Ports tried when the configured one is taken (another Blender has it): 9877, then 9850-9869.
# 9878-9899 belong to the UnrealMcp bridges, 9849 to the e2e test Blender. Keep in sync with BridgeDefaults.BlenderPorts (C#).
PORTS = [9877] + list(range(9850, 9870))

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


def _finite(obj):
    """Copy of obj with NaN/Infinity floats replaced by None."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    if obj is None or isinstance(obj, (str, int)):
        return obj
    return _finite(_json_default(obj))  # vectors, matrices etc. become lists/str first


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
        buf = bytearray()
        scanned = 0  # bytes of buf already known to hold no newline
        try:
            while self.alive and self.server.running:
                chunk = self.sock.recv(1 << 16)
                if not chunk:
                    break
                buf += chunk
                if buf.find(b"\n", scanned) < 0:
                    scanned = len(buf)  # search only new bytes next time, not the whole buffer
                    if len(buf) > _MAX_LINE_BYTES:
                        self.send({"id": None, "ok": False, "error": "Request too large"})
                        break
                    continue
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
                scanned = len(buf)  # the remainder has no newline
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
        try:
            text = json.dumps(obj, separators=(",", ":"), default=_json_default, allow_nan=False)
        except ValueError:  # NaN/Infinity aren't valid JSON and the C# side would drop the reply
            text = json.dumps(_finite(obj), separators=(",", ":"), default=_json_default, allow_nan=False)
        data = (text + "\n").encode("utf-8")
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
        self.announce = False

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


def _instances_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "BlenderMcp", "instances")


def _announce_path(port):
    return os.path.join(_instances_dir(), f"{port}.json")


def announce():
    """Write instances/<port>.json (port, pid, open file) so the bridge service finds this Blender and its file
    even while it is too busy to answer a ping. Rewritten when a file is loaded or saved."""
    srv = _state["server"]
    if srv is None or not srv.running or not srv.announce:
        return
    data = {
        "port": srv.port,
        "pid": os.getpid(),
        "file": bpy.data.filepath,
        "blender": bpy.app.version_string,
    }
    try:
        os.makedirs(_instances_dir(), exist_ok=True)
        tmp = _announce_path(srv.port) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _announce_path(srv.port))
    except OSError as ex:
        print(f"[BlenderMCP] could not write {_announce_path(srv.port)}: {ex}")


def _unannounce(srv):
    if srv is None or not srv.announce:
        return
    try:
        path = _announce_path(srv.port)
        with open(path, encoding="utf-8") as f:
            mine = json.load(f).get("pid") == os.getpid()
        if mine:
            os.remove(path)
    except (OSError, ValueError):
        pass


@bpy.app.handlers.persistent
def _on_file_changed(*_args):
    announce()


def _at_exit():
    _unannounce(_state["server"])


atexit.register(_at_exit)


def start(host="127.0.0.1", port=9877, fallback=False, announce_self=True):
    """Listen on port. fallback=True: if another Blender has it, take the next free one of PORTS instead.
    announce_self=False keeps this Blender out of the bridge service's view (the e2e test Blender)."""
    stop()
    candidates = [port] + ([p for p in PORTS if p != port] if fallback else [])
    error = None
    for p in candidates:
        srv = BridgeServer(host, p)
        try:
            srv.start()
        except OSError as ex:
            error = error or ex
            try:
                srv._sock.close()
            except Exception:
                pass
            continue
        srv.announce = announce_self
        _state["server"] = srv
        announce()
        for handlers_list in (bpy.app.handlers.load_post, bpy.app.handlers.save_post):
            if _on_file_changed not in handlers_list:
                handlers_list.append(_on_file_changed)
        print(f"[BlenderMCP] bridge listening on {host}:{p}" + ("" if p == port else f" (port {port} is taken)"))
        return p
    raise error


def stop():
    srv = _state["server"]
    if srv is not None:
        srv.stop()
        _unannounce(srv)
        _state["server"] = None
        print("[BlenderMCP] bridge stopped")
    for handlers_list in (bpy.app.handlers.load_post, bpy.app.handlers.save_post):
        if _on_file_changed in handlers_list:
            handlers_list.remove(_on_file_changed)
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)


def current_port():
    srv = _state["server"]
    return srv.port if srv is not None and srv.running else None


def is_running():
    srv = _state["server"]
    return srv is not None and srv.running


def stats():
    return dict(_state["stats"])
