"""System tray for the desktop build on Windows — pystray.

This is the backend ``backend.tray`` dispatches to on Windows.  pywebview's
window backend there (winforms) already gives the same ``closing`` veto and
thread-safe ``hide``/``show`` as GTK, so the only missing half is the icon
itself.  pystray provides it: ``Icon.run_detached`` runs the Win32 message loop
on its own thread, which is exactly the loop we must not hand-write.

Verified against the pystray 0.19.5 sources:

* ``run_detached(setup)`` starts the loop on a separate thread; ``setup`` runs
  (on a pystray thread) once the message window exists, and **a custom setup
  must show the icon itself** (``visible = True``) — the default setup that
  does so is only used when no ``setup`` is passed.
* if the loop thread dies before that, ``setup`` never runs — hence the
  ready-wait with a timeout below: no tray is better than a dead one.
* ``stop()`` posts ``WM_STOP`` and joins the setup thread (bounded); ``title``
  updates go through ``Shell_NotifyIcon(NIM_MODIFY)``, a global API, so they
  are safe from our tick thread.
* ``notify()`` raises a tray balloon (``NIF_INFO``); Windows converts those to
  toast notifications.
* the left button activates the menu item marked ``default`` — so "Show
  window" is both the click action and the first menu entry.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from backend.tray import run_off_gui_thread, tray_labels

log = logging.getLogger(__name__)

#: How long to wait for pystray's message window to come up.
CREATE_TIMEOUT = 5.0
#: Tray tooltip refresh interval (seconds).
STATUS_INTERVAL_S = 5.0


def available() -> bool:
    """True when a tray icon can probably be created here."""
    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class Tray:
    """A live tray icon.  Create it with :func:`create_tray`."""

    def __init__(self, icon, *, title: str,
                 status_provider: Optional[Callable[[], str]] = None):
        self._icon = icon
        self._status_provider = status_provider
        self._closed = False
        self._wake = threading.Event()
        if title:
            try:
                icon.title = title
            except Exception:  # noqa: BLE001
                log.debug("could not set the initial tray title", exc_info=True)
        self.update_status()
        self._start_ticking()

    # -- status ---------------------------------------------------------------
    def update_status(self) -> None:
        """Refresh the tooltip from the caller's status provider.

        ``Icon.title`` updates the tray tooltip through ``Shell_NotifyIcon``,
        which is a global Win32 API — safe to call from this thread.
        """
        if self._closed or self._status_provider is None:
            return
        try:
            text = self._status_provider()
        except Exception:  # noqa: BLE001 - the tooltip must never break the app
            log.debug("tray status provider failed", exc_info=True)
            return
        if not text:
            return
        try:
            self._icon.title = text
        except Exception:  # noqa: BLE001
            log.debug("could not update the tray tooltip", exc_info=True)

    def _loop(self) -> None:
        while not self._closed and not self._wake.wait(STATUS_INTERVAL_S):
            self.update_status()

    def _start_ticking(self) -> None:
        thread = threading.Thread(target=self._loop, name="tray-status",
                                  daemon=True)
        thread.start()

    # -- notification ---------------------------------------------------------
    def notify(self, title: str, body: str = "") -> bool:
        """A tray balloon (a toast notification on Windows 10+)."""
        if self._closed:
            return False
        try:
            self._icon.notify(body or title, title)
            return True
        except Exception:  # noqa: BLE001
            log.debug("tray notification failed", exc_info=True)
            return False

    # -- teardown -------------------------------------------------------------
    def shutdown(self) -> None:
        """Remove the icon.  Safe to call twice, never raises."""
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        try:
            self._icon.stop()      # no-op when the loop never came up
        except Exception:  # noqa: BLE001
            log.debug("could not retire the tray icon", exc_info=True)


def create_tray(
    *,
    title: str,
    icon_path: Path,
    on_show: Callable[[], None],
    on_open_browser: Callable[[], None],
    on_quit: Callable[[], None],
    status_provider: Optional[Callable[[], str]] = None,
    locale_name: Optional[str] = None,
) -> Optional[Tray]:
    """Create the tray icon, or return None when it cannot be done.

    Never raises: every failure path (no pystray, an unreadable icon, a loop
    that never comes up) ends in ``None`` so the caller keeps its default
    behaviour.
    """
    try:
        import pystray
        from PIL import Image
    except Exception:  # noqa: BLE001
        log.info("no tray support on Windows: pystray/Pillow is unavailable")
        return None

    icon_file = Path(icon_path)
    if not icon_file.exists():
        log.warning("tray icon missing at %s; not creating a tray", icon_file)
        return None
    try:
        image = Image.open(icon_file)
    except Exception:  # noqa: BLE001
        log.warning("could not read the tray icon at %s; no tray", icon_file,
                    exc_info=True)
        return None

    labels = tray_labels(locale_name)

    def _menu_action(action: Callable[[], None]):
        # pystray invokes this on its own loop thread; keep the loop free.
        return lambda icon=None, item=None: run_off_gui_thread(action)

    try:
        icon = pystray.Icon(
            "pdf-ocr-embed",
            icon=image,
            title=title or "",
            menu=pystray.Menu(
                pystray.MenuItem(labels["show"], _menu_action(on_show),
                                 default=True),
                pystray.MenuItem(labels["browser"], _menu_action(on_open_browser)),
                pystray.MenuItem(labels["quit"], _menu_action(on_quit)),
            ),
        )
    except Exception:  # noqa: BLE001 - any failure -> no tray
        log.warning("could not build the tray icon", exc_info=True)
        return None

    ready = threading.Event()

    def _setup(ic) -> None:
        try:
            # A custom setup MUST show the icon itself (pystray's default
            # setup only does this when no setup is passed).
            ic.visible = True
        except Exception:  # noqa: BLE001
            log.debug("could not show the tray icon", exc_info=True)
        finally:
            ready.set()

    try:
        icon.run_detached(_setup)   # the Win32 loop runs on its own thread
    except Exception:  # noqa: BLE001
        log.warning("could not start the tray icon", exc_info=True)
        return None
    if not ready.wait(CREATE_TIMEOUT):
        # The loop thread died before its message window existed: no tray is
        # better than a dead one (the caller then keeps "closing quits").
        log.warning("the tray icon did not become ready within %.0fs; no tray",
                    CREATE_TIMEOUT)
        return None
    log.info("system tray icon created (pystray)")
    return Tray(icon, title=title, status_provider=status_provider)
