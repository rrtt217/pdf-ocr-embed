"""System tray for the desktop build on Linux — AppIndicator via PyGObject.

This is the backend ``backend.tray`` dispatches to on Linux.  The icon is
created against the desktop's StatusNotifier host through AppIndicator —
Ayatana first, the older namespace as a fallback — which is the same GTK stack
``requirements-desktop.txt`` already installs for the window.

Notes (verified on a KDE/Wayland session):

* ``Gtk.StatusIcon`` must NOT be used: it is XEmbed-era (deprecated in GTK
  3.14, removed in GTK 4) and is simply invisible on Wayland.
* GNOME has no tray by default (it needs the AppIndicator extension) — the
  icon may register without ever being visible there; the one-off hide notice
  and the WebUI's Quit button are the escapes.

Threading: the indicator is created on the GTK main loop (we marshal it with
``GLib.idle_add`` and wait), because that is the loop that serves its D-Bus
menu.  Menu actions then run on their own daemon threads, and the window calls
they make are pywebview's ``glib.idle_add`` wrappers, so nothing blocks or
touches GTK from the wrong thread.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

from backend.tray import run_off_gui_thread, tray_labels

log = logging.getLogger(__name__)

#: How long to wait for the GUI thread to hand back a created indicator.
CREATE_TIMEOUT = 5.0
#: Tray tooltip refresh interval (ms).
STATUS_INTERVAL_MS = 5000


def _indicator():
    """The AppIndicator module, or None when unavailable."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
    except Exception:  # noqa: BLE001 - no PyGObject/GTK at all
        return None
    for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(namespace, "0.1")
            module = __import__("gi.repository", fromlist=[namespace])
            return getattr(module, namespace)
        except Exception:  # noqa: BLE001 - try the next namespace
            continue
    return None


def available() -> bool:
    """True when a tray icon can probably be created here."""
    return _indicator() is not None


class Tray:
    """A live tray icon.  Create it with :func:`create_tray`."""

    def __init__(self, indicator, module, *, title: str, labels: dict,
                 status_provider: Optional[Callable[[], str]] = None):
        from gi.repository import GLib

        self._GLib = GLib
        self._indicator = indicator
        self._module = module
        self._labels = labels
        self._status_provider = status_provider
        self._source_id = None
        self._closed = False
        indicator.set_title(title)
        self.update_status()

    # -- status ---------------------------------------------------------------
    def update_status(self) -> None:
        """Refresh the tooltip from the caller's status provider."""
        if self._closed or self._status_provider is None:
            return
        try:
            text = self._status_provider()
        except Exception:  # noqa: BLE001 - the tooltip must never break the app
            log.debug("tray status provider failed", exc_info=True)
            return
        if text:
            try:
                self._indicator.set_title(text)
            except Exception:  # noqa: BLE001
                log.debug("could not update the tray tooltip", exc_info=True)

    def _tick(self) -> bool:
        self.update_status()
        return not self._closed

    def _start_ticking(self) -> None:
        try:
            self._source_id = self._GLib.timeout_add(STATUS_INTERVAL_MS, self._tick)
        except Exception:  # noqa: BLE001 - a live tooltip is a bonus, not a need
            log.debug("could not start the tray status timer", exc_info=True)

    # -- teardown -------------------------------------------------------------
    def shutdown(self) -> None:
        """Hide the icon.  Safe to call twice, never raises."""
        if self._closed:
            return
        self._closed = True
        if self._source_id is not None:
            try:
                self._GLib.source_remove(self._source_id)
            except Exception:  # noqa: BLE001
                pass
            self._source_id = None
        try:
            self._indicator.set_status(self._module.IndicatorStatus.PASSIVE)
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

    Never raises: every failure path (no GTK, no AppIndicator, no display, no
    session bus, a host that ignores us) ends in ``None`` so the caller keeps
    its default behaviour.
    """
    module = _indicator()
    if module is None:
        log.info("no tray support: GTK/AppIndicator is unavailable")
        return None
    icon = Path(icon_path)
    if not icon.exists():
        log.warning("tray icon missing at %s; not creating a tray", icon)
        return None

    try:
        from gi.repository import GLib, Gtk
    except Exception:  # noqa: BLE001
        log.info("no tray support: GTK is not importable")
        return None

    labels = tray_labels(locale_name)
    result: dict = {}
    done = threading.Event()

    def _build() -> bool:
        try:
            if result.get("tray") is not None:
                return False        # already built (a late idle callback)
            if not Gtk.init_check(None)[0]:
                log.info("no tray support: no usable display")
                return False
            menu = Gtk.Menu()
            for label, action in (
                (labels["show"], on_show),
                (labels["browser"], on_open_browser),
                (labels["quit"], on_quit),
            ):
                item = Gtk.MenuItem(label=label)
                item.connect("activate", lambda _w, fn=action: run_off_gui_thread(fn))
                menu.append(item)
            menu.show_all()

            indicator = module.Indicator.new(
                "pdf-ocr-embed", str(icon),
                module.IndicatorCategory.APPLICATION_STATUS)
            indicator.set_status(module.IndicatorStatus.ACTIVE)
            indicator.set_menu(menu)
            tray = Tray(indicator, module, title=title, labels=labels,
                        status_provider=status_provider)
            tray._start_ticking()
            result["tray"] = tray
            return False
        except Exception:  # noqa: BLE001 - any failure -> no tray
            log.warning("could not create the tray icon", exc_info=True)
            return False
        finally:
            done.set()

    try:
        GLib.idle_add(_build)      # must be created by the GTK main loop
    except Exception:  # noqa: BLE001
        log.warning("could not schedule tray creation", exc_info=True)
        return None
    if not done.wait(CREATE_TIMEOUT):
        # Nothing served the idle callback: there is no GTK main loop, so the
        # icon would exist without a working menu.  No tray is better than a
        # dead one (the caller then keeps "closing quits").
        log.warning("no GTK main loop answered within %.0fs; no tray",
                    CREATE_TIMEOUT)
        return None
    tray = result.get("tray")
    if tray is not None:
        log.info("system tray icon created")
    return tray


def notify(title: str, body: str = "") -> bool:
    """Best-effort desktop notification (used when the window hides).

    Returns False when libnotify/its typelib is not available — the caller must
    treat that as "no notification", never as an error.
    """
    try:
        import gi

        gi.require_version("Notify", "0.7")
        from gi.repository import Notify
    except Exception:  # noqa: BLE001
        return False
    try:
        if not Notify.is_initted():
            Notify.init(title or "PDF OCR Embed")
        Notify.Notification.new(title, body or "", None).show()
        return True
    except Exception:  # noqa: BLE001
        log.debug("desktop notification failed", exc_info=True)
        return False
