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

from pathlib import Path

from . import config, single_instance
from . import paths as pathutil
from .api import Api

from .logs import get as _get_logger

log = _get_logger("host")


WEB_DIR = "elysian/web"


def _index_path() -> str:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / WEB_DIR / "index.html"
        if candidate.is_file():
            return str(candidate)
    return str(Path(__file__).resolve().parent / "web" / "index.html")


def _argv_paths() -> list[str]:
    """Audio files handed to us by Explorer via file association."""
    found = []
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        candidate = Path(arg)
        if candidate.suffix.lower() in config.AUDIO_EXTENSIONS \
                and candidate.is_file():
            # absolute, not resolved: keep the case the user sees, and do not
            # follow symlinks over a share
            found.append(pathutil.absolute(candidate))
    return found


def run() -> int:
    log.info("Elysian Player %s starting", config.APP_VERSION)
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
    api._assert_bridge_surface()

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
            log.exception("file drop failed")

    def bind(win):
        try:
            doc = win.dom.document
            doc.events.drop += DOMEventHandler(on_drop, False, False)
        except Exception:
            log.error("could not bind the drop handler; dragging files onto "
                      "the window will not work", exc_info=True)

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
                    log.debug("could not raise the window", exc_info=True)
            single_instance.serve(lock, incoming)

        api.boot()
        opened = _argv_paths()
        if opened:
            target = api.open_paths(opened)
            if target >= 0:
                api.play_id(target)

    # Keep the toggle honest when the window is maximised by other means:
    # Win+Up, a title bar double-click, or the OS restoring the session.
    window.events.maximized += lambda: api.set_maximized(True)
    window.events.restored += lambda: api.set_maximized(False)
    window.events.closing += lambda: api.close()

    icon = config.resource_path("icon.ico")
    start_kwargs = {"func": bind, "args": window}
    if Path(icon).is_file():
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
        log.warning("startup failed; retrying without the window icon",
                    exc_info=True)
        start_kwargs.pop("icon", None)
        _start(start_kwargs)
    api.close()
    return 0
