"""System tray for the desktop build on macOS — pyobjc ``NSStatusItem``.

This is the backend ``backend.tray`` dispatches to on macOS.  pywebview's
window backend there (cocoa) already gives the same ``closing`` veto and
thread-safe ``hide``/``show`` as GTK, so the only missing half is the icon.

Why NOT pystray: its darwin backend can only run the icon loop on the **main**
thread (``run_detached`` merely marks ready and never runs the loop), and that
is exactly where pywebview's own Cocoa loop lives.  ``NSStatusItem`` instead
hangs off the ``NSApplication`` loop pywebview already runs — with **zero new
dependencies**, because pywebview on macOS already requires pyobjc (Cocoa).

Threading: every AppKit call below is marshalled to the main thread with
``AppHelper.callAfter`` (the same helper pywebview's cocoa backend uses) and
waited for when the caller needs the result.  Menu actions run on their own
daemon threads, so a quit never blocks the main loop.

Notes:

* ``NSMenuItem.target`` is *not* retained by AppKit, so the Python target
  object is kept alive on the returned :class:`Tray`.
* The notification fallback uses ``osascript`` — the same channel pystray's
  darwin backend uses — best-effort and silent when the host app has no
  notification permission.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

from backend.tray import run_off_gui_thread, tray_labels

log = logging.getLogger(__name__)

#: How long to wait for the main thread to hand back a created status item.
CREATE_TIMEOUT = 5.0
#: Tray title refresh interval (seconds).
STATUS_INTERVAL_S = 5.0


def available() -> bool:
    """True when a tray icon can probably be created here."""
    try:
        import AppKit  # noqa: F401
        import Foundation  # noqa: F401
        from PyObjCTools import AppHelper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


class Tray:
    """A live status item.  Create it with :func:`create_tray`."""

    def __init__(self, status_item, *, title: str,
                 status_provider: Optional[Callable[[], str]] = None,
                 target=None, menu=None):
        self._status_item = status_item
        self._status_provider = status_provider
        # AppKit does NOT retain NSMenuItem.target, and the proxies behind the
        # menu are ours — keep Python references for the item's lifetime.
        self._target = target
        self._menu = menu
        self._closed = False
        self._wake = threading.Event()
        self.update_status()
        self._start_ticking()

    # -- status ---------------------------------------------------------------
    def _apply(self, text: str) -> None:
        """Set the title/tooltip — runs on the main thread."""
        try:
            button = self._status_item.button()
            if button is None:              # pre-10.10 status item
                self._status_item.setTitle_(text)
                return
            button.setTitle_(text)
            button.setToolTip_(text)
        except Exception:  # noqa: BLE001 - the tooltip must never break the app
            log.debug("could not update the tray title", exc_info=True)

    def update_status(self) -> None:
        """Refresh the title from the caller's status provider."""
        if self._closed or self._status_provider is None:
            return
        try:
            text = self._status_provider()
        except Exception:  # noqa: BLE001
            log.debug("tray status provider failed", exc_info=True)
            return
        if not text:
            return
        _main_call(self._apply, text)

    def _loop(self) -> None:
        while not self._closed and not self._wake.wait(STATUS_INTERVAL_S):
            self.update_status()

    def _start_ticking(self) -> None:
        thread = threading.Thread(target=self._loop, name="tray-status",
                                  daemon=True)
        thread.start()

    # -- teardown -------------------------------------------------------------
    def shutdown(self) -> None:
        """Remove the status item.  Safe to call twice, never raises."""
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        item = self._status_item
        done = threading.Event()

        def _remove() -> None:
            try:
                from AppKit import NSStatusBar

                NSStatusBar.systemStatusBar().removeStatusItem_(item)
            except Exception:  # noqa: BLE001
                log.debug("could not retire the status item", exc_info=True)
            finally:
                done.set()

        _main_call(_remove)
        # Bounded: the removal must land before the app terminates, but a
        # wedged main loop must not hold a quit hostage.
        done.wait(CREATE_TIMEOUT)


def _main_call(function, *args) -> None:
    """Run ``function`` on the main thread (the Cocoa loop lives there).

    Best-effort: when the loop is gone (an app that is shutting down) the call
    simply never happens — never an error.
    """
    try:
        from PyObjCTools import AppHelper

        AppHelper.callAfter(function, *args)
    except Exception:  # noqa: BLE001
        log.debug("could not schedule a main-thread tray call", exc_info=True)


def _escape(text: str) -> str:
    """Escape a string for an ``osascript`` AppleScript literal."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, body: str = "") -> bool:
    """Best-effort desktop notification through ``osascript``.

    Returns False when osascript is not there or the host app lacks
    notification permission — "no notification", never an error.
    """
    script = 'display notification "{}" with title "{}"'.format(
        _escape(body), _escape(title))
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:  # noqa: BLE001
        log.debug("desktop notification failed", exc_info=True)
        return False
    return proc.returncode == 0


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

    Never raises: every failure path (no pyobjc, a missing icon, no run loop)
    ends in ``None`` so the caller keeps its default behaviour.
    """
    try:
        import AppKit
        from Foundation import NSObject
    except Exception:  # noqa: BLE001
        log.info("no tray support on macOS: pyobjc is unavailable")
        return None

    icon_file = Path(icon_path)
    if not icon_file.exists():
        log.warning("tray icon missing at %s; not creating a tray", icon_file)
        return None

    labels = tray_labels(locale_name)
    result: dict = {}
    done = threading.Event()

    def _build() -> None:
        try:
            NSStatusBar = AppKit.NSStatusBar
            NSMenu = AppKit.NSMenu
            NSMenuItem = AppKit.NSMenuItem
            NSImage = AppKit.NSImage
            NSVariableStatusItemLength = AppKit.NSVariableStatusItemLength

            # The menu target.  AppKit does not retain it — the Tray keeps a
            # Python reference for the item's lifetime.
            class _Target(NSObject):
                def initWithCallbacks_(self, callbacks):
                    self = super().init()
                    if self is None:
                        return None
                    self._callbacks = callbacks
                    return self

                def showWindow_(self, sender):
                    self._callbacks["show"]()

                def openBrowser_(self, sender):
                    self._callbacks["browser"]()

                def quitApp_(self, sender):
                    self._callbacks["quit"]()

            target = _Target.alloc().initWithCallbacks_({
                # Menu actions fire on the MAIN thread; keep the loop free.
                "show": lambda: run_off_gui_thread(on_show),
                "browser": lambda: run_off_gui_thread(on_open_browser),
                "quit": lambda: run_off_gui_thread(on_quit),
            })

            menu = NSMenu.alloc().initWithTitle_(title or "PDF OCR Embed")
            for label, selector in (
                (labels["show"], "showWindow:"),
                (labels["browser"], "openBrowser:"),
                (labels["quit"], "quitApp:"),
            ):
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    label, selector, "")
                item.setTarget_(target)
                menu.addItem_(item)

            status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(
                NSVariableStatusItemLength)
            status_item.setMenu_(menu)      # left click opens the menu
            image = NSImage.alloc().initWithContentsOfFile_(str(icon_file))
            if image is not None:
                try:
                    image.setSize_((18, 18))    # menu-bar icon size
                except Exception:  # noqa: BLE001
                    pass
                status_item.button().setImage_(image)
            result["tray"] = Tray(status_item, title=title,
                                  status_provider=status_provider,
                                  target=target, menu=menu)
        except Exception:  # noqa: BLE001 - any failure -> no tray
            log.warning("could not create the tray icon", exc_info=True)
        finally:
            done.set()

    _main_call(_build)   # AppKit must be touched from the main thread only
    if not done.wait(CREATE_TIMEOUT):
        # The main loop never served the call: no tray is better than a dead
        # one (the caller then keeps "closing quits").
        log.warning("the main thread did not answer within %.0fs; no tray",
                    CREATE_TIMEOUT)
        return None
    tray = result.get("tray")
    if tray is not None:
        log.info("system tray icon created (NSStatusItem)")
    return tray
