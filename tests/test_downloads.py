# -*- coding: utf-8 -*-
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from streamvault.archive import downloads as dl
from streamvault.events import EventBus
from streamvault.settings import Settings
from tests.helpers import EventLog, FakeYtdlp, wait_for

PLAYLIST = {"_type": "playlist", "extractor_key": "TwitchVideos", "title": "список",
            "entries": [{"id": "1", "title": "a", "ie_key": "TwitchVod"},
                        {"id": "2", "title": "b", "ie_key": "TwitchVod"}]}


class DownloadTestCase(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "streams")
        self.log_path = os.path.join(self.dir, "ytdlp.log")
        self.settings = Settings(os.path.join(self.dir, "settings.json"),
                                 {"root_path": self.root})
        self.bus = EventBus()
        self.events = EventLog(self.bus)
        self.managers = []
        logs = self.assertLogs(level="INFO")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def manager(self, resume=True, start=True):
        manager = dl.DownloadManager(self.settings, bus=self.bus, resume=resume)
        self.managers.append(manager)
        if start:
            manager.start()
        return manager

    def fake(self, mode="ok", info=None):
        info_path = None
        if info is not None:
            info_path = os.path.join(self.dir, "info.json")
            with open(info_path, "w", encoding="utf-8") as target:
                json.dump(info, target)
        return FakeYtdlp(mode, info=info_path, log=self.log_path)

    def calls(self):
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as source:
            return [json.loads(line) for line in source]

    def wait_state(self, manager, job_id, *states, timeout=15):
        ok = wait_for(lambda: manager.job(job_id)["state"] in states, timeout=timeout)
        self.assertTrue(ok, "загрузка в состоянии %s, ждали %s"
                        % (manager.job(job_id)["state"], states))
        return manager.job(job_id)

    def files(self):
        found = []
        for folder, _, names in os.walk(os.path.join(self.root, "downloads")):
            found.extend(os.path.join(folder, name) for name in names)
        return found


class ChannelListTest(unittest.TestCase):

    def test_empty_page_is_asked_again(self):
        # Twitch иногда отвечает пустой страницей там, где записи есть
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        manager = dl.DownloadManager(Settings(os.path.join(directory, "settings.json"),
                                              {"root_path": directory}), resume=False)
        full = {"type": "playlist", "entries": [{"channel": "", "url": "https://www.twitch.tv/videos/1"}]}
        answers = [{"type": "playlist", "entries": []}, full]
        with mock.patch.object(manager, "inspect", side_effect=lambda *a, **k: answers.pop(0)) as inspect:
            result = manager.list_channel("twitch", "recrent")
        self.assertEqual(inspect.call_count, 2)
        self.assertEqual(result["entries"][0]["channel"], "recrent")
        # список и правда пуст — после нескольких попыток так и говорим
        with mock.patch.object(manager, "inspect", return_value={"type": "playlist", "entries": []}) as inspect:
            self.assertEqual(manager.list_channel("twitch", "nobody")["entries"], [])
        self.assertEqual(inspect.call_count, dl.EMPTY_PAGE_ATTEMPTS)
        # у канала YouTube нет вкладки трансляций — это пустой список, а не ошибка
        missing = dl.ytdlp.YtdlpError("[youtube:tab] @xQcOW: This channel does not have a streams tab")
        with mock.patch.object(manager, "inspect", side_effect=missing):
            self.assertEqual(manager.list_channel("youtube", "xQcOW")["entries"], [])
        with mock.patch.object(manager, "inspect", side_effect=dl.ytdlp.YtdlpError("нет связи")):
            with self.assertRaises(dl.ytdlp.YtdlpError):
                manager.list_channel("youtube", "xQcOW")


class DownloadFlowTest(DownloadTestCase):

    def test_downloads_and_names_file(self):
        with self.fake("ok"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            job = self.wait_state(manager, job_id, dl.DONE)

        self.assertEqual(os.path.dirname(job["file"]),
                         os.path.join(self.root, "downloads", "twitch", "streamer"))
        name = os.path.basename(job["file"])
        self.assertRegex(name, r"^streamer - 2026-10-2\d - Тест \{x\} 100% \[v123\]\.mp4$")
        self.assertEqual(os.path.getsize(job["file"]), 3000)
        self.assertEqual(job["size"], 3000)       # «Готово · 2,9 КБ» и после перезапуска
        self.assertEqual(job["progress"]["percent"], 100.0)
        self.assertEqual((job["title"], job["kind"]), ("Тест {x} 100%", "vod"))

        states = [event["job"]["state"] for event in self.events.of("download")]
        for expected in (dl.QUEUED, dl.DOWNLOADING, dl.PROCESSING, dl.DONE):
            self.assertIn(expected, states)
        with open(os.path.join(self.dir, "downloads.json"), encoding="utf-8") as source:
            self.assertEqual(json.load(source)[0]["state"], dl.DONE)

    def test_fixed_target(self):
        # начало эфира кладётся рядом с записью эфира, а не в загрузки
        target = os.path.join(self.root, "finished", "twitch", "x", "x - 2026-10-31 12h00m00s - эфир - part00")
        with self.fake("ok"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123", section=(0, 60), target=target)
            job = self.wait_state(manager, job_id, dl.DONE)
        self.assertEqual(job["file"], target + ".mp4")
        self.assertTrue(os.path.exists(job["file"]))

    def test_known_meta_skips_inspect(self):
        meta = {"id": "c1", "title": "клип", "channel": "LegendaAFK", "platform": "twitch",
                "kind": "clip", "duration": 30, "timestamp": 1656873059}
        with self.fake("ok"):
            manager = self.manager()
            job_id = manager.add("https://clips.twitch.tv/x", quality="720",
                                 section=(5, 20), meta=meta)
            job = self.wait_state(manager, job_id, dl.DONE)
        calls = self.calls()
        self.assertEqual([call["kind"] for call in calls], ["download"])
        args = calls[0]["args"]
        self.assertEqual(args[args.index("-f") + 1],
                         "bv*[height<=720]+ba/b[height<=720]/wv*+ba/w")
        self.assertIn("*5.0-20.0", args)
        self.assertIn(os.path.join("twitch", "legendaafk"), job["file"])
        self.assertTrue(job["file"].endswith("[c1] 720 (00h00m05s-00h00m20s).mp4"))

    def test_updated_yt_dlp_is_used(self):
        # обновлённый кнопкой yt-dlp новее встроенного — его и запускают
        own = os.path.join(self.dir, "tools", "yt-dlp")
        os.makedirs(own)
        copy = os.path.join(own, "yt-dlp-9999.1.1.zip")
        open(copy, "wb").close()
        with open(os.path.join(own, "version.json"), "w", encoding="utf-8") as record:
            json.dump({"version": "9999.1.1", "file": os.path.basename(copy)}, record)
        with self.fake("ok"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            self.wait_state(manager, job_id, dl.DONE)
        self.assertEqual([(call["kind"], call["copy"]) for call in self.calls()],
                         [("inspect", copy), ("download", copy)])

    def test_bundled_yt_dlp_without_update(self):
        with self.fake("ok"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            self.wait_state(manager, job_id, dl.DONE)
        self.assertEqual({call["copy"] for call in self.calls()}, {None})

    def test_error_text_reaches_job(self):
        with self.fake("fail"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            job = self.wait_state(manager, job_id, dl.FAILED)
        self.assertIn("only available to subscribers", job["error"])

    def test_dead_link(self):
        with self.fake("inspect_fail"):
            manager = self.manager()
            job = self.wait_state(manager, manager.add("https://x.tv/1"), dl.FAILED)
        self.assertIn("Video does not exist", job["error"])

    def test_no_file_is_failure(self):
        with self.fake("nofile"):
            manager = self.manager()
            job = self.wait_state(manager, manager.add("https://x.tv/1"), dl.FAILED)
        self.assertTrue(job["error"])

    def test_playlist_link_is_refused(self):
        with self.fake("ok", info=PLAYLIST):
            manager = self.manager()
            job = self.wait_state(manager, manager.add("https://x.tv/list"), dl.FAILED)
        self.assertIn("список из 2 видео", job["error"])

    def test_section_after_end(self):
        # было: клип на 4 секунды с отрезком 2:00:00-2:01:00 давал пустышку как "скачано"
        manager = self.manager()
        with self.assertRaises(ValueError):
            manager.add("https://x.tv/clip", section=(7200, 7260),
                        meta={"id": "c", "duration": 4})
        with self.fake("ok"):
            job_id = manager.add("https://www.twitch.tv/videos/123", section=(4000, 4100))
            job = self.wait_state(manager, job_id, dl.FAILED)
        self.assertIn("после конца записи", job["error"])
        self.assertEqual(self.files(), [])


class ControlTest(DownloadTestCase):

    def test_pause_keeps_partial_and_resume_continues(self):
        with self.fake("slow") as fake:
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            self.assertTrue(wait_for(lambda: (manager.job(job_id)["progress"] or {})
                                     .get("downloaded", 0) > 2000))
            manager.pause(job_id)
            self.wait_state(manager, job_id, dl.PAUSED)
            partials = [path for path in self.files() if path.endswith(".part")]
            self.assertEqual(len(partials), 1)

            fake.set_mode("ok")
            manager.resume(job_id)
            job = self.wait_state(manager, job_id, dl.DONE)
        self.assertTrue(self.calls()[-1]["resumed"])
        self.assertGreater(os.path.getsize(job["file"]), 3000)
        self.assertEqual(self.files(), [job["file"]])

    def test_cancel_removes_partial(self):
        with self.fake("slow"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            wait_for(lambda: manager.job(job_id)["progress"])
            manager.cancel(job_id)
            job = self.wait_state(manager, job_id, dl.CANCELLED)
        self.assertEqual(self.files(), [])
        self.assertEqual(job["target"], "")

    def test_remove_running(self):
        with self.fake("slow"):
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            wait_for(lambda: manager.job(job_id)["progress"])
            manager.remove(job_id)
            self.assertTrue(wait_for(lambda: not manager.snapshot()["jobs"]))
        self.assertEqual(self.events.of("download_removed")[-1]["id"], job_id)
        self.assertEqual(self.files(), [])

    def test_limit_of_simultaneous_downloads(self):
        self.settings.update(max_downloads=1)
        with self.fake("slow"):
            manager = self.manager()
            first = manager.add("https://www.twitch.tv/videos/1")
            second = manager.add("https://www.twitch.tv/videos/2")
            self.wait_state(manager, first, dl.DOWNLOADING)
            wait_for(lambda: False, timeout=1)
            self.assertEqual(manager.job(second)["state"], dl.QUEUED)
            manager.cancel(first)
            self.wait_state(manager, second, dl.DOWNLOADING)

    def test_same_link_is_not_added_twice(self):
        with self.fake("ok"):
            manager = self.manager()
            first = manager.add("https://www.twitch.tv/videos/123")
            self.assertEqual(manager.add("https://www.twitch.tv/videos/123"), first)
            self.wait_state(manager, first, dl.DONE)
            # уже скачано — возвращается готовое
            self.assertEqual(manager.add("https://www.twitch.tv/videos/123"), first)
            # другое качество — другая загрузка
            other = manager.add("https://www.twitch.tv/videos/123", quality="720")
            self.assertNotEqual(other, first)
            # заново по требованию — рядом, с "(2)" в имени
            again = manager.add("https://www.twitch.tv/videos/123", force=True)
            job = self.wait_state(manager, again, dl.DONE)
        self.assertTrue(job["file"].endswith("[v123] (2).mp4"))

    def test_interrupted_download_continues_after_restart(self):
        with self.fake("slow") as fake:
            manager = self.manager()
            job_id = manager.add("https://www.twitch.tv/videos/123")
            wait_for(lambda: (manager.job(job_id)["progress"] or {}).get("downloaded"))
            manager.shutdown(timeout=10)
            self.managers.remove(manager)

            # консольная команда не продолжает чужое сама
            idle = self.manager(resume=False, start=False)
            self.assertEqual(idle.job(job_id)["state"], dl.PAUSED)

            fake.set_mode("ok")
            restarted = self.manager(resume=True)
            job = self.wait_state(restarted, job_id, dl.DONE)
        self.assertTrue(self.calls()[-1]["resumed"])
        self.assertTrue(os.path.exists(job["file"]))


if __name__ == "__main__":
    unittest.main()


class DownloadAllTest(DownloadTestCase):
    """«Скачать всё» с вкладки канала: только то, чего ещё нет."""

    def entries(self, first, last):
        return [{"id": "v%d" % number, "url": "https://www.twitch.tv/videos/%d" % number,
                 "title": "запись %d" % number, "platform": "twitch", "kind": "vod", "channel": "chan",
                 "duration": 60, "timestamp": 1700000000 + number}
                for number in range(first, last + 1)]

    def test_pages_and_known_videos_are_skipped(self):
        manager = self.manager(start=False)
        pages = []

        def list_channel(platform, channel, kind="vods", limit=30, start=1, period="all"):
            pages.append((start, limit))
            everything = self.entries(1, 150)
            return {"entries": everything[start - 1:start - 1 + limit]}

        manager.list_channel = list_channel
        # одна уже в очереди, другая скачана раньше и лежит файлом
        manager.add("https://www.twitch.tv/videos/1", meta=self.entries(1, 1)[0])
        folder = os.path.join(manager.downloads_dir(), "twitch", "chan")
        os.makedirs(folder)
        open(os.path.join(folder, "chan - 2026-10-20 - запись 2 [v2].mp4"), "wb").close()
        # отрезок видео — ещё не всё видео
        open(os.path.join(folder, "chan - 2026-10-20 - запись 3 [v3] (0-1_00).mp4"), "wb").close()

        result = manager.download_all("twitch", "chan", "vods")
        self.assertEqual(pages, [(1, 100), (101, 100)])
        self.assertEqual(result, {"added": 148, "known": 2, "total": 150})
        urls = [job["url"] for job in manager.snapshot()["jobs"]]
        self.assertEqual(len(urls), 149)
        self.assertNotIn("https://www.twitch.tv/videos/2", urls)
        job = next(job for job in manager.snapshot()["jobs"] if job["url"].endswith("/150"))
        self.assertEqual((job["title"], job["video_id"]), ("запись 150", "v150"))

        # второй раз — ничего нового
        self.assertEqual(manager.download_all("twitch", "chan", "vods")["added"], 0)

    def test_cancelled_is_downloaded_again(self):
        manager = self.manager(start=False)
        manager.list_channel = lambda *args, **kwargs: {"entries": self.entries(1, 1)}
        job_id = manager.add("https://www.twitch.tv/videos/1", meta=self.entries(1, 1)[0])
        manager.cancel(job_id)
        self.assertEqual(manager.download_all("twitch", "chan")["added"], 1)
