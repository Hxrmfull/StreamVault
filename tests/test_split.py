# -*- coding: utf-8 -*-
"""Деление записи на куски при конвертации в mp4 — вместе с чатом."""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import unittest.mock

from streamvault import util
from streamvault.library import broadcast_parts, parse_recording_name, probe_duration
from streamvault.live import livechat
from streamvault.live.processing import GB, Outcome, ProcessingQueue, piece_name, split_points
from streamvault.settings import Settings
from streamvault.ui.api import Api
from tests.test_ui import StubDownloads, StubLive

FFMPEG = shutil.which("ffmpeg")


class PointsTest(unittest.TestCase):

    def test_by_size_equal_pieces(self):
        # 4,1 ГБ и порог 2 ГБ — три равных куска, а не 2 + 2 + 0,1
        self.assertEqual(split_points("size", 3000, 4.1 * GB, size_gb=2), [1000.0, 2000.0])
        self.assertEqual(split_points("size", 3000, 4 * GB, size_gb=2), [1500.0])
        self.assertEqual(split_points("size", 3000, 1.9 * GB, size_gb=2), [])

    def test_by_duration_and_count(self):
        self.assertEqual(split_points("duration", 3 * 3600, 0, minutes=60), [3600, 7200])
        # хвост короче минуты прилипает к последнему куску
        self.assertEqual(split_points("duration", 2 * 3600 + 30, 0, minutes=60), [3600])
        self.assertEqual(split_points("count", 900, 0, count=3), [300.0, 600.0])

    def test_nothing_to_split(self):
        self.assertEqual(split_points("off", 9000, 10 * GB, size_gb=1), [])
        self.assertEqual(split_points("count", 100, 0, count=3), [])        # меньше двух минут
        self.assertEqual(split_points("count", None, 0, count=3), [])       # длительность не узнать
        # кусков не больше, чем минут в записи
        self.assertEqual(len(split_points("count", 180, 0, count=50)), 2)

    def test_names(self):
        self.assertEqual(piece_name("/r/chan - 2026-10-28 12h00m00s - эфир - part02.mp4", 1, 3),
                         "/r/chan - 2026-10-28 12h00m00s - эфир - part02 - piece 1 of 3.mp4")
        parsed = parse_recording_name("chan - 2026-10-28 12h00m00s - эфир - part02 - piece 3 of 3")
        self.assertEqual((parsed["title"], parsed["part"], parsed["piece"]), ("эфир", 2, 3))

    def test_order_of_parts_and_pieces(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        names = ["chan - 2026-10-28 12h00m00s - эфир - piece 2 of 2.mp4",
                 "chan - 2026-10-28 12h00m00s - эфир - part02.mp4",
                 "chan - 2026-10-28 12h00m00s - эфир - piece 1 of 2.mp4"]
        for name in names:
            open(os.path.join(folder, name), "wb").close()
        order = [os.path.basename(path) for _, path in broadcast_parts(os.path.join(folder, names[1]))]
        self.assertEqual(order, [names[2], names[0], names[1]])
        self.assertEqual([number for number, _ in broadcast_parts(os.path.join(folder, names[1]))], [1, 2, 3])

    def test_settings_from_window(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        settings = Settings(os.path.join(folder, "settings.json"), {"root_path": folder})
        api = Api(settings, StubLive(), StubDownloads(folder))
        self.addCleanup(api._media_server.stop)
        api.save_settings({"split_mode": "size", "split_size_gb": 2.5, "split_minutes": 90, "split_count": 3})
        self.assertEqual((settings["split_mode"], settings["split_size_gb"], settings["split_minutes"],
                          settings["split_count"]), ("size", 2.5, 90, 3))
        for bad in ({"split_mode": "pieces"}, {"split_size_gb": 0}, {"split_size_gb": "много"},
                    {"split_count": 1}, {"split_minutes": 2}):
            with self.assertRaises(ValueError, msg=str(bad)):
                api.save_settings(bad)


class Locator:
    def path(self):
        return FFMPEG


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class ConvertTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.mkdtemp()
        cls.sample = os.path.join(cls.samples, "sample.ts")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=180:s=160x90:r=25",
                        "-f", "lavfi", "-i", "sine=d=180", "-c:v", "libx264", "-preset", "ultrafast",
                        "-g", "25", "-c:a", "aac", "-shortest", "-f", "mpegts", cls.sample], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.recorded = os.path.join(self.root, util.IN_PROGRESS_DIR, "twitch", "chan",
                                     "chan - 2026-10-28 12h00m00s - эфир 100%.ts")
        self.processed = os.path.join(self.root, util.FINISHED_DIR, "twitch", "chan",
                                      os.path.basename(self.recorded))
        os.makedirs(os.path.dirname(self.recorded))
        shutil.copy(self.sample, self.recorded)
        self.done = []
        logs = self.assertLogs(level="INFO")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def queue(self, split):
        return ProcessingQueue(FFMPEG, False, threading.Event(), root_path=self.root, settle_seconds=0,
                               on_done=lambda recorded, target: self.done.append(target), split=lambda recorded: split)

    def test_split_into_equal_parts(self):
        self.assertEqual(self.queue(("count", 2.0, 60, 3))._process(self.recorded, self.processed), Outcome.DONE)
        self.assertFalse(os.path.exists(self.recorded))
        pieces = self.done[0]
        # «%» в имени ffmpeg принял бы за шаблон — имя должно дойти как есть
        self.assertEqual([os.path.basename(path) for path in pieces],
                         [f"chan - 2026-10-28 12h00m00s - эфир 100% - piece {n} of 3.mp4" for n in (1, 2, 3)])
        lengths = [probe_duration(FFMPEG, path) for path in pieces]
        self.assertAlmostEqual(sum(lengths), 180, delta=1)
        for length in lengths:
            self.assertAlmostEqual(length, 60, delta=2)
        self.assertFalse([name for name in os.listdir(os.path.dirname(pieces[0])) if name.endswith(".part")])

    def test_small_recording_is_not_split(self):
        self.queue(("size", 2.0, 60, 3))._process(self.recorded, self.processed)
        self.assertTrue(self.done[0].endswith("эфир 100%.mp4"))

    def test_chat_is_split_too(self):
        start = 1000.0
        with open(livechat.jsonl_path(self.recorded), "w", encoding="utf-8") as target:
            for record in [{"type": "start", "ts": start, "platform": "kick", "channel": "chan"}] + [
                    {"type": "msg", "ts": start + offset, "id": str(offset), "name": "З", "parts": [["м%d" % offset]]}
                    for offset in (10, 70, 140)] + [{"type": "end", "ts": start + 180}]:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.queue(("count", 2.0, 60, 2))._process(self.recorded, self.processed)
        pieces = self.done[0]
        self.assertEqual(len(pieces), 2)
        settings = Settings(os.path.join(self.root, "settings.json"), {"root_path": self.root})
        finisher = livechat.ChatFinisher(settings, Locator(), Locator(), threading.Event())
        with unittest.mock.patch.object(finisher, "_enqueue"):
            finisher.finish(self.recorded, pieces)
        offsets = []
        for piece in pieces:
            with open(os.path.splitext(piece)[0] + ".chat.json", encoding="utf-8") as source:
                offsets.append([c["content_offset_seconds"] for c in json.load(source)["comments"]])
        first = probe_duration(FFMPEG, pieces[0])
        self.assertEqual(offsets[0], [10.0, 70.0])
        self.assertEqual([round(value) for value in offsets[1]], [round(140 - first)])
        self.assertFalse(os.path.exists(livechat.jsonl_path(self.recorded)))
        self.assertFalse([name for name in os.listdir(os.path.dirname(self.recorded)) if "whole" in name])


if __name__ == "__main__":
    unittest.main()
