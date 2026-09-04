"""Native child-window host for engine-rendered video.

Windows-only. Owns a child HWND inside the pywebview host window that the
owned video engine can be handed through set_video_target().

Two deliberate departures from the naive version of this layer:

* The child window is created on a dedicated thread that runs a real
  message pump. CreateWindowExW binds a window to the creating thread's
  message queue, and neither the JS bridge thread nor the Api worker pumps
  messages, so a child created there would never service WM_PAINT. That is
  invisible today, when the engine does not paint yet, and fatal the day it
  does; the pump costs a page of code now and saves re-plumbing this layer
  during the native painting phase. Moving, showing and hiding are
  message-based and remain safe from any thread.

* On non-Windows platforms this module imports cleanly and VideoHost is a
  no-op with available False, so the shell (and its tests) run anywhere
  even though the binding itself is Windows-only.

A note on ctypes.wintypes gaps: it does not define every real Win32 type.
LRESULT and HCURSOR are both absent (confirmed directly against the module,
not assumed) even though they are ordinary Win32 types with real ABI
meaning. Real Win32 headers define HCURSOR as a plain alias of HICON
(`typedef HICON HCURSOR;` in winuser.h) and LRESULT as a pointer-sized
signed integer identical in layout to LPARAM, so wintypes.HICON and
wintypes.LPARAM stand in for them here; both are ordinary struct/argtype
substitutions; and GetModuleHandleW gets an explicit HMODULE restype for
the same reason, since ctypes silently assumes a 32-bit c_int return for
any function whose restype was never set, which is not safe for a handle
value on 64-bit Windows.

Z-order note: move() and show() force this window to the top of its
Z-order among its own siblings (WS_CHILD windows sharing the same parent)
on every call, rather than leaving Z-order untouched as an earlier version
of this file did. pywebview's WinForms backend adds its WebView2 control
directly to the host Form's Controls collection, which makes WebView2's
own HWND a direct child of the same Form HWND this window also attaches
to - true Z-order siblings, not unrelated windows. A child window is not
guaranteed to stay above a sibling added or repainted later purely from
creation order, and this class of app (a raw child HWND that must stay
visually above a browser control hosted in the same parent) is exactly
where relying on default Z-order has been the wrong assumption.
"""
from __future__ import annotations

import os
import threading

from .logs import get as _get_logger

log = _get_logger("videohost")

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_CLIPSIBLINGS = 0x04000000
    WS_CLIPCHILDREN = 0x02000000
    SW_HIDE = 0
    SW_SHOWNA = 8            # show without stealing activation
    HWND_TOP = 0             # SetWindowPos special value, not a real handle
    SWP_NOACTIVATE = 0x0010
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    BLACK_BRUSH = 4
    ERROR_CLASS_ALREADY_EXISTS = 1410

    WNDPROC = ctypes.WINFUNCTYPE(
        wintypes.LPARAM,   # LRESULT: pointer-sized signed, LPARAM matches
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HICON),   # HCURSOR: see module docstring
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = wintypes.LPARAM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.PostQuitMessage.argtypes = [ctypes.c_int]
    user32.PostQuitMessage.restype = None
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                   wintypes.HWND, wintypes.UINT,
                                   wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.restype = wintypes.LPARAM
    gdi32.GetStockObject.argtypes = [ctypes.c_int]
    gdi32.GetStockObject.restype = wintypes.HBRUSH

    def _wndproc(hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    #: Held at module level so the callback thunk outlives every window.
    _WNDPROC = WNDPROC(_wndproc)
    _CLASS_NAME = "ElysianVideoHostWindow"
    _registered = False

    def _register_class() -> None:
        global _registered
        if _registered:
            return
        wc = WNDCLASSW()
        wc.lpfnWndProc = _WNDPROC
        wc.hInstance = kernel32.GetModuleHandleW(None)
        # Black background matches the CSS slot, so an unpainted frame
        # reads as an idle screen rather than uninitialised garbage.
        wc.hbrBackground = gdi32.GetStockObject(BLACK_BRUSH)
        wc.lpszClassName = _CLASS_NAME
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise OSError(ctypes.get_last_error(), "RegisterClassW failed")
        _registered = True


class VideoHost:
    """Owns the native child window and the thread that pumps its messages.

    ensure_child() blocks briefly (one thread start plus one CreateWindowExW)
    the first time; every later call is a handle check. All methods are safe
    from any thread and are no-ops when unavailable or unattached.
    """

    def __init__(self):
        self.available = IS_WINDOWS
        self.parent_hwnd = 0
        self.child_hwnd = 0
        self._thread: threading.Thread | None = None
        self._created = threading.Event()
        self._create_error: str | None = None

    def attach_parent(self, hwnd: int) -> None:
        self.parent_hwnd = int(hwnd or 0)

    # -- creation, on the pump thread -------------------------------------

    def _pump(self) -> None:
        try:
            _register_class()
            hwnd = user32.CreateWindowExW(
                0, _CLASS_NAME, "Elysian Video Host",
                WS_CHILD | WS_VISIBLE | WS_CLIPSIBLINGS | WS_CLIPCHILDREN,
                0, 0, 16, 16,
                self.parent_hwnd, None,
                kernel32.GetModuleHandleW(None), None)
            if not hwnd:
                self._create_error = (
                    f"CreateWindowExW failed: {ctypes.get_last_error()}")
                return
            self.child_hwnd = int(hwnd)
            log.debug("video host child window created: hwnd=%r parent=%r",
                     self.child_hwnd, self.parent_hwnd)
            # Forced immediately on creation too, not just on the next
            # move()/show(): a window that starts out behind WebView2's own
            # HWND has nothing else promoting it to the front until the
            # first geometry update, which is one extra frame of "nothing
            # visible yet" at minimum, and depending on timing might not
            # happen before the first real video frame is presented.
            user32.SetWindowPos(self.child_hwnd, HWND_TOP, 0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        finally:
            self._created.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        self.child_hwnd = 0

    def ensure_child(self) -> int:
        if not self.available:
            return 0
        if not self.parent_hwnd:
            # Silent before this line: nothing ever explained why the
            # video pane stays black forever if attach_parent() was never
            # given a real handle (host.py logs a warning at startup when
            # _native_hwnd() fails, but that log line is easy to miss, and
            # this is the exact point where that earlier failure becomes
            # "no window, ever, no further trace").
            log.warning("cannot create the video child window: no parent "
                        "window handle was ever attached")
            return 0
        if self.child_hwnd and user32.IsWindow(self.child_hwnd):
            return self.child_hwnd
        if self._thread is not None and self._thread.is_alive():
            # Creation in flight from another caller; wait it out.
            self._created.wait(timeout=2.0)
            return self.child_hwnd
        self._created.clear()
        self._create_error = None
        self._thread = threading.Thread(target=self._pump,
                                        name="elysian-videohost",
                                        daemon=True)
        self._thread.start()
        if not self._created.wait(timeout=2.0):
            log.error("video host window creation timed out")
            return 0
        if self._create_error:
            log.error("video host window creation failed: %s",
                      self._create_error)
            return 0
        return self.child_hwnd

    # -- geometry and visibility, safe from any thread ---------------------

    def move(self, x: int, y: int, w: int, h: int) -> None:
        if self.ensure_child():
            # HWND_TOP, not SWP_NOZORDER: forces this window back to the
            # front of its Z-order siblings on every reposition, rather
            # than trusting whatever order it happened to end up in. See
            # the module docstring's Z-order note for why that trust was
            # misplaced.
            user32.SetWindowPos(self.child_hwnd, HWND_TOP,
                                int(x), int(y), int(w), int(h),
                                SWP_NOACTIVATE)

    def show(self) -> None:
        if self.ensure_child():
            user32.ShowWindow(self.child_hwnd, SW_SHOWNA)
            user32.SetWindowPos(self.child_hwnd, HWND_TOP, 0, 0, 0, 0,
                               SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def hide(self) -> None:
        if self.available and self.child_hwnd \
                and user32.IsWindow(self.child_hwnd):
            user32.ShowWindow(self.child_hwnd, SW_HIDE)

    def destroy(self) -> None:
        """Close the child and stop its pump. Safe to call more than once."""
        if not self.available:
            return
        if self.child_hwnd and user32.IsWindow(self.child_hwnd):
            # DestroyWindow only works from the owning thread; WM_CLOSE is
            # posted instead, DefWindowProc destroys, WM_DESTROY posts the
            # quit that ends the pump.
            user32.PostMessageW(self.child_hwnd, WM_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self.child_hwnd = 0
