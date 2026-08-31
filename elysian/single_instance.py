"""Single instance handling.

Double-clicking a track in Explorer starts the exe. Without this, doing that
while the player is already open launches a second copy and you get two songs
playing at once. The first instance holds a loopback socket; later ones hand
their file paths over and exit.
"""
import json
import os
import socket
import sys
import threading
import uuid

HOST = "127.0.0.1"
PORT = 49517
TOKEN_FILE = os.path.join(
    os.path.expanduser("~"), ".elysian_player_instance")


def _token() -> str:
    """Shared secret so stray local traffic on this port is ignored."""
    try:
        if os.path.isfile(TOKEN_FILE):
            value = open(TOKEN_FILE, encoding="utf-8").read().strip()
            if value:
                return value
    except OSError:
        pass
    value = uuid.uuid4().hex
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        pass
    return value


def try_acquire() -> socket.socket | None:
    """Bind the port. None means another instance already owns it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Windows and POSIX need opposite options here. SO_REUSEADDR on Windows
    # lets two sockets bind the same port at once, which would defeat the
    # whole point; SO_EXCLUSIVEADDRUSE is the correct one there. On POSIX
    # SO_REUSEADDR only permits rebinding a port left in TIME_WAIT, never one
    # that is actively listening, so the guard still holds.
    try:
        if sys.platform == "win32":
            sock.setsockopt(socket.SOL_SOCKET,
                            getattr(socket, "SO_EXCLUSIVEADDRUSE", 1), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    try:
        sock.bind((HOST, PORT))
        sock.listen(4)
        return sock
    except OSError:
        try:
            sock.close()
        except OSError:
            pass
        return None


def hand_off(paths) -> bool:
    """Give these paths to the running instance. True if it accepted."""
    payload = json.dumps({"token": _token(), "paths": list(paths)}).encode()
    try:
        with socket.create_connection((HOST, PORT), timeout=1.5) as c:
            c.sendall(payload)
            c.shutdown(socket.SHUT_WR)
            return c.recv(16) == b"ok"
    except OSError:
        return False


def serve(sock: socket.socket, on_paths) -> threading.Thread:
    """Listen for hand-offs from later instances."""
    expected = _token()

    def loop():
        while True:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.settimeout(2.0)
                    chunks = []
                    while True:
                        block = conn.recv(4096)
                        if not block:
                            break
                        chunks.append(block)
                        if len(chunks) > 64:
                            break
                    data = json.loads(b"".join(chunks).decode())
                    if data.get("token") != expected:
                        continue
                    paths = [p for p in data.get("paths", [])
                             if isinstance(p, str) and os.path.isfile(p)]
                    conn.sendall(b"ok")
                    on_paths(paths)
                except Exception:
                    continue

    thread = threading.Thread(target=loop, name="elysian-instance", daemon=True)
    thread.start()
    return thread
