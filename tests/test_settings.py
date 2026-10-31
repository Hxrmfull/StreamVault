# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import sys
import unittest
from unittest import mock

from streamvault import settings as settings_module
from streamvault.settings import (DEFAULTS, Settings, SettingsError,
                                  import_legacy_config)


class SettingsTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text, encoding="utf-8"):
        with open(self.path, "w", encoding=encoding) as target:
            target.write(text)

    def test_missing_file_gives_defaults(self):
        settings = Settings.load(self.path)
        self.assertEqual(settings.as_dict(), DEFAULTS)
        self.assertFalse(os.path.exists(self.path))

    def test_bad_values_fall_back(self):
        self.write(json.dumps({"probe_timeout": "abc", "quality": 5, "refresh": 30,
                               "channels": "kick:xqc", "drain_grace": True}))
        with self.assertLogs(level="WARNING"):
            settings = Settings.load(self.path)
        self.assertEqual(settings["probe_timeout"], 180)
        self.assertEqual(settings["quality"], "best")
        self.assertEqual(settings["drain_grace"], 900)
        # одно число на все платформы — как в первой версии рекордера
        self.assertEqual(set(settings["refresh"].values()), {30})
        self.assertEqual(settings["channels"],
                         [{"platform": "kick", "channel": "xqc", "enabled": False}])

    def test_flags(self):
        # было бы: True — тоже целое число, и проверка для int отвергала бы да/нет
        self.write(json.dumps({"close_to_tray": False, "tray_notifications": 1}))
        with self.assertLogs(level="WARNING"):
            settings = Settings.load(self.path)
        self.assertIs(settings["close_to_tray"], False)
        self.assertIs(settings["tray_notifications"], True)

    def test_legacy_names_and_unknown_keys(self):
        self.write(json.dumps({"client_id": "id", "my_note": "оставь"}))
        with self.assertLogs(level="WARNING"):
            settings = Settings.load(self.path)
        self.assertEqual(settings["twitch_client_id"], "id")
        self.assertEqual(settings["my_note"], "оставь")

    def test_bom_from_notepad(self):
        self.write(json.dumps({"quality": "720p"}), encoding="utf-8-sig")
        self.assertEqual(Settings.load(self.path)["quality"], "720p")

    def test_broken_json(self):
        self.write("{ не json")
        with self.assertRaises(SettingsError):
            Settings.load(self.path)

    def test_update_saves_atomically(self):
        settings = Settings.load(self.path)
        settings.update(quality="1080p60", channels=["kick:xqc"])
        self.assertEqual(sorted(os.listdir(self.dir)), ["settings.json"])
        again = Settings.load(self.path)
        self.assertEqual(again["quality"], "1080p60")
        self.assertEqual(again["channels"][0]["channel"], "xqc")

    def test_returned_values_are_copies(self):
        settings = Settings.load(self.path)
        settings["refresh"]["twitch"] = 1
        self.assertEqual(settings["refresh"]["twitch"], 15)

    def test_import_legacy_config(self):
        legacy = os.path.join(self.dir, "config.py")
        with open(legacy, "w", encoding="utf-8") as target:
            target.write('root_path = r"D:\\Streams"\n'
                         'channels = {"twitch": ["LegendaAFK", "witchbritch"], '
                         '"kick": "xqc", "youtube": []}\n'
                         'client_id = "abc"\nclient_secret = "def"\n'
                         'refresh = 20\nquality = "best"\n')
        settings = import_legacy_config(legacy, self.path)
        self.assertEqual(settings["root_path"], r"D:\Streams")
        self.assertEqual([c["channel"] for c in settings["channels"]],
                         ["legendaafk", "witchbritch", "xqc"])
        self.assertEqual(settings["twitch_client_secret"], "def")
        self.assertEqual(settings["refresh"]["kick"], 20)
        self.assertTrue(os.path.exists(self.path))


class AppDirTest(unittest.TestCase):
    """Где exe хранит настройки: рядом с собой, а если там нельзя писать — в APPDATA."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.exe = os.path.join(self.dir, "StreamVault", "StreamVault.exe")
        os.makedirs(os.path.dirname(self.exe))
        self.appdata = os.path.join(self.dir, "AppData")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def app_dir(self, writable=True):
        with mock.patch.object(sys, "frozen", True, create=True),                 mock.patch.object(sys, "executable", self.exe),                 mock.patch.dict(os.environ, {"APPDATA": self.appdata}),                 mock.patch.object(settings_module, "_writable", return_value=writable):
            return settings_module.app_dir()

    def test_from_sources_project_folder(self):
        project = os.path.dirname(os.path.dirname(os.path.abspath(settings_module.__file__)))
        self.assertEqual(settings_module.app_dir(), project)

    def test_exe_keeps_data_next_to_itself(self):
        self.assertEqual(self.app_dir(), os.path.dirname(self.exe))

    def test_read_only_folder_uses_appdata(self):
        self.assertEqual(self.app_dir(writable=False), os.path.join(self.appdata, "StreamVault"))

    def test_existing_settings_stay_where_they_are(self):
        open(os.path.join(os.path.dirname(self.exe), "settings.json"), "w").close()
        self.assertEqual(self.app_dir(writable=False), os.path.dirname(self.exe))

    def test_writable_check(self):
        self.assertTrue(settings_module._writable(self.dir))
        self.assertFalse(settings_module._writable(os.path.join(self.dir, "нет такой")))


if __name__ == "__main__":
    unittest.main()
