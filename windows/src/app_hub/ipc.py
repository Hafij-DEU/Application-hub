"""One App Hub window: a second start hands its request (a page, a file) to the first.

A named mutex says whether App Hub runs; the running copy listens on 127.0.0.1 with a
random port and a secret token kept in %LOCALAPPDATA%\\App Hub\\run\\ipc.json.
"""

import ctypes
import json
import os
import secrets
import socket
import threading
import time

from . import core, winsys

INFO = os.path.join(core.DATA_DIR, "run", "ipc.json")
_mutex = None


def claim():
    """True if this is the only App Hub (keeps the mutex for the life of the process)."""
    global _mutex
    if not winsys.IS_WIN:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, r"Local\AppHub-io.github.hafij_bge.AppHub")
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


def send(message, wait=20.0):
    """Give `message` (a dict) to the running App Hub. False if it does not answer."""
    deadline = time.time() + wait
    while time.time() < deadline:
        try:
            with open(INFO, encoding="utf-8") as f:
                info = json.load(f)
            with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=3) as s:
                try:
                    ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY: let it come to the front
                except (AttributeError, OSError):
                    pass
                s.sendall(json.dumps(dict(message, token=info["token"])).encode() + b"\n")
                return s.recv(16).startswith(b"ok")
        except (OSError, ValueError, KeyError):
            time.sleep(0.4)          # the other copy is still starting
    return False


def serve(on_message):
    """Listen for other starts; on_message(dict) runs in a worker thread."""
    token = secrets.token_hex(16)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    os.makedirs(os.path.dirname(INFO), exist_ok=True)
    with open(INFO, "w", encoding="utf-8") as f:
        json.dump({"port": srv.getsockname()[1], "token": token, "pid": os.getpid()}, f)

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(3)
                    data = b""
                    while not data.endswith(b"\n") and len(data) < 65536:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    msg = json.loads(data.decode("utf-8"))
                    if not secrets.compare_digest(str(msg.get("token", "")), token):
                        continue
                    conn.sendall(b"ok\n")
                    on_message(msg)
                except (OSError, ValueError):
                    continue
    threading.Thread(target=loop, daemon=True).start()
