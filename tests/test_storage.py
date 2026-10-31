# -*- coding: utf-8 -*-
"""Сохранность записей: запись с начала эфира, место на диске, срок хранения."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from streamvault import util
from streamvault.events import EventBus
from streamvault.live.probes import Status
from streamvault.live.storage import (CRITICAL, GB, LOW, OK, DiskGuard, Retention,
                                      channel_days, expired_recordings)
from streamvault.platforms import normalize_channels
from streamvault.settings import Settings
from tests.helpers import OFFLINE, EventLog, FakeProcessing, FakeStreamlink, ScriptedProbe, wait_for
from tests.test_recorder import InstantRecorder


class FakeUsage:
    def __init__(self, free):
        self.free = free

    def __call__(self, path):
        return SimpleNamespace(free=self.free)


class DiskGuardTest(unittest.TestCase):

    def setUp(self):
        self.usage = FakeUsage(100 * GB)
        self.bus = EventBus()
        self.events = EventLog(self.bus)
        self.guard = DiskGuard(lambda: 5 * GB, self.bus, usage=self.usage)

    def test_levels_are_announced_once(self):
        self.assertEqual(self.guard.check("/x")[0], OK)
        self.usage.free = 3 * GB
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(self.guard.check("/x")[0], LOW)
            self.assertEqual(self.guard.check("/x")[0], LOW)
        self.assertEqual(len(logs.output), 1)
        self.usage.free = GB // 2           # меньше пятой части порога
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.guard.check("/x")[0], CRITICAL)
        self.usage.free = 50 * GB
        with self.assertLogs(level="INFO"):
            self.assertEqual(self.guard.check("/x")[0], OK)
        self.assertEqual([event["level"] for event in self.events.of("disk")], [LOW, CRITICAL, OK])
        self.assertEqual(self.guard.snapshot()["level"], OK)

    def test_zero_floor_means_off(self):
        guard = DiskGuard(lambda: 0, usage=FakeUsage(1))
        self.assertEqual(guard.check("/x")[0], OK)

    def test_missing_folder_does_not_block(self):
        def broken(path):
            raise FileNotFoundError(path)
        self.assertEqual(DiskGuard(lambda: GB, usage=broken).check("/нет")[0], OK)


class RecorderTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.processing = FakeProcessing()
        self.runs = []

    def make(self, probe, platform="twitch", restart=True, disk=None):
        runs = self.runs

        asked = self.asked = []

        class Fetcher:
            def session_started(self, platform, channel, began, target, *args, **options):
                asked.append(("start", os.path.basename(target)))

            def session_ended(self, platform, channel):
                asked.append(("end", channel))

        class Recorder(InstantRecorder):
            def _run_streamlink(self, url, recorded):
                runs.append(recorded)
                with open(recorded, "wb") as target:
                    target.write(b"\x47" * 188)
                return ""

        return Recorder(platform, "c", probe, "best", 15, self.root, self.processing,
                        live_restart=lambda: restart, disk=disk, beginning=Fetcher())

    def online(self, count):
        return [(Status.ONLINE, {"title": "эфир"})] * count

    def test_only_first_part_asks_for_the_beginning(self):
        # обрыв, эфир продолжается — вторая часть не просит начало эфира заново
        recorder = self.make(ScriptedProbe(*self.online(3), OFFLINE))
        with self.assertLogs(level="INFO"):
            recorder._iteration()
        self.assertEqual(len(self.runs), 3)
        self.assertEqual(self.asked[0][0], "start")
        self.assertTrue(self.asked[0][1].endswith(" - part00"))
        # конец эфира — YouTube и VK выкладывают запись после него
        self.assertEqual(self.asked[1:], [("end", "c")])

    def test_setting_off(self):
        recorder = self.make(ScriptedProbe(*self.online(1), OFFLINE), restart=False)
        with self.assertLogs(level="INFO"):
            recorder._iteration()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.asked, [("end", "c")])

    def test_no_room_no_recording(self):
        guard = DiskGuard(lambda: 5 * GB, usage=FakeUsage(GB // 2))
        recorder = self.make(ScriptedProbe(*self.online(1)), disk=guard)
        with self.assertLogs(level="ERROR"):
            recorder._iteration()
        self.assertEqual(self.runs, [])
        state = recorder.snapshot()
        self.assertEqual(state["state"], "unavailable")
        self.assertIn("освободи место", state["message"])

    def test_recording_stops_when_space_runs_out(self):
        usage = FakeUsage(100 * GB)
        guard = DiskGuard(lambda: 5 * GB, usage=usage)
        recorder = InstantRecorder("twitch", "c", ScriptedProbe(*self.online(5)), "best", 15,
                                   self.root, self.processing, live_restart=lambda: False, disk=guard)
        thread = threading.Thread(target=recorder._iteration)
        with mock.patch("streamvault.live.recorder.DISK_CHECK_INTERVAL", 0.2), \
                FakeStreamlink("forever"), self.assertLogs(level="INFO") as logs:
            thread.start()
            self.assertTrue(wait_for(lambda: recorder.snapshot()["state"] == "recording"))
            usage.free = GB // 10
            thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any("останавливаю запись" in line for line in logs.output))
        self.assertEqual(recorder.snapshot()["state"], "unavailable")
        self.assertEqual(len(self.processing.submitted), 1)    # записанное ушло в обработку


class RetentionTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.settings = Settings(os.path.join(self.root, "settings.json"),
                                 {"root_path": self.root, "keep_days": 30,
                                  "channels": [{"platform": "kick", "channel": "short", "keep_days": 2},
                                               {"platform": "twitch", "channel": "forever", "keep_days": 0}]})

    def recording(self, platform, label, days_ago, extension=".mp4"):
        folder = os.path.join(self.root, util.FINISHED_DIR, platform, label)
        os.makedirs(folder, exist_ok=True)
        moment = time.localtime(time.time() - days_ago * 86400)
        path = os.path.join(folder, "%s - %s - эфир%s" % (label, time.strftime("%Y-%m-%d %Hh%Mm%Ss", moment),
                                                          extension))
        open(path, "wb").close()
        return path

    def test_channel_days(self):
        self.assertEqual(channel_days(self.settings, "kick", "short"), 2)
        self.assertEqual(channel_days(self.settings, "twitch", "forever"), 0)
        self.assertEqual(channel_days(self.settings, "youtube", "other"), 30)

    def test_sweep_removes_old_with_chat(self):
        old = self.recording("kick", "short", 3)
        chat_json = os.path.splitext(old)[0] + ".chat.json"
        open(chat_json, "w").close()
        fresh = self.recording("kick", "short", 1)
        kept_forever = self.recording("twitch", "forever", 400)
        general_old = self.recording("youtube", "other", 31, ".ts")
        general_fresh = self.recording("youtube", "other", 29)
        self.assertEqual(sorted(path for path, _ in expired_recordings(self.settings)),
                         sorted([old, general_old]))

        removed = []

        def delete(path):
            from streamvault.archive import chat
            for target in [path, *chat.existing(path).values()]:
                removed.append(target)
                os.remove(target)

        with self.assertLogs(level="INFO"):
            Retention(self.settings, delete=delete).sweep()
        self.assertEqual(sorted(removed), sorted([old, chat_json, general_old]))
        for path in (fresh, kept_forever, general_fresh):
            self.assertTrue(os.path.exists(path))

    def test_off_by_default(self):
        self.settings.update(keep_days=0, channels=[])
        self.recording("kick", "x", 1000)
        self.assertEqual(expired_recordings(self.settings), [])

    def test_failed_delete_is_logged_not_raised(self):
        self.recording("youtube", "other", 100)

        def delete(path):
            raise OSError("корзина недоступна")

        with self.assertLogs(level="WARNING"):
            self.assertEqual(Retention(self.settings, delete=delete).sweep(), [])

    def test_channel_keep_days_normalized(self):
        channels = normalize_channels([{"platform": "kick", "channel": "a", "keep_days": "7"},
                                       {"platform": "kick", "channel": "b", "keep_days": -3},
                                       {"platform": "kick", "channel": "c", "keep_days": "много"}])
        self.assertEqual([entry.get("keep_days") for entry in channels], [7, 0, None])


if __name__ == "__main__":
    unittest.main()
