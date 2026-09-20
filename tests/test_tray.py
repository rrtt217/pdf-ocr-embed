"""System tray (background running): labels, status text, and real registration.

The valuable part of a tray for this app is *background running*: the OCR phase
lives in the server process, so once the window can be hidden safely the job
keeps going.  These tests cover the two halves that must not drift:

* the pure presentation bits (localized labels, the "still running" notice, the
  tooltip built from the job list) — no GTK needed;
* the real thing: an indicator that the desktop's StatusNotifier host actually
  accepts (Linux, skipped elsewhere / without a session bus).  That test briefly
  puts an icon in the tray of the machine running the suite; it is skipped when
  there is no display or no tray host to talk to.

The other half — "closing the window hides instead of quitting, but only when a
tray exists" — is pinned in ``tests/test_desktop.py``.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from backend import tray as tray_mod

ICON = Path(__file__).resolve().parents[1] / "frontend" / "tray.png"


# --- pure presentation -------------------------------------------------------

def test_labels_follow_the_locale():
    assert tray_mod.tray_labels("zh_CN.UTF-8")["quit"] == "退出"
    assert tray_mod.tray_labels("zh")["show"] == "显示主窗口"
    assert tray_mod.tray_labels("en_US.UTF-8")["quit"] == "Quit"
    # Anything unknown falls back to English rather than failing.
    assert tray_mod.tray_labels("fr_FR")["quit"] == "Quit"
    assert tray_mod.tray_labels("")["quit"] == "Quit"


def test_hidden_notice_is_localized():
    assert tray_mod.hidden_notice("zh_CN")[0] == "仍在后台运行"
    assert tray_mod.hidden_notice("en_US")[0] == "Still running"
    assert "托盘" in tray_mod.hidden_notice("zh_CN")[1]


def test_status_text_reports_progress_and_idleness():
    idle = tray_mod.status_text([], "zh_CN")
    assert idle == "PDF OCR Embed · 空闲"

    # Finished jobs are not "running": the tooltip must not claim activity.
    assert tray_mod.status_text(
        [{"status": "done", "pages_done": 224, "num_pages": 224}], "zh_CN"
    ) == idle
    assert tray_mod.status_text(
        [{"status": "error", "pages_done": 1, "num_pages": 4}], "zh_CN"
    ) == idle

    one = tray_mod.status_text(
        [{"status": "running", "pages_done": 187, "num_pages": 224}], "zh_CN")
    assert one == "PDF OCR Embed · 第 187/224 页"

    many = tray_mod.status_text([
        {"status": "running", "pages_done": 10, "num_pages": 20},
        {"status": "uploaded", "current": 0, "total": 5},
    ], "en_US")
    assert many == "PDF OCR Embed · 2 jobs · page 10/20"

    # The pre-rebuild aliases (current/total) are accepted too.
    assert "10/20" in tray_mod.status_text(
        [{"status": "retrying", "current": 10, "total": 20}], "en")


def test_status_text_never_raises_on_odd_jobs():
    assert tray_mod.status_text(None, "en") == "PDF OCR Embed · idle"
    assert "0/?" in tray_mod.status_text([{"status": "running"}], "en")


# --- degradation: no tray must mean None, never an exception -----------------

def test_create_tray_returns_none_without_a_backend(monkeypatch):
    monkeypatch.setattr(tray_mod, "_indicator", lambda: None)
    assert tray_mod.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_create_tray_returns_none_without_an_icon(monkeypatch, tmp_path):
    """A missing icon asset must not produce a broken icon in the tray."""
    monkeypatch.setattr(tray_mod, "_indicator", lambda: object())
    assert tray_mod.create_tray(
        title="t", icon_path=tmp_path / "nope.png", on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_available_is_a_bool():
    assert isinstance(tray_mod.available(), bool)


def test_notify_failure_is_not_an_error():
    """A desktop without libnotify must just mean "no notification"."""
    assert tray_mod.notify("title", "body") in (True, False)


# --- the real thing: does the desktop host accept our icon? ------------------

def _tray_environment() -> tuple[bool, str]:
    """(usable, reason) for a live tray test: GTK + a display + a tray host."""
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gio, GLib, Gtk
    except Exception as exc:  # noqa: BLE001
        return False, f"no PyGObject/GTK: {exc}"
    if not Gtk.init_check(None)[0]:
        return False, "no display"
    if tray_mod._indicator() is None:
        return False, "no AppIndicator library"
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                            "org.freedesktop.DBus.Properties", "GetAll",
                            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
                            GLib.VariantType("(a{sv})"), 0, -1, None)
        if not res.unpack()[0].get("IsStatusNotifierHostRegistered"):
            return False, "the tray host is not registered"
    except Exception as exc:  # noqa: BLE001
        return False, f"no tray host on the session bus: {exc}"
    return True, ""


_USABLE, _REASON = _tray_environment()


@pytest.mark.skipif(not _USABLE, reason=_REASON or "no tray environment")
def test_tray_registers_with_the_desktop_host():
    """End-to-end: the host really lists our item while it is alive.

    Mirrors how ``desktop.py`` does it — the icon is created from a worker
    thread while a GLib main loop serves the GUI thread — so this also covers
    the threading contract that makes the tray work at all.

    Note: this briefly shows a tray icon on the machine running the suite.
    """
    from gi.repository import Gio, GLib, Gtk

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def registered() -> set:
        res = bus.call_sync(
            "org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
            "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType("(a{sv})"), 0, -1, None)
        return set(res.unpack()[0].get("RegisteredStatusNotifierItems") or [])

    before = registered()
    outcome: dict = {}
    seen: dict = {}

    def create() -> None:
        outcome["tray"] = tray_mod.create_tray(
            title="PDF OCR Embed (test)", icon_path=ICON,
            on_show=lambda: None, on_open_browser=lambda: None,
            on_quit=lambda: None,
            status_provider=lambda: "PDF OCR Embed · test",
        )

    worker = threading.Thread(target=create, name="tray-test", daemon=True)
    worker.start()

    def check() -> bool:
        seen["items"] = registered()
        Gtk.main_quit()
        return False

    GLib.timeout_add(2500, check)
    Gtk.main()
    worker.join(5)

    assert not worker.is_alive()
    assert outcome.get("tray") is not None, "the tray could not be created"
    assert seen["items"] - before, (
        "the tray host did not register our item: "
        f"before={sorted(before)} during={sorted(seen['items'])}")
    outcome["tray"].shutdown()


def test_indicator_falls_back_to_the_old_namespace(monkeypatch):
    """Ayatana first, AppIndicator3 second — a distro may ship only one."""
    gi = pytest.importorskip("gi")
    real_require = gi.require_version

    def fake(name, version=None):
        if name == "AyatanaAppIndicator3":
            raise ValueError("simulated: only the old namespace is installed")
        return real_require(name, version)

    monkeypatch.setattr(gi, "require_version", fake)
    module = tray_mod._indicator()
    assert module is not None
    assert module.__name__.endswith("AppIndicator3")


def test_indicator_returns_none_without_gtk(monkeypatch):
    """No GTK at all (a headless server install): no tray, no exception."""
    import sys

    monkeypatch.setitem(sys.modules, "gi", None)
    assert tray_mod._indicator() is None
    assert tray_mod.available() is False


def test_tray_pushes_status_and_retires_cleanly():
    """The tooltip must actually reach the indicator, and shutdown must not
    raise on a half-dead indicator (it runs during a bounded quit)."""
    pytest.importorskip("gi")

    class _FakeIndicator:
        def __init__(self):
            self.titles: list = []
            self.statuses: list = []

        def set_title(self, text):
            self.titles.append(text)

        def set_status(self, value):
            self.statuses.append(value)

    class _FakeModule:
        class IndicatorStatus:
            PASSIVE = 0
            ACTIVE = 1

    indicator = _FakeIndicator()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return f"status {calls['n']}"

    tray = tray_mod.Tray(indicator, _FakeModule, title="PDF OCR Embed",
                         labels=tray_mod.tray_labels("en"),
                         status_provider=provider)
    # The app name is the tooltip until the first status arrives, then the
    # provider's text wins (it already carries the app name).
    assert indicator.titles[0] == "PDF OCR Embed"
    assert indicator.titles[-1] == "status 1"
    tray.update_status()
    assert indicator.titles[-1] == "status 2"
    tray.shutdown()
    assert indicator.statuses[-1] == _FakeModule.IndicatorStatus.PASSIVE

    # Idempotent, and silent once closed.
    tray.shutdown()
    tray.update_status()
    assert indicator.titles[-1] == "status 2"


def test_tray_survives_a_failing_status_provider():
    pytest.importorskip("gi")

    class _FakeIndicator:
        def set_title(self, text): ...
        def set_status(self, value): ...

    class _FakeModule:
        class IndicatorStatus:
            PASSIVE = 0

    def boom():
        raise RuntimeError("registry gone")

    tray = tray_mod.Tray(_FakeIndicator(), _FakeModule, title="t", labels={},
                         status_provider=boom)
    tray.update_status()      # must not raise
    tray.shutdown()
