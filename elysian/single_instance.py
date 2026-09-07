"""Single instance handling.

Double-clicking a track in Explorer starts the exe. Without this, doing that
while the player is already open launches a second copy and you get two songs
playing at once. The first instance owns a channel; later ones hand their file
paths over and exit.

On Windows that channel is a named pipe. A loopback TCP socket does the same
job, but listening on a port makes Windows Firewall prompt on first launch --
an alarming thing for a music player to do, and a hard-coded port number can
be taken by anything else on the machine. A pipe has a name rather than a
number, needs no firewall permission, and cannot collide.

Elsewhere, for development on Linux or macOS, it falls back to a Unix domain
socket, which is likewise a filesystem path rather than a port.
"""
import json
import os
import sys
import threading
import time
import uuid

from .logs import get as _get_logger

log = _get_logger("instance")

IS_WINDOWS = sys.platform == "win32"
PIPE_NAME = r"\\.\pipe\Elysian-Player-Instance"
UNIX_SOCKET = os.path.join(
    os.path.expanduser("~"), ".elysian_player_instance.sock")
TOKEN_FILE = os.path.join(
    os.path.expanduser("~"), ".elysian_player_instance")

_BUFFER = 64 * 1024


def _token() -> str:
    """Shared secret, so stray local traffic is ignored."""
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
        log.warning("could not write the instance token", exc_info=True)
    return value


class _Lock:
    """Marks this process as the owner of the channel.

    Also carries anything handed over before the player was ready to act
    on it. Listening began when ownership was claimed, which is seconds
    before the window exists: a second launch in that gap used to find
    nothing listening, decide it was the only copy, and open a second
    player.
    """

    def __init__(self, name: str):
        self.name = name
        self.closed = False
        self.on_paths = None
        self.pending = []
        self.lock = threading.Lock()

    def deliver(self, paths) -> None:
        with self.lock:
            handler = self.on_paths
            if handler is None:
                self.pending.append(list(paths))
                return
        handler(paths)

    def attach(self, on_paths) -> None:
        """Install the real handler and flush whatever arrived early."""
        with self.lock:
            self.on_paths = on_paths
            waiting, self.pending = self.pending, []
        for paths in waiting:
            try:
                on_paths(paths)
            except Exception:
                log.exception("could not act on an early hand-off")

    def close(self) -> None:
        self.closed = True


# ---- Windows: named pipe -------------------------------------------------
# Called through ctypes rather than pywin32. pywin32-ctypes, which is already
# pinned, is a signing shim for PyInstaller and does not provide win32pipe;
# real pywin32 would add about ten megabytes to the build for one IPC channel.

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    PIPE_ACCESS_DUPLEX = 0x00000003
    PIPE_TYPE_BYTE = 0x00000000
    PIPE_READMODE_BYTE = 0x00000000
    PIPE_WAIT = 0x00000000
    PIPE_UNLIMITED_INSTANCES = 255
    ERROR_FILE_NOT_FOUND = 2
    ERROR_PIPE_BUSY = 231
    ERROR_BROKEN_PIPE = 109

    _k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.CreateFileW.restype = wintypes.HANDLE

    _k32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    _k32.CreateNamedPipeW.restype = wintypes.HANDLE

    _k32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _k32.ConnectNamedPipe.restype = wintypes.BOOL

    _k32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _k32.DisconnectNamedPipe.restype = wintypes.BOOL

    _k32.ReadFile.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.ReadFile.restype = wintypes.BOOL

    _k32.WriteFile.argtypes = [
        wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _k32.WriteFile.restype = wintypes.BOOL

    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

    def _close(handle):
        if handle and handle != INVALID_HANDLE_VALUE:
            try:
                _k32.CloseHandle(handle)
            except Exception:
                pass

    def _read(handle, size=_BUFFER):
        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD(0)
        ok = _k32.ReadFile(handle, buf, size, ctypes.byref(got), None)
        if not ok:
            err = ctypes.get_last_error()
            if err != ERROR_BROKEN_PIPE:
                raise OSError(err, "ReadFile failed")
        return buf.raw[:got.value]

    def _write(handle, data: bytes) -> None:
        put = wintypes.DWORD(0)
        if not _k32.WriteFile(handle, data, len(data), ctypes.byref(put), None):
            raise OSError(ctypes.get_last_error(), "WriteFile failed")


def _win_open_client():
    """Connect to a running instance's pipe, or None."""
    handle = _k32.CreateFileW(
        PIPE_NAME, GENERIC_READ | GENERIC_WRITE, 0, None,
        OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return None
    return handle


def _win_try_acquire():
    handle = _win_open_client()
    if handle is None:
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_BUSY:
            return None            # serving, just busy right now
        return _Lock(PIPE_NAME)    # ERROR_FILE_NOT_FOUND: nobody is serving
    _close(handle)
    return None                    # a live instance answered


def _win_hand_off(paths) -> bool:
    payload = json.dumps({"token": _token(), "paths": list(paths)}).encode()
    handle = _win_open_client()
    if handle is None:
        return False
    try:
        _write(handle, payload)
        return _read(handle, 16) == b"ok"
    except OSError:
        log.debug("hand-off failed", exc_info=True)
        return False
    finally:
        _close(handle)


def _win_listen(lock) -> threading.Thread:
    expected = _token()

    def loop():
        while not lock.closed:
            handle = _k32.CreateNamedPipeW(
                PIPE_NAME, PIPE_ACCESS_DUPLEX,
                PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
                PIPE_UNLIMITED_INSTANCES, _BUFFER, _BUFFER, 0, None)
            if handle == INVALID_HANDLE_VALUE:
                log.error("could not create the instance pipe (error %d)",
                          ctypes.get_last_error())
                return
            try:
                _k32.ConnectNamedPipe(handle, None)
                raw = _read(handle)
                if not raw:
                    continue
                data = json.loads(raw.decode())
                if data.get("token") != expected:
                    log.warning("rejected a hand-off with a bad token")
                    continue
                paths = [p for p in data.get("paths", [])
                         if isinstance(p, str) and os.path.isfile(p)]
                _write(handle, b"ok")
                lock.deliver(paths)
            except Exception:
                log.warning("bad hand-off from another instance", exc_info=True)
                continue
            finally:
                try:
                    _k32.DisconnectNamedPipe(handle)
                except Exception:
                    pass
                _close(handle)

    thread = threading.Thread(target=loop, name="elysian-instance", daemon=True)
    thread.start()
    return thread


# ---- Elsewhere: Unix domain socket ---------------------------------------

def _unix_try_acquire():
    import socket

    def bind():
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(UNIX_SOCKET)
        s.listen(4)
        held = _Lock(UNIX_SOCKET)
        held.sock = s
        return held

    try:
        return bind()
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        probe.connect(UNIX_SOCKET)
        probe.close()
        return None                   # a live instance answered
    except OSError:
        pass
    # Stale file left by a crash; clear it and try once more.
    try:
        os.unlink(UNIX_SOCKET)
        return bind()
    except OSError:
        return None


def _unix_hand_off(paths) -> bool:
    import socket

    payload = json.dumps({"token": _token(), "paths": list(paths)}).encode()
    try:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(1.5)
        c.connect(UNIX_SOCKET)
        with c:
            c.sendall(payload)
            c.shutdown(socket.SHUT_WR)
            return c.recv(16) == b"ok"
    except OSError:
        return False


def _unix_listen(lock) -> threading.Thread:
    sock = lock.sock
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
                    raw = b"".join(chunks)
                    if not raw:
                        # try_acquire()'s liveness probe connects and closes
                        # without sending anything. Not an error.
                        continue
                    data = json.loads(raw.decode())
                    if data.get("token") != expected:
                        log.warning("rejected a hand-off with a bad token")
                        continue
                    paths = [p for p in data.get("paths", [])
                             if isinstance(p, str) and os.path.isfile(p)]
                    conn.sendall(b"ok")
                    lock.deliver(paths)
                except Exception:
                    log.warning("bad hand-off from another instance",
                                exc_info=True)
                    continue

    thread = threading.Thread(target=loop, name="elysian-instance", daemon=True)
    thread.start()
    return thread


# ---- public API ----------------------------------------------------------

def try_acquire():
    """Claim the channel. None means another copy already owns it.

    If the check itself fails, a lock is returned anyway. A problem here must
    never stop the player from opening; the worst case is the behaviour from
    before single-instance existed.
    """
    try:
        lock = _win_try_acquire() if IS_WINDOWS else _unix_try_acquire()
    except Exception:
        log.warning("single-instance check unavailable; starting without it",
                    exc_info=True)
        return _Lock("unavailable")
    if lock is None:
        return None
    # Listen straight away. Waiting until the window was ready left several
    # seconds during which a second launch found nothing listening and
    # opened itself; anything handed over in the meantime is held on the
    # lock until serve() installs the real handler.
    try:
        if IS_WINDOWS:
            _win_listen(lock)
        else:
            _unix_listen(lock)
    except Exception:
        log.error("could not listen for other instances", exc_info=True)
    return lock


def _allow_foreground() -> None:
    """Let the running copy take the foreground from us.

    Windows only permits the process that currently owns the foreground to
    give it away. This process does, having just been launched, and the one
    being handed to does not: without this its window can only flash in the
    taskbar instead of coming forward.
    """
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ASFW_ANY = -1
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
        user32.AllowSetForegroundWindow.restype = ctypes.c_bool
        user32.AllowSetForegroundWindow(ctypes.c_ulong(ASFW_ANY))
    except Exception:
        log.debug("could not release the foreground", exc_info=True)


def hand_off(paths) -> bool:
    """Give these paths to the running instance. True if it accepted.

    Retried briefly: the owner is momentarily without a pipe between one
    hand-off and the next, and giving up on the first miss would open a
    second player over a gap of a millisecond.
    """
    _allow_foreground()
    for attempt in range(6):
        try:
            if _win_hand_off(paths) if IS_WINDOWS else _unix_hand_off(paths):
                return True
        except Exception:
            log.debug("hand-off attempt failed", exc_info=True)
        time.sleep(0.15)
    return False


def serve(lock, on_paths):
    """Install the handler for hand-offs, and replay any that came early.

    Listening itself started in try_acquire(); this only says what to do
    with what arrives.
    """
    if lock is None:
        return None
    try:
        lock.attach(on_paths)
    except Exception:
        log.error("could not accept hand-offs", exc_info=True)
    return None
