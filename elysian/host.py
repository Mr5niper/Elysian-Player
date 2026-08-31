"""Window host.

Creates the frameless pywebview window, wires the DOM drop event (which is how
real file paths reach Python -- the browser alone only exposes filenames), and
handles a path passed on the command line so double-clicking an audio file in
Explorer opens it here.
"""
import os
import sys

import webview
from webview.dom import DOMEventHandler

from . import config, single_instance
from .api import Api

WEB_DIR = "elysian/web"


def _index_path() -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = os.path.join(base, WEB_DIR, "index.html")
        if os.path.isfile(candidate):
            return candidate
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "web", "index.html")


def _argv_paths() -> list[str]:
    """Audio files handed to us by Explorer via file association."""
    found = []
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        if os.path.isfile(arg) and \
                os.path.splitext(arg)[1].lower() in config.AUDIO_EXTENSIONS:
            found.append(os.path.abspath(arg))
    return found


def run() -> int:
    # If a copy is already running, hand it the file and exit rather than
    # opening a second player.
    lock = single_instance.try_acquire()
    if lock is None:
        opened = _argv_paths()
        if opened and single_instance.hand_off(opened):
            return 0
        if not opened and single_instance.hand_off([]):
            return 0

    api = Api()

    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = False

    window = webview.create_window(
        title=config.APP_NAME,
        url=_index_path(),
        js_api=api,
        width=config.WINDOW_WIDTH,
        height=config.WINDOW_HEIGHT,
        min_size=(config.MIN_WIDTH, config.MIN_HEIGHT),
        frameless=True,
        easy_drag=False,
        background_color="#131316",
        text_select=False,
        confirm_close=False,
    )
    api.attach(window)

    def on_drop(event):
        """pywebview attaches pywebviewFullPath to each dropped file.

        Only the drop is handled here. Highlighting during the drag is done
        entirely in JavaScript -- routing every dragover across the bridge to
        toggle a CSS class made the overlay strobe.
        """
        try:
            files = (event.get("dataTransfer") or {}).get("files") or []
            paths = [f.get("pywebviewFullPath") for f in files]
            paths = [p for p in paths if p]
            if paths:
                api.ingest(paths)
        except Exception:
            pass

    def bind(win):
        try:
            doc = win.dom.document
            doc.events.drop += DOMEventHandler(on_drop, False, False)
        except Exception:
            pass

        if lock is not None:
            def incoming(paths):
                if paths:
                    target = api.open_paths(paths)
                    if target >= 0:
                        api.play_id(target)
                # Launching with no file should still surface the window
                # rather than appear to do nothing.
                try:
                    win.restore()
                    win.show()
                except Exception:
                    pass
            single_instance.serve(lock, incoming)

        api.boot()
        opened = _argv_paths()
        if opened:
            target = api.open_paths(opened)
            if target >= 0:
                api.play_id(target)

    window.events.closing += lambda: api.close()

    icon = config.resource_path("icon.ico")
    start_kwargs = {"func": bind, "args": window}
    if os.path.isfile(icon):
        start_kwargs["icon"] = icon

    def _start(kwargs):
        webview.start(**kwargs)

    if os.environ.get("ELYSIAN_DEBUG"):
        start_kwargs["debug"] = True
        webview.settings["OPEN_DEVTOOLS_IN_DEBUG"] = True

    try:
        _start(start_kwargs)
    except Exception:
        # Never let a decorative failure such as an icon the platform cannot
        # load stop the player from opening.
        start_kwargs.pop("icon", None)
        _start(start_kwargs)
    api.close()
    return 0
