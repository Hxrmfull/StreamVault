# -*- coding: utf-8 -*-
"""
Запись с начала эфира: начало — из записи эфира на площадке, первой частью
«part00». Площадки и очередь загрузок подменяются.
"""
import datetime
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from streamvault import sites
from streamvault.library import broadcast_parts
from streamvault.live import beginning
from streamvault.live.beginning import OVERLAP, Beginning, download_quality
from streamvault.util import build_recording_path
from tests.helpers import wait_for
from tests.test_search import answer
from tests.test_sites import REQUEST, fresh_memos


class FakeDownloads:
    def __init__(self):
        self.added = []
        self.done = threading.Event()

    def add(self, url, **options):
        self.added.append((url, options))
        self.done.set()
        return "job1"


class BeginningTest(unittest.TestCase):

    def setUp(self):
        self.downloads = FakeDownloads()
        self.fetcher = Beginning(self.downloads)
        self.addCleanup(self.fetcher.stop)
        self.reports = []
        patcher = mock.patch.multiple(beginning, LIVE_RETRIES=(0, 0), ENDED_CHECK=0.05)
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, platform, began=10000, started=1000, url="https://archive/1", reason="", expect=True):
        with mock.patch.object(sites, "stream_started", return_value=started), \
                mock.patch.object(sites, "archive_for", return_value=(url, reason)) as archive:
            self.fetcher.session_started(platform, "chan", began, "D:/rec/chan - part00", "эфир", "720p60",
                                         "text", meta={"id": "abc"}, report=self.reports.append)
            if sites.beginning_when(platform) == "ended":
                self.assertFalse(self.downloads.added)        # до конца эфира — ничего
                self.fetcher.session_ended(platform, "chan")
            if expect:
                wait_for(lambda: self.reports and self.reports[-1]["state"] in ("queued", "unavailable"), timeout=5)
        return archive

    def test_live_platform_queues_right_away(self):
        archive = self.start("twitch")
        url, options = self.downloads.added[0]
        self.assertEqual(url, "https://archive/1")
        # от начала эфира до начала записи и чуть дальше — без дыры между частями
        self.assertEqual(options["section"], (0, 9000 + OVERLAP))
        self.assertEqual(options["target"], "D:/rec/chan - part00")
        self.assertEqual((options["quality"], options["chat_mode"]), ("720", "text"))
        self.assertEqual((options["meta"]["kind"], options["meta"]["live_status"]), ("vod", "was_live"))
        self.assertEqual(archive.call_args.args[:3], ("twitch", "chan", 1000))
        self.assertEqual([report["state"] for report in self.reports], ["searching", "queued"])
        self.assertEqual(self.reports[-1]["length"], 9000)

    def test_ended_platform_waits_for_the_end(self):
        self.start("youtube")
        self.assertEqual(self.downloads.added[0][1]["meta"]["kind"], "video")
        self.assertEqual([report["state"] for report in self.reports], ["waiting", "queued"])

    def test_short_gap_and_unknown_start(self):
        self.start("kick", began=1050, started=1000, expect=False)   # запись почти с начала — добирать нечего
        self.assertEqual((self.downloads.added, self.reports), ([], []))
        self.start("kick", started=None, expect=False)
        self.assertEqual(self.reports[-1]["state"], "unavailable")
        self.assertFalse(self.downloads.added)

    def test_no_archive_explained(self):
        with self.assertLogs(level="WARNING") as logs:
            self.start("twitch", url=None, reason="у канала выключен архив прошедших эфиров")
        self.assertFalse(self.downloads.added)
        self.assertEqual(self.reports[-1]["state"], "unavailable")
        self.assertIn("выключен архив", self.reports[-1]["reason"])
        self.assertIn("начало эфира не взять", logs.output[0])

    def test_quality(self):
        self.assertEqual(download_quality("1080p60"), "1080")
        self.assertEqual(download_quality("160p30,best"), "160")
        self.assertEqual(download_quality("best"), "best")
        self.assertEqual(download_quality("audio_only"), "audio")


class RecorderHookTest(unittest.TestCase):

    def test_first_part_asks_for_beginning(self):
        from streamvault.live.recorder import ChannelRecorder
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        calls = []

        class Fetcher:
            def session_started(self, *args, **options):
                calls.append((args, options))

        wanted = {"on": True}
        probe = mock.Mock()
        probe.name = "streamlink"
        recorder = ChannelRecorder("kick", "chan", probe, "720p", 15, root, None,
                                   live_chat=lambda: "text", live_restart=lambda: wanted["on"], beginning=Fetcher())
        started = datetime.datetime(2026, 10, 31, 12, 0, 0)
        recorder._fetch_beginning(5000.0, "эфир", started)
        (platform, channel, began, target, title, quality, chat_mode), options = calls[0]
        self.assertEqual((platform, channel, began, title, quality, chat_mode), ("kick", "chan", 5000.0, "эфир", "720p", "text"))
        self.assertTrue(target.endswith("chan - 2026-10-31 12h00m00s - эфир - part00"))
        self.assertEqual(os.path.dirname(target), recorder.processed_path)
        options["report"]({"state": "queued"})
        self.assertEqual(recorder.snapshot()["beginning"], {"state": "queued"})
        wanted["on"] = False
        recorder._fetch_beginning(5000.0, "эфир", started)
        self.assertEqual(len(calls), 1)                      # настройка выключена — не добирать


class PartZeroTest(unittest.TestCase):

    def test_beginning_goes_first(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        moment = datetime.datetime(2026, 10, 31, 12, 0, 0)
        names = [build_recording_path(folder, "chan", "эфир", moment, part, ".mp4") for part in (2, 1, 0)]
        for name in names:
            open(name, "wb").close()
        self.assertTrue(names[2].endswith(" - part00.mp4"))
        self.assertTrue(names[1].endswith(" - эфир.mp4"))            # первая часть — без пометки
        self.assertEqual([path for _, path in broadcast_parts(names[1])], [names[2], names[1], names[0]])


class SitesTest(unittest.TestCase):

    def setUp(self):
        for patcher in fresh_memos():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_twitch(self):
        live = {"data": {"user": {"stream": {"createdAt": "2026-10-31T06:16:30Z",
                                             "archiveVideo": {"id": "2915548556"}}}}}
        with mock.patch(REQUEST, return_value=answer(200, live)):
            self.assertEqual(sites.stream_started("twitch", "vulpeshd"), 1793427390.0)
            self.assertEqual(sites.archive_for("twitch", "vulpeshd", 1793427390.0),
                             ("https://www.twitch.tv/videos/2915548556", ""))
        off = {"data": {"user": {"stream": {"createdAt": "2026-10-29T22:17:47Z", "archiveVideo": None}}}}
        with mock.patch(REQUEST, return_value=answer(200, off)):
            url, reason = sites.archive_for("twitch", "mira", 1.0)
        self.assertIsNone(url)
        self.assertIn("архив", reason)
        self.assertEqual(sites.beginning_when("twitch"), "live")

    def test_kick(self):
        def respond(method, url, **kwargs):
            if "web.kick.com" in url:
                return answer(200, {"data": [{"id": "vod-1", "is_live": True, "start_time": "2026-10-31T08:22:30Z"}]})
            return answer(200, {"slug": "sus", "id": 5, "livestream": {"start_time": "2026-10-31 08:22:30"}})

        with mock.patch(REQUEST, side_effect=respond):
            started = sites.stream_started("kick", "sus")
            self.assertEqual(started, 1793434950.0)
            self.assertEqual(sites.archive_for("kick", "sus", started), ("https://kick.com/sus/videos/vod-1", ""))

    def test_vk_and_youtube_after_end(self):
        self.assertEqual((sites.beginning_when("vkvideolive"), sites.beginning_when("youtube")), ("ended", "ended"))
        with mock.patch(REQUEST, return_value=answer(200, {"isOnline": True, "startTime": 1793000000})):
            self.assertEqual(sites.stream_started("vkvideolive", "blog"), 1793000000.0)
        records = {"data": {"records": [{"id": "r1", "startTime": 1793000004, "processState": "done"}]}}
        with mock.patch("streamvault.archive.vkrecords.requests.get", return_value=answer(200, records)):
            self.assertEqual(sites.archive_for("vkvideolive", "blog", 1793000000.0),
                             ("https://live.vkvideo.ru/blog/record/r1", ""))

        class Tools:
            status = "is_live"

            def ytdlp_json(self, args, timeout):
                return {"release_timestamp": 1793000000, "live_status": self.status}

        tools = Tools()
        meta = {"id": "abcdefghijk"}
        self.assertEqual(sites.stream_started("youtube", "c", meta, tools), 1793000000.0)
        self.assertIn("готовит", sites.archive_for("youtube", "c", 1, meta, tools)[1])
        tools.status = "was_live"
        self.assertEqual(sites.archive_for("youtube", "c", 1, meta, tools),
                         ("https://www.youtube.com/watch?v=abcdefghijk", ""))


if __name__ == "__main__":
    unittest.main()
