# -*- coding: utf-8 -*-
import http.client
import json
import logging
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from streamvault import i18n, util
from streamvault.archive import ytdlp
from streamvault.events import EventBus
from streamvault.logs import MemoryLogHandler
from streamvault.settings import Settings
from streamvault.sites.common import Memo
from streamvault.ui.api import Api, split_args
from streamvault.ui.app import WEB_DIR
from streamvault.ui.bridge import EventPump
from streamvault.ui.server import TOKEN_HEADER, BrowserServer
from tests.helpers import wait_for
from tests.test_search import answer


class StubLive:
    def __init__(self):
        self.channels = []
        self.calls = []

    def snapshot(self):
        return {"channels": self.channels, "processing": {"pending": 0, "current": None}}

    def twitch_profiles(self, logins):
        return {login: {"name": login.upper(), "avatar": ""} for login in logins}

    def disk_status(self):
        return {"level": "ok", "free": 10 ** 12, "floor": 5 * 1024 ** 3, "path": ""}

    def set_keep_days(self, cid, days=None):
        self.calls.append(("keep_days", cid, days))

    def remember_profile(self, cid, profile):
        self.calls.append(("profile", cid, profile))
        return True

    def add_channel(self, text, quality=None, enabled=False, profile=None):
        self.calls.append(("add", text, quality))
        return "twitch:" + text


class StubDownloads:
    def __init__(self, root):
        self.root = root
        self.added = []
        self.chat_modes = []

    def downloads_dir(self):
        return os.path.join(self.root, "downloads")

    def snapshot(self):
        return {"jobs": [], "active": 0, "downloads_dir": self.downloads_dir()}

    def add(self, url, quality=None, section=None, meta=None, force=False, chat_mode=None):
        self.added.append((url, quality, section, meta))
        self.chat_modes.append(chat_mode)
        return "job1"


class ApiTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "streams")
        self.settings = Settings(os.path.join(self.dir, "settings.json"),
                                 {"root_path": self.root, "twitch_client_secret": "секрет"})
        self.live = StubLive()
        self.downloads = StubDownloads(self.root)
        self.api = Api(self.settings, self.live, self.downloads, logs=MemoryLogHandler())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_only_listed_methods(self):
        self.assertFalse(self.api.call("_checked", ["x", 1])["ok"])
        self.assertFalse(self.api.call("attach_window", [None])["ok"])
        self.assertFalse(self.api.call("init", "не список")["ok"])
        reply = self.api.call("init", [])
        self.assertTrue(reply["ok"])
        sites = reply["result"]["sites"]
        self.assertIn("clips", sites["twitch"]["listings"])
        self.assertEqual(sites["twitch"]["periodic"], ["clips"])
        self.assertIn("clips", sites["youtube"]["unsupported"])
        self.assertEqual(sites["youtube"]["chat_skip_status"], ["not_live"])

    def test_errors_are_plain_text(self):
        # KeyError печатался бы в кавычках
        self.live.add_channel = lambda text, quality=None, enabled=False, profile=None: (_ for _ in ()).throw(KeyError("нет канала"))
        self.assertEqual(self.api.call("add_channel", ["x"]), {"ok": False, "error": "нет канала"})

    def test_resolve(self):
        self.assertEqual(self.api.resolve("kick:XQC")["id"], "kick:xqc")
        self.assertEqual(self.api.resolve("https://www.twitch.tv/LegendaAFK")["id"],
                         "twitch:legendaafk")
        self.assertEqual(self.api.resolve("https://www.twitch.tv/videos/1"),
                         {"kind": "url", "url": "https://www.twitch.tv/videos/1"})
        with self.assertRaises(ValueError):
            self.api.resolve("foo:bar")

    def test_profiles_of_all_platforms(self):
        kick = {"slug": "xqc", "user": {"username": "xQc", "profile_pic": "https://k/x.png"}}
        vk = {"blogUrl": "kolya", "owner": {"displayName": "Коля", "avatarUrl": "https://v/k.png"}}

        def request(method, url, **kwargs):
            data = kick if "kick.com" in url else vk if "kolya" in url else None
            return answer(200 if data else 404, data)

        memo = mock.patch("streamvault.sites.kick._info", Memo(60))
        with memo, mock.patch("streamvault.sites.common.requests.request", side_effect=request):
            found = self.api.profiles(["twitch:xqc", "kick:xqc", "vkvideolive:kolya",
                                       "vkvideolive:nobody", "twitch:https://twitch.tv/v/1", "foo:bar"])
        self.assertEqual(found, {"twitch:xqc": {"name": "XQC", "avatar": ""},
                                 "kick:xqc": {"name": "xQc", "avatar": "https://k/x.png"},
                                 "vkvideolive:kolya": {"name": "Коля", "avatar": "https://v/k.png"}})
        # найденное сохраняется с каналом, как профиль из поиска
        self.assertIn(("profile", "kick:xqc", found["kick:xqc"]), self.live.calls)
        # «канала нет» помнится: второй раз площадку не спрашиваем
        with mock.patch("streamvault.sites.common.requests.request") as again:
            self.api.profiles(["vkvideolive:nobody", "kick:xqc"])
        again.assert_not_called()

    def test_youtube_profile_through_ytdlp(self):
        answers = {"channel": "Канал", "thumbnails": [
            {"id": "banner_uncropped", "url": "https://yt3/banner"},
            {"id": "avatar_uncropped", "url": "https://yt3/avatar"}]}
        self.downloads.ytdlp_json = mock.Mock(return_value=answers)
        found = self.api.profiles(["youtube:someone"])
        self.assertEqual(found, {"youtube:someone": {"name": "Канал", "avatar": "https://yt3/avatar"}})
        args = self.downloads.ytdlp_json.call_args.args[0]
        self.assertEqual(args[-1], "https://www.youtube.com/@someone")
        self.assertIn("--flat-playlist", args)
        self.assertEqual(args[args.index("-I") + 1], "0")

    def test_library_tells_free_space(self):
        class Library:
            def items(self):
                return []

            def folders(self):
                return {}

        self.api._library = Library()
        free = self.api.library()["free"]
        # папки загрузок ещё нет — место берётся у ближайшей существующей
        self.assertIsInstance(free["recordings"], int)
        self.assertIsInstance(free["downloads"], int)

    def test_files_outside_recordings_are_refused(self):
        os.makedirs(self.downloads.downloads_dir())
        inside = os.path.join(self.downloads.downloads_dir(), "a.mp4")
        open(inside, "wb").close()
        self.assertEqual(self.api._media(inside), os.path.abspath(inside))
        outside = os.path.join(self.dir, "evil.mp4")
        open(outside, "wb").close()
        script = os.path.join(self.downloads.downloads_dir(), "run.bat")
        open(script, "wb").close()
        for path in (outside, script, os.path.join(self.downloads.downloads_dir(), "..", "..", "evil.mp4"),
                     r"C:\Windows\System32\calc.exe"):
            with self.assertRaises(ValueError, msg=path):
                self.api._media(path)
        self.assertFalse(self.api.call("open_url", ["file:///C:/Windows/notepad.exe"])["ok"])

    def test_settings_hide_secrets(self):
        data = self.api.get_settings()
        self.assertNotIn("twitch_client_secret", data)
        self.assertTrue(data["twitch_client_secret_set"])
        self.assertFalse(data["twitch_auth_token_set"])

    def test_save_settings(self):
        result = self.api.save_settings({
            "twitch_auth_token": "OAuth abc123XYZ",
            "ytdlp_extra_args": r'--cookies "C:\My Files\c.txt" --limit-rate 5M',
            "max_downloads": "3",
            "root_path": os.path.join(self.dir, "other")})
        self.assertTrue(result["restart"])
        self.assertEqual(self.settings["twitch_auth_token"], "abc123XYZ")
        self.assertEqual(self.settings["ytdlp_extra_args"],
                         ["--cookies", r"C:\My Files\c.txt", "--limit-rate", "5M"])
        self.assertEqual(self.settings["max_downloads"], 3)
        self.assertTrue(result["settings"]["twitch_auth_token_set"])
        # секрет, которого нет в изменениях, не трогается
        self.api.save_settings({"quality": "720p"})
        self.assertEqual(self.settings["twitch_client_secret"], "секрет")

    def test_recordings_safety_settings(self):
        result = self.api.save_settings({"record_from_start": False, "min_free_gb": "10",
                                         "keep_days": 30})
        self.assertFalse(result["restart"])
        self.assertEqual((self.settings["record_from_start"], self.settings["min_free_gb"],
                          self.settings["keep_days"]), (False, 10, 30))
        self.api.set_keep_days("kick:a", 7)
        self.api.set_keep_days("kick:a", None)
        self.assertEqual(self.live.calls[-2:], [("keep_days", "kick:a", 7), ("keep_days", "kick:a", None)])
        for days in (-1, 5000, "7", True):
            with self.assertRaises(ValueError):
                self.api.set_keep_days("kick:a", days)
        self.assertEqual(self.api.call("init", [])["result"]["disk"]["level"], "ok")

    def test_save_settings_rejects(self):
        for changes in ({"channels": []}, {"max_downloads": 50}, {"log_level": "loud"},
                        {"record_from_start": "да"}, {"keep_days": -1}, {"min_free_gb": 5000},
                        {"twitch_auth_token": "abc\tdef"}, {"refresh": {"tiktok": 30}}):
            with self.assertRaises(ValueError, msg=str(changes)):
                self.api.save_settings(changes)

    def test_add_download_cleans_input(self):
        self.api.add_download("https://x.tv/1", "720", ["1:00", "2:00"],
                              {"title": "t", "platform": "twitch", "duration": 300,
                               "evil": "x", "timestamp": "не число"})
        url, quality, section, meta = self.downloads.added[0]
        self.assertEqual(section, (60.0, 120.0))
        self.assertEqual(meta, {"title": "t", "platform": "twitch", "duration": 300})
        with self.assertRaises(ValueError):
            self.api.add_download("javascript:alert(1)")
        with self.assertRaises(ValueError):
            self.api.add_download("https://x.tv/1", None, ["2:00", "1:00"])

    def test_search_channels(self):
        from streamvault import search
        self.downloads.youtube_channels = lambda query, limit: [{"uploader_id": "@" + query, "channel": query}]
        result = self.api.call("search_channels", ["youtube", "abc"])
        self.assertEqual(result["result"]["results"][0]["id"], "youtube:abc")
        # платформа не ответила — пустая выдача с причиной, а не ошибка
        with mock.patch("streamvault.sites.kick.search", side_effect=search.SearchError("нет связи")):
            self.assertEqual(self.api.call("search_channels", ["kick", "abc"])["result"],
                             {"results": [], "error": "нет связи"})

    def test_add_download_chat_mode(self):
        self.api.add_download("https://twitch.tv/videos/1", None, None, None, False, "render")
        self.api.add_download("https://twitch.tv/videos/2")
        self.assertEqual(self.downloads.chat_modes, ["render", None])
        with self.assertRaises(ValueError):
            self.api.add_download("https://twitch.tv/videos/3", None, None, None, False, "всё")

    def test_library_only_inside_its_folders(self):
        class StubLibrary:
            def preview(library, path):
                return {"thumbnail": "", "duration": 1}

            def delete(library, path):
                raise AssertionError("удалять нельзя")

        self.api = Api(self.settings, self.live, self.downloads, library=StubLibrary())
        os.makedirs(self.downloads.downloads_dir())
        inside = os.path.join(self.downloads.downloads_dir(), "a.mp4")
        open(inside, "wb").close()
        self.assertEqual(self.api.library_preview(inside)["duration"], 1)
        outside = os.path.join(self.dir, "settings.json")
        self.assertFalse(self.api.call("delete_file", [outside])["ok"])
        self.assertFalse(self.api.call("library_preview", [r"C:\Windows\win.ini"])["ok"])

    def test_autostart_and_flags(self):
        switched = []

        class StubAutostart:
            def is_enabled(self):
                return bool(switched and switched[-1])

            def set_enabled(self, enabled):
                switched.append(enabled)

        self.api = Api(self.settings, self.live, self.downloads, autostart=StubAutostart())
        result = self.api.save_settings({"autostart": True, "close_to_tray": False})
        self.assertEqual(switched, [True])
        self.assertTrue(result["settings"]["autostart"])
        self.assertFalse(self.settings["close_to_tray"])
        # негодное значение рядом — реестр не трогается
        with self.assertRaises(ValueError):
            self.api.save_settings({"autostart": False, "tray_notifications": "да"})
        self.assertEqual(switched, [True])
        # без модуля автозапуска (не Windows, режим тестов) переключатель недоступен
        bare = Api(self.settings, self.live, self.downloads)
        self.assertFalse(bare.get_settings()["autostart"])
        with self.assertRaises(ValueError):
            bare.save_settings({"autostart": True})

    def test_ffmpeg_path_is_checked_before_saving(self):
        refreshed = []

        class StubLocator:
            def refresh(self):
                refreshed.append(True)

            def status(self):
                return {"path": None, "version": None, "own": False}

        self.api = Api(self.settings, self.live, self.downloads, ffmpeg=StubLocator())
        with self.assertRaises(ValueError):
            self.api.save_settings({"ffmpeg_path": os.path.join(self.dir, "нет.exe")})
        not_ffmpeg = os.path.join(self.dir, "fake.exe")
        open(not_ffmpeg, "wb").close()
        with self.assertRaises(ValueError):
            self.api.check_ffmpeg(not_ffmpeg)
        self.assertEqual(refreshed, [])
        real = shutil.which("ffmpeg")
        if real:
            self.assertTrue(self.api.check_ffmpeg('"%s"' % real)["version"])
            self.api.save_settings({"ffmpeg_path": real})
            self.assertEqual(self.settings["ffmpeg_path"], os.path.abspath(real))
            self.assertEqual(refreshed, [True])
        # пусто — снова искать самой
        self.api.save_settings({"ffmpeg_path": ""})
        self.assertEqual(self.settings["ffmpeg_path"], "")
        self.assertIsNone(self.api.ffmpeg_status()["install"])

    def test_language(self):
        self.addCleanup(i18n.set_language, "ru")
        self.api.save_settings({"language": "en"})
        self.assertEqual(i18n.current_language(), "en")
        self.assertEqual(self.api.init()["language"], "en")
        # ошибки окна — уже на новом языке
        self.assertEqual(self.api.call("open_url", ["file:///x"])["error"],
                         "only http and https links can be opened")
        with self.assertRaises(ValueError):
            self.api.save_settings({"language": "zh"})

    def test_split_args_keeps_backslashes(self):
        self.assertEqual(split_args(r"--paths D:\Video\x -N 4"), ["--paths", r"D:\Video\x", "-N", "4"])


class TokenTest(unittest.TestCase):

    def test_normalize(self):
        self.assertEqual(ytdlp.normalize_auth_token("auth-token=abc123"), "abc123")
        self.assertEqual(ytdlp.normalize_auth_token(" oauth:abc123 "), "abc123")
        self.assertEqual(ytdlp.normalize_auth_token(""), "")
        with self.assertRaises(ValueError):
            ytdlp.normalize_auth_token("abc\n.twitch.tv")

    def test_cookie_file(self):
        path = ytdlp.write_cookie_file("abc123")
        try:
            with open(path, encoding="utf-8") as source:
                lines = source.read().splitlines()
            self.assertEqual(lines[0], "# Netscape HTTP Cookie File")
            fields = lines[1].split("\t")
            self.assertEqual((fields[0], fields[5], fields[6]), (".twitch.tv", "auth-token", "abc123"))
        finally:
            os.remove(path)


class PumpTest(unittest.TestCase):

    def test_coalesces_and_adds_recording_sizes(self):
        bus = EventBus()
        live = StubLive()
        live.channels = [{"id": "twitch:a", "recording": {"bytes": 500}}]
        batches = []
        pump = EventPump(bus, live, batches.append, interval=0.05, tick=0.05)
        for percent in (10, 20, 30):
            bus.emit("download", job={"id": "j1", "progress": {"percent": percent}})
        bus.emit("log", message="строка")
        bus.emit("channel", channel={"id": "twitch:a", "state": "offline"})
        bus.emit("channel", channel={"id": "twitch:a", "state": "recording"})
        pump.start()
        try:
            self.assertTrue(wait_for(lambda: batches, timeout=3))
        finally:
            pump.stop()
        events = batches[0]
        downloads = [e for e in events if e["type"] == "download"]
        channels = [e for e in events if e["type"] == "channel"]
        self.assertEqual([e["job"]["progress"]["percent"] for e in downloads], [30])
        self.assertEqual([e["channel"]["state"] for e in channels], ["recording"])
        self.assertIn({"twitch:a": 500}, [e.get("bytes") for e in events])
        self.assertEqual(len([e for e in events if e["type"] == "log"]), 1)


class ServerTest(unittest.TestCase):

    def setUp(self):
        self.calls = []

        class FakeApi:
            def call(api, name, args):
                self.calls.append((name, args))
                return {"ok": True, "result": "pong"}

        self.server = BrowserServer(FakeApi(), WEB_DIR)
        self.server.start()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None, token=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if token:
            headers[TOKEN_HEADER] = self.server.token if token is True else token
        if host:
            headers["Host"] = host
        connection.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_page_with_security_headers(self):
        response, data = self.request("GET", "/", token=False)
        self.assertEqual(response.status, 200)
        self.assertIn(b"StreamVault", data)
        self.assertIn("script-src 'self'", response.getheader("Content-Security-Policy"))

    def test_no_files_outside_web_folder(self):
        for path in ("/../api.py", "/..%2F..%2Fsettings.py", "/../../settings.py"):
            response, _ = self.request("GET", path, token=False)
            self.assertEqual(response.status, 404, path)

    def test_api_needs_token_and_right_host(self):
        response, _ = self.request("POST", "/api", {"name": "init", "args": []}, token=False)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/api", {"name": "init", "args": []}, token="wrong-token")
        self.assertEqual(response.status, 403)
        # подмена адреса через DNS: токен верный, а Host — чужой сайт
        response, _ = self.request("POST", "/api", {"name": "init", "args": []},
                                   host="evil.example:%d" % self.server.port)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.calls, [])
        response, data = self.request("POST", "/api", {"name": "init", "args": [1]})
        self.assertEqual(json.loads(data), {"ok": True, "result": "pong"})
        self.assertEqual(self.calls, [("init", [1])])

    def test_events_long_poll(self):
        threading.Timer(0.3, self.server.push, args=([{"type": "log"}],)).start()
        response, data = self.request("GET", "/events?after=0")
        reply = json.loads(data)
        self.assertEqual(reply["seq"], 1)
        self.assertEqual(reply["batches"], [[{"type": "log"}]])

    def test_events_mark_skips_old_batches(self):
        # было: страница брала события с нуля и второй раз показывала строки журнала из снимка
        self.server.push([{"type": "log", "text": "старое"}])
        response, data = self.request("GET", "/events?after=-1")
        self.assertEqual(json.loads(data), {"seq": 1, "batches": []})


class InstanceTest(unittest.TestCase):

    def test_second_copy_is_refused(self):
        name = "StreamVaultTest-" + uuid.uuid4().hex
        self.assertTrue(util.single_instance(name))
        self.assertFalse(util.single_instance(name))


class MemoryLogTest(unittest.TestCase):

    def test_keeps_last_lines(self):
        handler = MemoryLogHandler(capacity=2)
        logger = logging.getLogger("streamvault-test-memory")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        try:
            for index in range(3):
                logger.info("строка %d", index)
        finally:
            logger.removeHandler(handler)
        self.assertEqual([line["message"] for line in handler.lines()], ["строка 1", "строка 2"])


if __name__ == "__main__":
    unittest.main()
