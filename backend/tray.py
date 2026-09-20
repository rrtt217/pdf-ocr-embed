"""System tray for the desktop build — the platform-agnostic facade.

The desktop app is a local server plus a window, and the OCR phase runs in the
*server* process on daemon threads — so "keep working in the background" only
needs two things a window cannot provide: **closing the window must not quit**,
and **there must be a way back**.  This module supplies the second half on all
three desktop platforms, through per-platform backends:

``backend.tray_gtk``  Linux — AppIndicator (Ayatana first, old namespace
                      fallback) over the same GTK stack ``requirements-desktop.txt``
                      installs for the window.
``backend.tray_win``  Windows — pystray, which drives ``Shell_NotifyIcon`` from
                      its own message-loop thread (``Icon.run_detached``).
``backend.tray_mac``  macOS — pyobjc ``NSStatusItem`` hanging off the
                      ``NSApplication`` loop pywebview already runs.

Why not pywebview's own tray: pywebview 6.2.1 has no tray API at all (no
``Tray``, no ``tray=`` on ``webview.start``, no tray code in the package).

**Everything here is best-effort.**  A missing library, a desktop without a
tray host (e.g. GNOME without the AppIndicator extension) or a broken session
bus must leave the app behaving *exactly* as before: ``create_tray`` returns
``None`` and ``desktop`` keeps "closing the window quits".  Never the other way
round — a hidden window with no tray would be unreachable.

Threading contract shared by every backend: the icon is created on the
platform's GUI thread (marshalled there and waited for), menu actions run on
their own daemon threads (:func:`run_off_gui_thread`), and the window calls
they make are pywebview's marshalled wrappers — so nothing blocks or touches
the GUI from the wrong thread.
"""
from __future__ import annotations

import logging
import sys
import threading
import weakref
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

#: Menu labels.  The WebUI is bilingual; the tray is native, so it follows the
#: process locale (``locale.getlocale()`` — not an environment variable).
_LABELS = {
    "en": {"show": "Show window", "browser": "Open in browser", "quit": "Quit"},
    "zh": {"show": "显示主窗口", "browser": "在浏览器中打开", "quit": "退出"},
}

#: Tooltip text.  Live statuses mirror ``ocr_service``'s notion of "a run is
#: going on"; formatting lives here so it can be unit-tested.
_STATUS = {
    "en": {"idle": "idle", "one": "page {done}/{total}",
           "many": "{n} jobs · page {done}/{total}"},
    "zh": {"idle": "空闲", "one": "第 {done}/{total} 页",
           "many": "{n} 个任务 · 第 {done}/{total} 页"},
}

_APP_NAME = "PDF OCR Embed"
_LIVE_STATUSES = ("uploaded", "running", "retrying")


def _language(locale_name: Optional[str]) -> str:
    if locale_name is None:
        import locale as _locale

        try:
            locale_name = _locale.getlocale()[0] or ""
        except Exception:  # noqa: BLE001 - a weird locale must not break the tray
            locale_name = ""
    lang = (locale_name or "").split(".")[0].split("_")[0].lower()
    return lang if lang in _LABELS else "en"


def tray_labels(locale_name: Optional[str] = None) -> dict:
    """Menu labels for a locale name (``zh_CN``/``en_US``/...)."""
    return dict(_LABELS[_language(locale_name)])


_NOTICE = {
    "en": ("Still running",
           "The window is hidden but the app keeps working. "
           "Click the tray icon to reopen it."),
    "zh": ("仍在后台运行",
           "窗口已隐藏，任务会继续执行。点击托盘图标可以重新打开窗口。"),
}


def hidden_notice(locale_name: Optional[str] = None):
    """``(title, body)`` for the one-off "it is still running" notification."""
    return _NOTICE[_language(locale_name)]


def status_text(jobs, locale_name: Optional[str] = None) -> str:
    """Tooltip text for the current job list (pure: no I/O, easy to test).

    This is the point of a tray for this app: the OCR phase keeps running in
    the server after the window is gone, so the tooltip is where "is it still
    working?" gets answered without opening anything.
    """
    text = _STATUS[_language(locale_name)]
    live = [j for j in (jobs or []) if (j.get("status") or "") in _LIVE_STATUSES]
    if not live:
        return f"{_APP_NAME} · {text['idle']}"
    job = live[0]
    done = job.get("pages_done", job.get("current", 0)) or 0
    total = job.get("num_pages", job.get("total", 0)) or 0
    key = "one" if len(live) == 1 else "many"
    detail = text[key].format(done=done, total=total or "?", n=len(live))
    return f"{_APP_NAME} · {detail}"


def run_off_gui_thread(action: Callable[[], None]) -> None:
    """Run a menu action on a daemon thread.

    The GUI main loop must stay responsive, and a quit waits (briefly) for the
    jobs to stop, so nothing a menu does may block the loop.
    """
    def runner() -> None:
        try:
            action()
        except Exception:  # noqa: BLE001 - the tray must never crash the app
            log.exception("tray action failed")

    threading.Thread(target=runner, name="tray-action", daemon=True).start()


# --- platform dispatch --------------------------------------------------------

#: The tray created most recently, as a weak reference: the hide-notice
#: notification routes through it on platforms without libnotify (Windows).
#: A weak reference because the owner (``desktop``) calls ``shutdown()`` on it;
#: a dead one must never be used again.
_last_tray: Optional["weakref.ReferenceType"] = None


def _backend():
    """The platform's tray backend module (imported lazily)."""
    if sys.platform == "darwin":
        from backend import tray_mac

        return tray_mac
    if sys.platform.startswith("win"):
        from backend import tray_win

        return tray_win
    from backend import tray_gtk

    return tray_gtk


def create_tray(
    *,
    title: str,
    icon_path: Path,
    on_show: Callable[[], None],
    on_open_browser: Callable[[], None],
    on_quit: Callable[[], None],
    status_provider: Optional[Callable[[], str]] = None,
    locale_name: Optional[str] = None,
):
    """Create the tray icon, or return None when it cannot be done.

    Never raises: every failure path (no backend, no display, a host that
    ignores us) ends in ``None`` so the caller keeps its default behaviour.
    """
    global _last_tray
    try:
        backend = _backend()
    except Exception:  # noqa: BLE001 - no tray may never break the app
        log.warning("could not load the tray backend", exc_info=True)
        return None
    try:
        tray = backend.create_tray(
            title=title, icon_path=icon_path, on_show=on_show,
            on_open_browser=on_open_browser, on_quit=on_quit,
            status_provider=status_provider, locale_name=locale_name)
    except Exception:  # noqa: BLE001
        log.warning("the tray backend failed; continuing without a tray",
                    exc_info=True)
        return None
    if tray is not None:
        try:
            _last_tray = weakref.ref(tray)
        except TypeError:  # pragma: no cover - every Tray is weakref-able
            _last_tray = None
    return tray


def available() -> bool:
    """True when a tray icon can probably be created here."""
    try:
        return bool(_backend().available())
    except Exception:  # noqa: BLE001
        return False


def notify(title: str, body: str = "") -> bool:
    """Best-effort desktop notification (used when the window hides).

    Returns False when the platform has no usable channel — the caller must
    treat that as "no notification", never as an error.
    """
    if sys.platform == "darwin":
        from backend import tray_mac

        return tray_mac.notify(title, body)
    if sys.platform.startswith("win"):
        tray = _last_tray() if _last_tray is not None else None
        if tray is not None:
            try:
                return bool(tray.notify(title, body))
            except Exception:  # noqa: BLE001
                log.debug("tray notification failed", exc_info=True)
        return False
    from backend import tray_gtk

    return tray_gtk.notify(title, body)
