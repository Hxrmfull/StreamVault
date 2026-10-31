# -*- coding: utf-8 -*-
import datetime
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from streamvault import util
from streamvault.ffmpeg import FFmpegLocator
from streamvault.library import Library, parse_download_name, parse_recording_name
from streamvault.settings import Settings
from tests.test_processing import FFMPEG, make_sample


class FakeDownloads:
    def __init__(self, root, jobs=()):
        self.root = root
        self.jobs = list(jobs)

    def downloads_dir(self):
        return os.path.join(self.root, "downloads")

    def snapshot(self):
        return {"jobs": self.jobs}


class NamesTest(unittest.TestCase):

    def test_recording_name(self):
        parsed = parse_recording_name("xqc - 2026-10-25 12h30m05s - Стрим - про - тире - part02")
        self.assertEqual(parsed["title"], "Стрим - про - тире")
        self.assertEqual(parsed["part"], 2)
        self.assertEqual(datetime.datetime.fromtimestamp(parsed["timestamp"]),
                         datetime.datetime(2026, 10, 25, 12, 30, 5))
        self.assertEqual(parse_recording_name("xqc - 2026-10-25 12h30m05s")["title"], "")
        self.assertEqual(parse_recording_name("что-то своё"), {})

    def test_download_name(self):
        parsed = parse_download_name("xqc - 2026-10-24 - GTA [v2909822254] 720 (02h00m00s-02h01m00s)")
        self.assertEqual((parsed["title"], parsed["video_id"]), ("GTA", "v2909822254"))


class DurationTest(unittest.TestCase):
    """Длительность из того, что печатает ffmpeg, открыв файл."""

    def duration(self, header):
        stub = types.SimpleNamespace(
            _ffmpeg=lambda args, timeout: types.SimpleNamespace(stderr=header.encode("utf-8")))
        return Library._probe_duration(stub, "x.ts")

    def test_twitch_mp4_fragments_in_ts(self):
        # настоящий вывод для записи strogo: полторы минуты, время — от начала трансляции
        self.assertAlmostEqual(self.duration(
            "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.ts':\n"
            "  Duration: 07:55:37.87, start: 28448.033000, bitrate: 0 kb/s\n"), 89.837, places=2)

    def test_ordinary_files(self):
        self.assertEqual(self.duration("Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'x.mp4':\n"
                                       "  Duration: 00:00:02.00, start: 0.200000, bitrate: 30 kb/s\n"), 2.0)
        self.assertEqual(self.duration("Input #0, mpegts, from 'x.ts':\n"
                                       "  Duration: 00:01:30.00, start: 28448.000000, bitrate: 200 kb/s\n"), 90.0)
        self.assertIsNone(self.duration("x.ts: Invalid data found when processing input\n"))


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class LibraryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.mkdtemp()
        cls.sample = os.path.join(cls.samples, "sample.mp4")
        ts = os.path.join(cls.samples, "sample.ts")
        make_sample(ts, seconds=3)
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-i", ts, "-c", "copy", cls.sample],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.root, "settings.json"), {"root_path": self.root})
        self.record = self.put(os.path.join(util.FINISHED_DIR, "twitch", "xqc"),
                               "xqc - 2026-10-25 12h00m00s - вечерний стрим - part02.mp4")
        self.download = self.put(os.path.join("downloads", "kick", "xqc"),
                                 "xqc - 2026-10-24 - клип [c1].mp4")
        # недоделанное в библиотеку не попадает
        for name in ("x.mp4.part", "x.f137.mp4", "x.temp.mp4", "x.mp4.ytdl", "notes.txt"):
            self.put(os.path.join("downloads", "kick", "xqc"), name)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def put(self, folder, name):
        directory = os.path.join(self.root, folder)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        shutil.copy(self.sample, path)
        return path

    def library(self, jobs=()):
        downloads = FakeDownloads(self.root, jobs)
        locator = FFmpegLocator(self.settings, os.path.join(self.root, "tools"))
        return Library(self.settings, downloads, os.path.join(self.root, "cache"), locator)

    def test_lists_records_and_downloads(self):
        items = self.library().items()
        self.assertEqual([item["name"] for item in items],
                         [os.path.basename(self.record), os.path.basename(self.download)])
        record, download = items
        self.assertEqual((record["kind"], record["platform"], record["channel_id"], record["part"]),
                         ("record", "twitch", "twitch:xqc", 2))
        self.assertEqual(record["title"], "вечерний стрим")
        self.assertEqual((download["kind"], download["title"]), ("download", "клип"))
        # части одного эфира узнаются по каналу и времени начала: окно собирает их в карточку
        first = self.put(os.path.join(util.FINISHED_DIR, "twitch", "xqc"),
                         "xqc - 2026-10-25 12h00m00s - другое название.mp4")
        keys = {item["path"]: item["broadcast"] for item in self.library().items()}
        self.assertTrue(keys[first])
        self.assertEqual(keys[first], keys[self.record])
        self.assertEqual(keys[self.download], "")
        # клип и видео с чатом, сделанные в окне, — не части эфира, сведения — как у исходника
        clip = self.put(os.path.join(util.FINISHED_DIR, "twitch", "xqc"),
                        "xqc - 2026-10-25 12h00m00s - другое название [clip 00h00m01s-00h00m03s].mp4")
        items = {item["path"]: item for item in self.library().items()}
        self.assertEqual((items[clip]["edit"], items[clip]["broadcast"], items[clip]["video_kind"]),
                         ("clip", "", "clip"))
        self.assertEqual(items[clip]["title"], "другое название")

    def test_download_takes_details_from_queue(self):
        job = {"file": self.download, "title": "Настоящее название", "channel": "xQc",
               "thumbnail": "https://example.com/t.jpg", "url": "https://kick.com/x",
               "kind": "clip", "timestamp": 1792638498, "duration": 54, "section": None}
        download = [item for item in self.library([job]).items() if item["kind"] == "download"][0]
        self.assertEqual((download["title"], download["channel"], download["video_kind"]),
                         ("Настоящее название", "xQc", "clip"))
        self.assertEqual((download["thumbnail"], download["duration"]),
                         ("https://example.com/t.jpg", 54))

    def test_preview_is_made_once(self):
        library = self.library()
        preview = library.preview(self.record)
        self.assertTrue(preview["thumbnail"].startswith("data:image/jpeg;base64,"))
        self.assertAlmostEqual(preview["duration"], 3.0, delta=0.5)
        # длительность теперь в списке, а повторное превью — из кэша, без ffmpeg
        self.assertAlmostEqual(library.items()[0]["duration"], 3.0, delta=0.5)
        with mock.patch.object(Library, "_ffmpeg", side_effect=AssertionError("ffmpeg снова")):
            self.assertEqual(self.library().preview(self.record), preview)

    def test_old_ts_duration_is_probed_again(self):
        # в кэше прошлой версии у .ts могла остаться длительность от начала трансляции
        ts = os.path.join(os.path.dirname(self.record), "xqc - 2026-10-25 13h00m00s.ts")
        shutil.copy(os.path.join(self.samples, "sample.ts"), ts)
        library = self.library()
        library.preview(ts)
        key = library._key(ts, os.stat(ts))
        library._cache[key].update(duration=28537.0, probe=None)
        listed = [item for item in library.items() if item["path"] == os.path.abspath(ts)]
        self.assertIsNone(listed[0]["duration"])   # окно возьмёт длительность из превью
        self.assertAlmostEqual(library.preview(ts)["duration"], 3.0, delta=0.5)
        with mock.patch.object(Library, "_ffmpeg", side_effect=AssertionError("ffmpeg снова")):
            self.assertAlmostEqual(library.preview(ts)["duration"], 3.0, delta=0.5)

    def test_audio_only_has_no_frame(self):
        audio = os.path.join(os.path.dirname(self.download), "звук.m4a")
        subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1",
                        "-c:a", "aac", audio], check=True)
        preview = self.library().preview(audio)
        self.assertEqual(preview["thumbnail"], "")
        self.assertAlmostEqual(preview["duration"], 1.0, delta=0.3)

    def test_delete_goes_to_recycle_bin(self):
        with mock.patch("streamvault.util.send_to_recycle_bin") as recycle, \
                self.assertLogs(level="INFO"):
            self.library().delete(self.download)
        recycle.assert_called_once_with(self.download)

    def chat_files(self):
        base = os.path.splitext(self.download)[0]
        files = {"html": base + ".chat.html", "json": base + ".chat.json", "video": base + ".chat.mp4"}
        for path in files.values():
            shutil.copy(self.sample, path)
        return files

    def test_chat_belongs_to_its_video(self):
        files = self.chat_files()
        items = self.library().items()
        # видео чата — не отдельная запись библиотеки
        self.assertEqual(len(items), 2)
        download = [item for item in items if item["kind"] == "download"][0]
        self.assertEqual(download["chat"], files)
        record = [item for item in items if item["kind"] == "record"][0]
        self.assertEqual(record["chat"], {})

    def test_delete_takes_chat_along(self):
        files = self.chat_files()
        with mock.patch("streamvault.util.send_to_recycle_bin") as recycle, \
                self.assertLogs(level="INFO"):
            self.library().delete(self.download)
        self.assertEqual(sorted(call.args[0] for call in recycle.call_args_list),
                         sorted([self.download, *files.values()]))


if __name__ == "__main__":
    unittest.main()
