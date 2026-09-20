"""System tray (background running): labels, status text, and real registration.

The valuable part of a tray for this app is *background running*: the OCR phase
lives in the server process, so once the window can be hidden safely the job
keeps going.  These tests cover the halves that must not drift:

* the pure presentation bits (localized labels, the "still running" notice, the
  tooltip built from the job list) — no GUI toolkit needed;
* the platform dispatch in ``backend.tray`` and the wiring contract of the
  Windows (pystray) and macOS (NSStatusItem) backends, faked because those
  toolkits cannot load here;
* the real thing on Linux: an indicator that the desktop's StatusNotifier host
  actually accepts (skipped elsewhere / without a session bus).  That test
  briefly puts an icon in the tray of the machine running the suite.

The other half — "closing the window hides instead of quitting, but only when a
tray exists" — is pinned in ``tests/test_desktop.py``.
"""
from __future__ import annotations

import json
import sys
import threading
import types
import weakref
from pathlib import Path

import pytest

from backend import tray as tray_mod
from backend import tray_gtk

ICON = Path(__file__).resolve().parents[1] / "frontend" / "tray.png"


@pytest.fixture(autouse=True)
def _reset_tray_state():
    """Module state (the weakref'd tray used for the Windows hide notice)
    must not leak between tests."""
    tray_mod._last_tray = None
    yield
    tray_mod._last_tray = None


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


# --- platform dispatch -------------------------------------------------------

def test_backend_dispatch_follows_the_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert tray_mod._backend().__name__.endswith("tray_mac")
    monkeypatch.setattr(sys, "platform", "win32")
    assert tray_mod._backend().__name__.endswith("tray_win")
    monkeypatch.setattr(sys, "platform", "linux")
    assert tray_mod._backend().__name__.endswith("tray_gtk")


def test_create_tray_dispatches_to_the_platform_backend(monkeypatch):
    calls = []

    class _Tray:
        pass

    tray = _Tray()                  # weakref-able, like a real Tray

    class _StubBackend:
        @staticmethod
        def create_tray(**kwargs):
            calls.append(kwargs)
            return tray

    monkeypatch.setattr(tray_mod, "_backend", lambda: _StubBackend())
    assert tray_mod.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is tray
    assert calls and calls[0]["title"] == "t"
    # The created tray is remembered: on Windows the hide-notice notification
    # routes through it (there is no libnotify to fall back on).
    assert tray_mod._last_tray is not None
    assert tray_mod._last_tray() is tray


def test_create_tray_returns_none_when_the_backend_raises(monkeypatch):
    class _BoomBackend:
        @staticmethod
        def create_tray(**kwargs):
            raise RuntimeError("no display")

    monkeypatch.setattr(tray_mod, "_backend", lambda: _BoomBackend())
    assert tray_mod.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None
    assert tray_mod._last_tray is None


def test_windows_notify_goes_through_the_live_tray(monkeypatch):
    calls = []

    class _FakeTray:
        def notify(self, title, body=""):
            calls.append((title, body))
            return True

    fake = _FakeTray()
    tray_mod._last_tray = weakref.ref(fake)
    monkeypatch.setattr(sys, "platform", "win32")
    try:
        assert tray_mod.notify("Still running", "body") is True
        assert calls == [("Still running", "body")]
    finally:
        tray_mod._last_tray = None


def test_windows_notify_without_a_tray_is_silently_skipped(monkeypatch):
    tray_mod._last_tray = None
    monkeypatch.setattr(sys, "platform", "win32")
    assert tray_mod.notify("Still running", "body") is False


def test_notify_failure_is_not_an_error():
    """A desktop without any notification channel must just mean "no notice"."""
    assert tray_mod.notify("title", "body") in (True, False)


def test_available_is_a_bool():
    assert isinstance(tray_mod.available(), bool)


# --- degradation: no tray must mean None, never an exception -----------------

def test_create_tray_returns_none_without_a_backend(monkeypatch):
    monkeypatch.setattr(tray_gtk, "_indicator", lambda: None)
    assert tray_mod.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_create_tray_returns_none_without_an_icon(monkeypatch, tmp_path):
    """A missing icon asset must not produce a broken icon in the tray."""
    monkeypatch.setattr(tray_gtk, "_indicator", lambda: object())
    assert tray_mod.create_tray(
        title="t", icon_path=tmp_path / "nope.png", on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


# --- the real thing on Linux: does the desktop host accept our icon? ---------

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
    if tray_gtk._indicator() is None:
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
    module = tray_gtk._indicator()
    assert module is not None
    assert module.__name__.endswith("AppIndicator3")


def test_indicator_returns_none_without_gtk(monkeypatch):
    """No GTK at all (a headless server install): no tray, no exception."""
    monkeypatch.setitem(sys.modules, "gi", None)
    assert tray_gtk._indicator() is None
    assert tray_gtk.available() is False


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

    tray = tray_gtk.Tray(indicator, _FakeModule, title="PDF OCR Embed",
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

    tray = tray_gtk.Tray(_FakeIndicator(), _FakeModule, title="t", labels={},
                         status_provider=boom)
    tray.update_status()      # must not raise
    tray.shutdown()


# --- Windows backend (pystray), faked: the wiring contract --------------------

class _FakeWinIcon:
    """The pystray.Icon surface tray_win uses, with every call recorded."""

    def __init__(self, name, icon=None, title=None, menu=None):
        self.name = name
        self.icon = icon
        self.title = title or ""
        self.menu = menu
        self.visible = None
        self.setup = None
        self.stopped = False
        self.notifies: list = []

    def run_detached(self, setup=None):
        self.setup = setup
        if setup is not None:
            setup(self)             # the message window is up "immediately"

    def stop(self):
        self.stopped = True

    def notify(self, message, title=None):
        self.notifies.append((title, message))


class _FakeWinModule:
    """A stand-in for the pystray module (same names tray_win touches)."""

    Icon = _FakeWinIcon

    @staticmethod
    def Menu(*items):
        return list(items)

    @staticmethod
    def MenuItem(text, action, **kwargs):
        return {"text": text, "action": action, **kwargs}


def _win_module(icon_cls=_FakeWinIcon) -> types.ModuleType:
    module = types.ModuleType("pystray")
    module.Icon = icon_cls
    module.Menu = _FakeWinModule.Menu
    module.MenuItem = _FakeWinModule.MenuItem
    return module


def test_win_tray_builds_the_menu_and_waits_for_ready(monkeypatch):
    from backend import tray_win

    monkeypatch.setitem(sys.modules, "pystray", _win_module())
    shown = threading.Event()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return f"status {calls['n']}"

    tray = tray_win.create_tray(
        title="PDF OCR Embed", icon_path=ICON, on_show=lambda: shown.set(),
        on_open_browser=lambda: None, on_quit=lambda: None,
        status_provider=provider, locale_name="en")
    assert tray is not None
    icon = tray._icon
    assert isinstance(icon, _FakeWinIcon)
    # Three menu entries in a stable order; the first is the click action.
    menu = icon.menu
    assert [i["text"] for i in menu] == [
        "Show window", "Open in browser", "Quit"]
    assert menu[0]["default"] is True
    # A custom setup MUST show the icon itself (pystray's default only does
    # that when no setup is passed).
    assert icon.visible is True
    # A menu entry really runs its callback, off the pystray loop thread.
    menu[0]["action"]()
    assert shown.wait(2)
    # The provider drives the tooltip.
    tray.update_status()
    assert icon.title == "status 2"
    tray.shutdown()
    assert icon.stopped is True
    # Idempotent, and silent once closed.
    tray.shutdown()
    calls["n"] = 99
    tray.update_status()
    assert icon.title == "status 2"


def test_win_tray_returns_none_when_the_loop_never_comes_up(monkeypatch):
    from backend import tray_win

    class _DeadIcon(_FakeWinIcon):
        def run_detached(self, setup=None):
            pass                    # the loop thread dies before setup runs

    monkeypatch.setitem(sys.modules, "pystray", _win_module(_DeadIcon))
    monkeypatch.setattr(tray_win, "CREATE_TIMEOUT", 0.05)
    assert tray_win.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_win_tray_returns_none_when_pystray_raises(monkeypatch):
    from backend import tray_win

    class _BoomIcon(_FakeWinIcon):
        def run_detached(self, setup=None):
            raise RuntimeError("no message loop")

    monkeypatch.setitem(sys.modules, "pystray", _win_module(_BoomIcon))
    assert tray_win.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_win_tray_returns_none_without_pystray(monkeypatch):
    from backend import tray_win

    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray_win.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_win_tray_notify_goes_through_the_balloon():
    from backend import tray_win

    icon = _FakeWinIcon("pdf-ocr-embed")
    tray = tray_win.Tray(icon, title="t")
    assert tray.notify("Still running", "body") is True
    assert icon.notifies == [("Still running", "body")]
    tray.shutdown()
    assert tray.notify("a", "b") is False       # silent once closed


# --- macOS backend (NSStatusItem), faked: the wiring contract -----------------

class _FakeNSObject:
    """Just enough of pyobjc's NSObject for tray_mac's target subclass."""

    def init(self):
        return self

    @classmethod
    def alloc(cls):
        return cls.__new__(cls)


class _FakeButton:
    def __init__(self):
        self.image = None
        self.titles: list = []
        self.tooltips: list = []

    def setImage_(self, image):
        self.image = image

    def setTitle_(self, text):
        self.titles.append(text)

    def setToolTip_(self, text):
        self.tooltips.append(text)


class _FakeStatusItem:
    def __init__(self):
        self.menu = None
        self.length = None
        self._button = _FakeButton()

    def setMenu_(self, menu):
        self.menu = menu

    def button(self):
        return self._button


class _FakeStatusBar:
    def __init__(self):
        self.items: list = []

    def statusItemWithLength_(self, length):
        item = _FakeStatusItem()
        item.length = length
        self.items.append(item)
        return item

    def removeStatusItem_(self, item):
        if item in self.items:
            self.items.remove(item)


class _FakeMenu:
    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def initWithTitle_(self, title):
        self.title = title
        self.items: list = []
        return self

    def addItem_(self, item):
        self.items.append(item)


class _FakeNSMenuItem:
    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def initWithTitle_action_keyEquivalent_(self, title, action, key):
        self.title = title
        self.action = action
        self.target = None
        return self

    def setTarget_(self, target):
        self.target = target


class _FakeImage:
    @classmethod
    def alloc(cls):
        return cls.__new__(cls)

    def initWithContentsOfFile_(self, path):
        self.path = path
        self.size = None
        return self

    def setSize_(self, size):
        self.size = size


def _mac_modules(app_helper=None):
    """(AppKit, Foundation, PyObjCTools, AppHelper) fakes + the shared bar."""
    bar = _FakeStatusBar()

    class NSStatusBar:
        @classmethod
        def systemStatusBar(cls):
            return bar

    appkit = types.ModuleType("AppKit")
    appkit.NSStatusBar = NSStatusBar
    appkit.NSMenu = _FakeMenu
    appkit.NSMenuItem = _FakeNSMenuItem
    appkit.NSImage = _FakeImage
    appkit.NSVariableStatusItemLength = -1

    foundation = types.ModuleType("Foundation")
    foundation.NSObject = _FakeNSObject

    pyobjctools = types.ModuleType("PyObjCTools")
    helper = types.ModuleType("PyObjCTools.AppHelper")
    helper.callAfter = app_helper or (lambda function, *a, **k: function(*a, **k))
    pyobjctools.AppHelper = helper
    return appkit, foundation, pyobjctools, helper


def _install_mac_fakes(monkeypatch, app_helper=None):
    appkit, foundation, pyobjctools, helper = _mac_modules(app_helper)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "PyObjCTools", pyobjctools)
    monkeypatch.setitem(sys.modules, "PyObjCTools.AppHelper", helper)


def test_mac_tray_builds_the_status_item_and_wires_the_menu(monkeypatch):
    from backend import tray_mac

    _install_mac_fakes(monkeypatch)
    shown = threading.Event()
    calls = {"n": 0}
    bar_holder: list = []

    def provider():
        calls["n"] += 1
        return f"status {calls['n']}"

    tray = tray_mac.create_tray(
        title="PDF OCR Embed", icon_path=ICON, on_show=lambda: shown.set(),
        on_open_browser=lambda: None, on_quit=lambda: None,
        status_provider=provider, locale_name="en")
    assert tray is not None
    item = tray._status_item
    bar_holder.append(item)
    # Three menu entries, stable order, with pyobjc selector names.
    menu = item.menu
    assert [i.title for i in menu.items] == [
        "Show window", "Open in browser", "Quit"]
    assert [i.action for i in menu.items] == [
        "showWindow:", "openBrowser:", "quitApp:"]
    # AppKit does NOT retain NSMenuItem.target — the Tray keeps the reference.
    assert tray._target is menu.items[0].target
    # A menu action really runs its callback, off the main thread.
    menu.items[0].target.showWindow_(None)
    assert shown.wait(2)
    # The icon image got attached and sized for the menu bar.
    button = item.button()
    assert button.image is not None
    assert button.image.size == (18, 18)
    # The provider drives the title.
    tray.update_status()
    assert button.titles[-1] == "status 2"
    # Shutdown removes the status item from the status bar.
    tray.shutdown()
    from AppKit import NSStatusBar

    assert NSStatusBar.systemStatusBar().items == []
    assert bar_holder


def test_mac_tray_returns_none_without_pyobjc(monkeypatch):
    from backend import tray_mac

    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert tray_mac.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_mac_tray_returns_none_without_an_icon(monkeypatch, tmp_path):
    from backend import tray_mac

    _install_mac_fakes(monkeypatch)
    assert tray_mac.create_tray(
        title="t", icon_path=tmp_path / "nope.png", on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_mac_tray_returns_none_when_the_main_thread_never_answers(monkeypatch):
    from backend import tray_mac

    def silent(function, *args, **kwargs):
        return None                 # the call never runs

    _install_mac_fakes(monkeypatch, app_helper=silent)
    monkeypatch.setattr(tray_mac, "CREATE_TIMEOUT", 0.05)
    assert tray_mac.create_tray(
        title="t", icon_path=ICON, on_show=lambda: None,
        on_open_browser=lambda: None, on_quit=lambda: None) is None


def test_mac_notify_uses_osascript(monkeypatch):
    from backend import tray_mac

    seen: dict = {}

    class _Proc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _Proc()

    monkeypatch.setattr(tray_mac.subprocess, "run", fake_run)
    assert tray_mac.notify("Still running", 'body with "quotes"') is True
    assert seen["cmd"][0] == "osascript"
    script = seen["cmd"][2]
    assert 'display notification' in script
    # The strings are AppleScript-escaped, not interpolated raw.
    assert 'body with \\"quotes\\"' in script

    class _Fail:
        returncode = 1

    monkeypatch.setattr(tray_mac.subprocess, "run", lambda *a, **k: _Fail())
    assert tray_mac.notify("t", "b") is False


def test_mac_notify_swallows_a_missing_osascript(monkeypatch):
    from backend import tray_mac

    def boom(*args, **kwargs):
        raise FileNotFoundError("no osascript")

    monkeypatch.setattr(tray_mac.subprocess, "run", boom)
    assert tray_mac.notify("t", "b") is False
