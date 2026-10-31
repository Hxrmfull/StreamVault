# -*- coding: utf-8 -*-
import os
import sys
import threading
import unittest
import uuid
from unittest import mock

from streamvault import util
from streamvault.events import EventBus
from streamvault.ui import autostart
from streamvault.ui.tray import Tray, TrayNotifier, draw_icon, status_text
from tests.helpers import wait_for


class FakeIcon:
    """Вместо pystray.Icon: запоминает, что с ним делали."""

    def __init__(self, name, icon, title, menu=None):
        self.icon = icon
        self.title = title
        self.menu = menu
        self.notes = []
        self.menu_updates = 0
        self.running = threading.Event()
        self.stopped = threading.Event()

    def run(self):
        self.running.set()
        self.stopped.wait(10)

    def stop(self):
        self.stopped.set()

    def update_menu(self):
        self.menu_updates += 1

    def notify(self, message, title):
        self.notes.append((title, message))


class TrayTest(unittest.TestCase):

    def test_icon_and_status_text(self):
        self.assertEqual(draw_icon().size, (64, 64))
        self.assertNotEqual(draw_icon(True).tobytes(), draw_icon(False).tobytes())
        self.assertEqual(status_text({"recording": [], "downloads": 0}), "Сейчас ничего не пишется")
        self.assertEqual(status_text({"recording": ["xqc", "a"], "downloads": 2}),
                         "Пишется: xqc, a · загрузок: 2")

    def test_status_reaches_icon(self):
        status = {"recording": [], "downloads": 0}
        icons = []
        tray = Tray(lambda: dict(status), on_open=lambda: None, on_quit=lambda: None,
                    on_check=lambda: None, on_folder=lambda which: None,
                    factory=lambda *a, **k: icons.append(FakeIcon(*a, **k)) or icons[-1])
        tray.start()
        try:
            icon = icons[0]
            self.assertTrue(icon.running.wait(5))
            idle = icon.icon
            status["recording"] = ["alinarinrin"]
            tray.refresh()
            self.assertIn("Пишется: alinarinrin", icon.title)
            self.assertIsNot(icon.icon, idle)
            self.assertGreater(icon.menu_updates, 0)
            self.assertLessEqual(len(icon.title), 127)
        finally:
            tray.stop()

    def test_menu_actions(self):
        calls = []
        icons = []
        tray = Tray(lambda: {}, on_open=lambda: calls.append("open"),
                    on_quit=lambda: calls.append("quit"), on_check=lambda: calls.append("check"),
                    on_folder=calls.append,
                    factory=lambda *a, **k: icons.append(FakeIcon(*a, **k)) or icons[-1])
        tray.start()
        try:
            items = [item for item in icons[0].menu.items if item.text]
            by_text = {str(item.text): item for item in items}
            for text in ("Открыть StreamVault", "Проверить каналы сейчас", "Папка записей", "Выход"):
                by_text[text](icons[0])
            self.assertEqual(calls, ["open", "check", "recordings", "quit"])
            self.assertTrue(by_text["Открыть StreamVault"].default)
        finally:
            tray.stop()


class NotifierTest(unittest.TestCase):

    def setUp(self):
        self.bus = EventBus()
        self.notes = []

        class Sink:
            def notify(sink, title, message):
                self.notes.append(title)

        self.hidden = True
        self.enabled = True
        self.notifier = TrayNotifier(self.bus, Sink(), enabled=lambda: self.enabled,
                                     hidden=lambda: self.hidden)

    def channel(self, state):
        self.bus.emit("channel", channel={"id": "twitch:x", "label": "x", "state": state,
                                          "stream": {"title": "эфир"}})

    def job(self, state):
        self.bus.emit("download", job={"id": "j", "state": state, "title": "клип"})

    def test_notifies_on_transitions_only(self):
        self.channel("offline")
        self.channel("recording")
        self.channel("recording")
        self.job("downloading")
        self.job("done")
        self.assertEqual(self.notes, ["Запись началась: x", "Скачано"])

    def test_stream_started_without_recording(self):
        self.channel("starting")
        self.channel("live")          # уже шёл при запуске программы — молчим
        self.channel("offline")
        self.channel("live")
        self.channel("live")
        self.assertEqual(self.notes, ["x в эфире"])

    def test_silent_with_window_open_or_disabled(self):
        self.hidden = False
        self.channel("recording")
        self.hidden, self.enabled = True, False
        self.job("downloading")
        self.job("failed")
        self.assertEqual(self.notes, [])


class FakeWinreg:
    """Раздел Run в памяти вместо реестра."""

    HKEY_CURRENT_USER = "HKCU"
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    def OpenKey(self, root, path, reserved=0, access=0):
        class Key:
            def __enter__(key):
                return key

            def __exit__(key, *exc):
                return False

        self.last_path = path
        return Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class AutostartTest(unittest.TestCase):

    def test_switch_on_and_off(self):
        registry = FakeWinreg()
        self.assertFalse(autostart.is_enabled(registry))
        autostart.set_enabled(True, registry)
        self.assertTrue(autostart.is_enabled(registry))
        self.assertEqual(registry.last_path, autostart.RUN_KEY)
        command = registry.values["StreamVault"]
        self.assertTrue(command.endswith('StreamVault.pyw" --tray'))
        self.assertTrue(os.path.exists(autostart.LAUNCHER))
        autostart.set_enabled(False, registry)
        autostart.set_enabled(False, registry)   # повторное выключение не ошибка
        self.assertFalse(autostart.is_enabled(registry))

    def test_exe_starts_itself(self):
        exe = os.path.join("C:\\", "Программы", "StreamVault", "StreamVault.exe")
        with mock.patch.object(sys, "frozen", True, create=True),                 mock.patch.object(sys, "executable", exe):
            self.assertEqual(autostart.command(), '"%s" --tray' % exe)


@unittest.skipUnless(os.name == "nt", "сигнал сделан на событиях Windows")
class ShowSignalTest(unittest.TestCase):

    def test_second_launch_wakes_first(self):
        name = "StreamVaultTest-" + uuid.uuid4().hex
        self.assertFalse(util.ShowSignal(name).fire())   # первой копии ещё нет
        shown = []
        util.ShowSignal(name).listen(lambda: shown.append(True))
        self.assertTrue(util.ShowSignal(name).fire())
        self.assertTrue(wait_for(lambda: shown == [True], timeout=3))
        self.assertTrue(util.ShowSignal(name).fire())
        self.assertTrue(wait_for(lambda: len(shown) == 2, timeout=3))


if __name__ == "__main__":
    unittest.main()
