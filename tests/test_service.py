# -*- coding: utf-8 -*-
import collections
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

import requests

from streamvault import util
from streamvault.events import EventBus
from streamvault.live.probes import PublicProfiles
from streamvault.live.service import LiveService, migrate_folders
from streamvault.platforms import PLATFORMS
from streamvault.settings import Settings
from tests.helpers import OFFLINE, ONLINE, EventLog, FakeStreamlink, wait_for


class PerChannelProbe:
    """Отвечает по имени канала; по умолчанию — не в эфире."""

    def __init__(self, name):
        self.name = name
        self.answers = {}
        self.calls = collections.Counter()
        self._lock = threading.Lock()

    def check(self, platform, channel):
        with self._lock:
            self.calls[channel] += 1
        return self.answers.get(channel, OFFLINE)

    def is_unusable(self):
        return False

    def refresh_token(self):
        pass

    def disable(self):
        pass


class ServiceTestCase(unittest.TestCase):

    CHANNELS = [{"platform": "twitch", "channel": "a", "enabled": True},
                {"platform": "kick", "channel": "b", "enabled": False}]

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.streams = os.path.join(self.dir, "streams")
        self.settings_path = os.path.join(self.dir, "settings.json")
        self.settings = Settings(self.settings_path,
                                 {"root_path": self.streams, "channels": self.CHANNELS})
        self.helix = PerChannelProbe("helix")
        self.streamlink = PerChannelProbe("streamlink")
        probes = {name: self.streamlink for name in PLATFORMS}
        probes["twitch"] = self.helix
        patcher = mock.patch("streamvault.live.service.build_probes",
                             return_value=(probes, self.streamlink))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bus = EventBus()
        self.events = EventLog(self.bus)
        self.service = None
        logs = self.assertLogs(level="INFO")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def tearDown(self):
        if self.service is not None:
            self.service.shutdown(grace=0)
        shutil.rmtree(self.dir, ignore_errors=True)

    def start(self, **kwargs):
        self.service = LiveService(self.settings, bus=self.bus, disable_ffmpeg=True, **kwargs)
        self.service.start()
        return self.service

    def state(self, cid):
        return self.service.channel_snapshot(cid)["state"]

    def saved_channels(self):
        return Settings.load(self.settings_path)["channels"]


class ChannelListTest(ServiceTestCase):

    def test_start_watches_every_channel(self):
        # без автозаписи канал тоже проверяется: окно показывает, кто в эфире
        service = self.start()
        self.assertTrue(wait_for(lambda: self.state("twitch:a") == "offline"))
        self.assertTrue(wait_for(lambda: self.state("kick:b") == "offline"))
        self.assertFalse(service.channel_snapshot("kick:b")["enabled"])
        ids = [channel["id"] for channel in service.snapshot()["channels"]]
        self.assertEqual(ids, ["twitch:a", "kick:b"])

    def test_console_watches_only_autorecord(self):
        service = self.start(watch_all=False)
        self.assertTrue(wait_for(lambda: self.state("twitch:a") == "offline"))
        self.assertEqual(self.state("kick:b"), "paused")
        self.assertEqual(self.streamlink.calls["b"], 0)
        service.set_enabled("kick:b", True)
        self.assertTrue(wait_for(lambda: self.state("kick:b") == "offline"))

    def test_add_and_remove_are_saved(self):
        service = self.start()
        self.assertEqual(service.add_channel("https://kick.com/C"), "kick:c")
        # автозапись нового канала выключена, пока её не включат
        self.assertIn({"platform": "kick", "channel": "c", "enabled": False},
                      self.saved_channels())
        service.add_channel("kick:d", enabled=True)
        self.assertIn({"platform": "kick", "channel": "d", "enabled": True}, self.saved_channels())
        self.assertTrue(wait_for(lambda: self.streamlink.calls["c"] >= 1))

        with self.assertRaises(ValueError):
            service.add_channel("kick:C")
        with self.assertRaises(ValueError):
            service.add_channel("foo:bar")

        service.remove_channel("kick:c")
        self.assertNotIn("c", [entry["channel"] for entry in self.saved_channels()])
        self.assertEqual(self.events.of("channel_removed")[-1]["id"], "kick:c")
        with self.assertRaises(KeyError):
            service.remove_channel("kick:c")

    def test_profile_from_search_is_saved(self):
        # имя и аватарку Kick и YouTube больше неоткуда взять — они хранятся с каналом
        service = self.start()
        service.add_channel("kick:p", profile={"name": "Пэ", "avatar": "https://x/p.png"})
        service.add_channel("kick:q", profile={"name": "", "avatar": "javascript:alert(1)"})
        saved = {entry["channel"]: entry.get("profile") for entry in self.saved_channels()}
        self.assertEqual(saved["p"], {"name": "Пэ", "avatar": "https://x/p.png"})
        self.assertIsNone(saved["q"])
        self.assertEqual(service.channel_snapshot("kick:p")["profile"]["name"], "Пэ")

    def test_autorecord_switch_keeps_watching(self):
        service = self.start()
        self.assertTrue(wait_for(lambda: self.state("twitch:a") == "offline"))
        recorder = service._recorders["twitch:a"]
        service.set_enabled("kick:b", True)
        service.set_enabled("twitch:a", False)
        saved = {entry["channel"]: entry["enabled"] for entry in self.saved_channels()}
        self.assertEqual(saved, {"a": False, "b": True})
        snapshot = service.channel_snapshot("twitch:a")
        self.assertEqual((snapshot["state"], snapshot["enabled"]), ("offline", False))
        # тот же поток наблюдения, канал по-прежнему проверяется
        self.assertIs(service._recorders["twitch:a"], recorder)
        calls = self.helix.calls["a"]
        service.check_now("twitch:a")
        self.assertTrue(wait_for(lambda: self.helix.calls["a"] > calls))

    def test_check_now(self):
        service = self.start()
        self.assertTrue(wait_for(lambda: self.helix.calls["a"] == 1))
        service.check_now("twitch:a")
        self.assertTrue(wait_for(lambda: self.helix.calls["a"] == 2, timeout=3))

    def test_quality(self):
        service = self.start()
        service.set_quality("twitch:a", "720p")
        self.assertEqual(service.channel_snapshot("twitch:a")["quality"], "720p")
        self.assertEqual(self.saved_channels()[0]["quality"], "720p")
        service.set_quality("twitch:a", None)
        self.assertEqual(service.channel_snapshot("twitch:a")["quality"], "best")

    def test_one_off_channels_are_not_saved(self):
        service = self.start(channels=["kick:z"])
        service.add_channel("kick:y")
        self.assertFalse(os.path.exists(self.settings_path))
        self.assertEqual([c["id"] for c in service.snapshot()["channels"]],
                         ["kick:z", "kick:y"])

    def test_shutdown(self):
        service = self.start()
        wait_for(lambda: self.state("twitch:a") == "offline")
        self.assertEqual(service.shutdown(grace=0), 0)
        self.service = None
        self.assertFalse(service.is_running())
        self.assertEqual(service.channel_snapshot("twitch:a")["state"], "stopped")


    def test_convert_recording(self):
        service = LiveService(self.settings, bus=self.bus)
        with self.assertRaises(ValueError):
            service.convert_recording(os.path.join(self.dir, "a.ts"))   # служба не запущена
        service = self.start()
        with mock.patch.object(service._processing, "submit") as submit:
            path = os.path.join(self.dir, util.FINISHED_DIR, "twitch", "a", "a.ts")
            service.convert_recording(path)
            submit.assert_called_once_with(path, path, trusted=True, convert=True)
            with self.assertRaises(ValueError):
                service.convert_recording(os.path.join(self.dir, "a.mp4"))
        # переключатели в настройках очередь читает на ходу
        self.assertTrue(service._processing._disable_ffmpeg())   # ключ --disable-ffmpeg тестов
        service._disable_ffmpeg = False
        self.assertFalse(service._processing._disable_ffmpeg())
        self.assertTrue(service._processing._faststart())
        self.settings.update(convert_recordings=False, mp4_faststart=False)
        self.assertTrue(service._processing._disable_ffmpeg())
        self.assertFalse(service._processing._faststart())

    def test_channel_options(self):
        # у канала свои настройки записи поверх общих; запись узнаётся по папке
        service = self.start()
        service._disable_ffmpeg = False
        recorded = os.path.join(self.streams, util.IN_PROGRESS_DIR, "kick", "b", "b - 2026-10-28 12h00m00s.ts")
        other = os.path.join(self.streams, util.IN_PROGRESS_DIR, "twitch", "a", "a - 2026-10-28 12h00m00s.ts")
        self.settings.update(split_mode="count", split_count=3)
        service.set_options("kick:b", {"convert_recordings": False, "split_mode": "size", "split_size_gb": 1.5,
                                       "record_from_start": True, "live_chat": "render"})
        self.assertTrue(service._processing._disable_ffmpeg(recorded))
        self.assertFalse(service._processing._disable_ffmpeg(other))
        self.assertEqual(service._processing._split(recorded), ("size", 1.5, 60, 3))
        self.assertEqual(service._processing._split(other), ("count", 2.0, 60, 3))
        # у канала — своё, у остальных — общее (по умолчанию выключено)
        self.assertTrue(service._option("kick:b", "record_from_start"))
        self.assertFalse(service._option("twitch:a", "record_from_start"))
        self.assertEqual(service.channel_snapshot("kick:b")["options"]["live_chat"], "render")
        # сохраняются в settings.json и переживают перезапуск
        saved = {entry["channel"]: entry for entry in self.saved_channels()}
        self.assertEqual(saved["b"]["options"]["split_size_gb"], 1.5)
        self.assertNotIn("options", saved["a"])
        # чат эфира тоже берёт режим канала: ChatFinisher знает только путь
        finished = os.path.join(self.streams, util.FINISHED_DIR, "kick", "b", "b.mp4")
        self.assertEqual(service._chat_finisher._mode(finished), "render")
        # в тестах общий режим — без чата (tests/__init__.py)
        self.assertEqual(service._chat_finisher._mode(
            os.path.join(self.streams, util.FINISHED_DIR, "twitch", "a", "a.mp4")), "none")
        # None — снова как в общих
        service.set_options("kick:b", {key: None for key in ("convert_recordings", "split_mode", "split_size_gb",
                                                             "record_from_start", "live_chat")})
        self.assertNotIn("options", {entry["channel"]: entry for entry in self.saved_channels()}["b"])
        self.assertFalse(service._processing._disable_ffmpeg(recorded))


class RecordingFlowTest(ServiceTestCase):

    CHANNELS = [{"platform": "twitch", "channel": "x", "enabled": True}]

    def recording_bytes(self, cid):
        return (self.service.channel_snapshot(cid)["recording"] or {}).get("bytes", 0)

    def test_stop_recording_keeps_watching_and_file_is_processed(self):
        # было: остановить запись можно было, только выключив автозапись
        self.helix.answers["x"] = ONLINE
        with FakeStreamlink("forever"):
            service = self.start()
            self.assertTrue(wait_for(lambda: self.recording_bytes("twitch:x") > 0))
            snapshot = service.channel_snapshot("twitch:x")
            self.assertEqual(snapshot["state"], "recording")
            self.assertEqual(snapshot["stream"]["title"], "тестовый стрим")

            service.stop_recording("twitch:x")
            processed = os.path.join(self.streams, util.FINISHED_DIR, "twitch", "x")
            self.assertTrue(wait_for(lambda: os.path.isdir(processed)
                                     and len(os.listdir(processed)) == 1, timeout=20))
            self.assertTrue(wait_for(lambda: self.state("twitch:x") == "live"))
            # автозапись осталась включённой — к следующему эфиру
            self.assertTrue(service.channel_snapshot("twitch:x")["enabled"])
        recorded = os.path.join(self.streams, util.IN_PROGRESS_DIR, "twitch", "x")
        self.assertEqual(os.listdir(recorded), [])
        actions = [event["action"] for event in self.events.of("processing")]
        self.assertEqual(actions, ["queued", "started", "done"])

    def test_record_channel_not_in_list(self):
        self.helix.answers["y"] = ONLINE
        with FakeStreamlink("forever"):
            service = self.start()
            self.assertEqual(service.record_now("y"), "twitch:y")
            self.assertTrue(wait_for(lambda: self.recording_bytes("twitch:y") > 0))
            self.assertTrue(service.channel_snapshot("twitch:y")["temporary"])
            # в эфире видно, в настройки не сохраняется
            self.assertNotIn("y", [entry["channel"] for entry in self.saved_channels()])
            service.stop_recording("twitch:y")
            self.assertTrue(wait_for(lambda: any(event["id"] == "twitch:y"
                                                 for event in self.events.of("channel_removed"))))
        self.assertNotIn("twitch:y", [channel["id"] for channel in service.snapshot()["channels"]])

    def test_follow_channel_while_it_is_recorded(self):
        self.helix.answers["y"] = ONLINE
        with FakeStreamlink("forever"):
            service = self.start()
            service.record_now("twitch:y")
            self.assertTrue(wait_for(lambda: self.recording_bytes("twitch:y") > 0))
            recorder = service._recorders["twitch:y"]
            service.add_channel("twitch:y")
            self.assertIn({"platform": "twitch", "channel": "y", "enabled": False}, self.saved_channels())
            snapshot = service.channel_snapshot("twitch:y")
            self.assertEqual((snapshot["state"], snapshot["temporary"]), ("recording", False))
            self.assertIs(service._recorders["twitch:y"], recorder)

    def test_check_channel_not_in_list(self):
        # страница канала не из списка: в эфире ли он — без добавления в список
        service = self.start()
        self.helix.answers["q"] = ONLINE
        answer = service.check_channel("twitch:q")
        self.assertEqual((answer["state"], answer["stream"]["title"]), ("live", "тестовый стрим"))
        self.assertIn("live_user_q", answer["stream"]["thumbnail"])
        self.assertEqual(service.check_channel("twitch:z")["state"], "offline")
        self.assertNotIn("twitch:q", [channel["id"] for channel in service.snapshot()["channels"]])

    def test_record_now_needs_live_stream(self):
        service = self.start()
        with self.assertRaisesRegex(ValueError, "не в эфире"):
            service.record_now("twitch:z")
        self.assertNotIn("twitch:z", [channel["id"] for channel in service.snapshot()["channels"]])
        # канал из списка без автозаписи пишется по кнопке, когда эфир есть
        self.helix.answers["x"] = ONLINE
        service.set_enabled("twitch:x", False)
        with FakeStreamlink("forever"):
            self.assertTrue(wait_for(lambda: self.state("twitch:x") == "live", timeout=20))
            service.record_now("twitch:x")
            self.assertTrue(wait_for(lambda: self.recording_bytes("twitch:x") > 0))
            service.stop_recording("twitch:x")
            self.assertTrue(wait_for(lambda: self.state("twitch:x") == "live"))



class FolderMigrationTest(unittest.TestCase):
    """recorded и processed прошлых версий — под понятными именами."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)

    def put(self, *parts, data=b"x"):
        path = os.path.join(self.root, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as target:
            target.write(data)
        return path

    def test_rename_whole_folders(self):
        self.put("recorded", "twitch", "a", "идёт.ts")
        self.put("processed", "twitch", "a", "готово.mp4")
        with self.assertLogs(level="INFO"):
            migrate_folders(self.root)
        self.assertEqual(sorted(os.listdir(self.root)), [util.FINISHED_DIR, util.IN_PROGRESS_DIR])
        self.assertTrue(os.path.isfile(os.path.join(self.root, util.FINISHED_DIR, "twitch", "a", "готово.mp4")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, util.IN_PROGRESS_DIR, "twitch", "a", "идёт.ts")))

    def test_merge_into_existing_folder(self):
        # новая папка уже есть: файлы переносятся по одному, одинаковые имена не затираются
        self.put("processed", "twitch", "a", "запись.mp4", data=b"old")
        self.put(util.FINISHED_DIR, "twitch", "a", "запись.mp4", data=b"new")
        with self.assertLogs(level="INFO"):
            migrate_folders(self.root)
        folder = os.path.join(self.root, util.FINISHED_DIR, "twitch", "a")
        self.assertEqual(sorted(os.listdir(folder)), ["запись (2).mp4", "запись.mp4"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "processed")))

    def test_nothing_to_do(self):
        migrate_folders(self.root)
        self.assertEqual(os.listdir(self.root), [])


class PublicProfilesTest(unittest.TestCase):
    """Аватарки Twitch без своих ключей."""

    def answer(self, users):
        response = mock.Mock()
        response.json.return_value = {"data": {"users": users}}
        return response

    def test_found_and_missing_are_cached(self):
        profiles = PublicProfiles()
        users = [{"login": "qewbite", "displayName": "Qewbite", "profileImageURL": "https://x/q.png"}, None]
        with mock.patch("streamvault.live.probes.requests.post",
                        return_value=self.answer(users)) as post:
            found = profiles.profiles(["Qewbite", "nobody"])
            again = profiles.profiles(["qewbite", "nobody"])
        self.assertEqual(found, {"qewbite": {"name": "Qewbite", "avatar": "https://x/q.png"}})
        self.assertEqual(again, found)
        self.assertEqual(post.call_count, 1)      # и найденный, и несуществующий — один раз
        self.assertEqual(post.call_args.kwargs["json"]["variables"], {"logins": ["qewbite", "nobody"]})

    def test_network_error_is_not_cached(self):
        profiles = PublicProfiles()
        with mock.patch("streamvault.live.probes.requests.post",
                        side_effect=requests.exceptions.ConnectionError()):
            self.assertEqual(profiles.profiles(["qewbite"]), {})
        users = [{"login": "qewbite", "displayName": "Qewbite", "profileImageURL": "https://x/q.png"}]
        with mock.patch("streamvault.live.probes.requests.post", return_value=self.answer(users)):
            self.assertIn("qewbite", profiles.profiles(["qewbite"]))

    def test_service_without_keys_uses_public_profiles(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        service = LiveService(Settings(os.path.join(directory, "settings.json"),
                                       {"root_path": directory}))
        users = [{"login": "qewbite", "displayName": "Qewbite", "profileImageURL": "https://x/q.png"}]
        with mock.patch("streamvault.live.probes.requests.post", return_value=self.answer(users)):
            self.assertEqual(service.twitch_profiles(["qewbite"])["qewbite"]["avatar"], "https://x/q.png")


if __name__ == "__main__":
    unittest.main()
