"""A lightweight outline-only overlay window that previews a frameless-window
resize before it is committed.

Why this exists: the app's main window is frameless (see host.py), so
Windows never draws a native resize border for it, and the frontend has to
fake one entirely in JS/Python. Two approaches were tried before this one:

  1. Resizing the real window continuously on every pointermove. This is
     what pywebview's own window.resize()/SetWindowPos already does for the
     working title-bar drag, so it looked like the right tool - but
     WebView2 keeps its own pointer capture in a separate process, and
     repeatedly moving/resizing the top-level HWND out from under an
     active capture can drop it. When that happened mid-drag the window
     would stop tracking the mouse, then snap back into a capture some
     distance from the cursor - the "window jumps off the mouse, then
     reattaches far away" bug.
  2. Deferring the real resize to pointerup only (still done today, see
     win_resize_to in api.py, and SAFE_RESIZE in app.js). This fixed the
     jumping bug, since the real window is now only ever touched once per
     drag, but it also removed all live visual feedback: nothing on
     screen changes shape until the mouse is released.

This module is what restores the live feedback without bringing the
jumping bug back. It is a second, completely separate, contentless Win32
window: a thin rectangular outline with nothing painted in its middle
(carved out with SetWindowRgn), shown click-through and always-on-top.
It follows the cursor across the whole drag - updated as often as JS
likes - and is hidden the instant the drag ends, right before the one
real win_resize_to call commits the actual size. Because it never touches
the WebView2-hosting window, there is no pointer capture for it to
disturb.

Windows-only, matching the rest of this app (see host.py's own ctypes use
for _raise_to_front). No-ops everywhere else, so importing this on a
platform pywebview doesn't run frameless windows on at all is harmless.
"""
import sys
import threading

from ..logs import get as _get_logger

log = _get_logger("resize_ghost")

_WIN = sys.platform == "win32"

if _WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # -- constants ---------------------------------------------------------
    WS_POPUP = 0x80000000
    WS_EX_LAYERED = 0x00080000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    # Click-through: the overlay must never steal a mouse message from the
    # app window underneath, or it could interfere with the very pointer
    # capture this whole design exists to protect.
    WS_EX_TRANSPARENT = 0x00000020
    LWA_ALPHA = 0x00000002
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    HWND_TOPMOST = -1
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    RGN_DIFF = 4
    IDC_ARROW = 32512

    # Accent-hi (#e04b3c) as a COLORREF, which packs 0x00BBGGRR.
    _BORDER_COLOR = 0x3C4BE0
    _BORDER_THICKNESS = 3
    _OVERLAY_ALPHA = 235

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = ctypes.c_long
    user32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
    user32.SetWindowRgn.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    user32.LoadCursorW.restype = wintypes.HICON

    gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int]
    gdi32.CreateRectRgn.restype = wintypes.HRGN
    gdi32.CombineRgn.argtypes = [wintypes.HRGN, wintypes.HRGN,
                                 wintypes.HRGN, ctypes.c_int]
    gdi32.CombineRgn.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH

    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE

    _CLASS_NAME = "ElysianResizeGhost"
    _lock = threading.Lock()
    _state = {"hwnd": None, "registered": False, "brush": None}

    def _ensure_registered() -> None:
        if _state["registered"]:
            return
        hinst = kernel32.GetModuleHandleW(None)
        # The class background brush does all the drawing: with no custom
        # WM_PAINT handler, DefWindowProc fills WM_ERASEBKGND with exactly
        # this brush, which is all a plain solid-colour frame needs. That
        # sidesteps a Python-side WNDPROC callback (and the GC-lifetime and
        # PAINTSTRUCT bookkeeping that would come with one) entirely.
        brush = gdi32.CreateSolidBrush(_BORDER_COLOR)
        _state["brush"] = brush
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p)
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = None
        wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
        wc.hbrBackground = brush
        wc.lpszMenuName = None
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS: harmless
                log.warning("could not register the resize-ghost window "
                            "class (error %s)", err)
        _state["registered"] = True

    def _ensure_window():
        with _lock:
            if _state["hwnd"]:
                return _state["hwnd"]
            _ensure_registered()
            hinst = kernel32.GetModuleHandleW(None)
            ex_style = (WS_EX_LAYERED | WS_EX_TOOLWINDOW
                       | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT)
            hwnd = user32.CreateWindowExW(
                ex_style, _CLASS_NAME, "", WS_POPUP,
                0, 0, 1, 1, None, None, hinst, None)
            if hwnd:
                user32.SetLayeredWindowAttributes(
                    hwnd, 0, _OVERLAY_ALPHA, LWA_ALPHA)
            _state["hwnd"] = hwnd
            return hwnd

    def _frame_region(width: int, height: int, thickness: int):
        """A rectangular ring: the outer rect minus an inset inner one.

        SetWindowRgn takes ownership of whatever handle it is given (and
        destroys whatever region it replaces), so only the inner, now-
        redundant region needs cleaning up here - the outer one is handed
        straight off to the caller.
        """
        outer = gdi32.CreateRectRgn(0, 0, width, height)
        inner = gdi32.CreateRectRgn(
            thickness, thickness,
            max(thickness, width - thickness),
            max(thickness, height - thickness))
        gdi32.CombineRgn(outer, outer, inner, RGN_DIFF)
        gdi32.DeleteObject(inner)
        return outer

    def show(x: int, y: int, width: int, height: int) -> None:
        """Move the outline to this screen rect, creating/showing it first
        if this is the start of a new drag. Safe to call at any rate."""
        try:
            hwnd = _ensure_window()
            if not hwnd:
                return
            w = max(2 * _BORDER_THICKNESS + 1, int(width))
            h = max(2 * _BORDER_THICKNESS + 1, int(height))
            region = _frame_region(w, h, _BORDER_THICKNESS)
            user32.SetWindowRgn(hwnd, region, True)
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, int(x), int(y), w, h,
                SWP_NOACTIVATE | SWP_SHOWWINDOW)
        except Exception:
            log.debug("resize ghost show failed", exc_info=True)

    def hide() -> None:
        try:
            hwnd = _state["hwnd"]
            if hwnd:
                user32.ShowWindow(hwnd, SW_HIDE)
        except Exception:
            log.debug("resize ghost hide failed", exc_info=True)

    def destroy() -> None:
        """Torn down on app shutdown. Not strictly required - the window
        goes away with the process either way - but leaves nothing lying
        around if this module is ever reused somewhere longer-lived."""
        try:
            hwnd = _state["hwnd"]
            if hwnd:
                user32.DestroyWindow(hwnd)
        except Exception:
            log.debug("resize ghost destroy failed", exc_info=True)
        finally:
            _state["hwnd"] = None

else:
    def show(x: int, y: int, width: int, height: int) -> None:  # noqa: D401
        pass

    def hide() -> None:
        pass

    def destroy() -> None:
        pass
