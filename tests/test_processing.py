# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from streamvault import util
from streamvault.events import EventBus
from streamvault.live.processing import Outcome, ProcessingQueue
from tests.helpers import EventLog, wait_for

FFMPEG = shutil.which("ffmpeg")


def make_sample(path, seconds=1):
    """Короткая настоящая запись в MPEG-TS, как её пишет streamlink."""
    subprocess.run([FFMPEG, "-nostdin", "-v", "error", "-f", "lavfi",
                    "-i", "testsrc=duration=%s:size=160x120:rate=10" % seconds,
                    "-c:v", "mpeg2video", "-f", "mpegts", path], check=True)


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class ProcessingTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.mkdtemp()
        cls.sample = os.path.join(cls.samples, "sample.ts")
        make_sample(cls.sample)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.recorded_dir = os.path.join(self.root, util.IN_PROGRESS_DIR, "twitch", "c")
        self.processed_dir = os.path.join(self.root, util.FINISHED_DIR, "twitch", "c")
        os.makedirs(self.recorded_dir)
        os.makedirs(self.processed_dir)
        self.source = os.path.join(self.recorded_dir, "rec.ts")
        self.target = os.path.join(self.processed_dir, "rec.ts")
        shutil.copy(self.sample, self.source)
        self.bus = EventBus()
        self.events = EventLog(self.bus)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def queue(self, ffmpeg=None, disable_ffmpeg=False, stop=None):
        return ProcessingQueue(ffmpeg or FFMPEG, disable_ffmpeg, stop or threading.Event(),
                               root_path=self.root, bus=self.bus)

    def fake_ffmpeg(self):
        """ffmpeg, который успевает написать мусор и падает, как при обрыве."""
        script = os.path.join(self.root, "fake_ffmpeg.py")
        with open(script, "w", encoding="utf-8") as target:
            target.write("import sys\nopen(sys.argv[-1], 'wb').write(b'broken')\n"
                         "sys.exit(1)\n")
        launcher = os.path.join(self.root, "fake_ffmpeg.cmd")
        with open(launcher, "w", encoding="utf-8") as target:
            target.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
        return launcher

    def test_converts_to_mp4(self):
        queue = self.queue()
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="INFO"):
            outcome = queue._process(self.source, self.target)
        self.assertIs(outcome, Outcome.DONE)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.mp4"])
        self.assertFalse(os.path.exists(self.source))
        actions = [event["action"] for event in self.events.of("processing")]
        self.assertEqual(actions, ["queued", "started", "done"])

    @unittest.skipUnless(os.name == "nt", "заглушка ffmpeg сделана для Windows")
    def test_interrupted_ffmpeg_leaves_no_broken_mp4(self):
        queue = self.queue(ffmpeg=self.fake_ffmpeg())
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="ERROR"):
            outcome = queue._process(self.source, self.target)
        self.assertIs(outcome, Outcome.RETRY)
        self.assertEqual(os.listdir(self.processed_dir), [])

        # повтор настоящим ffmpeg получает основное имя, а не "rec (2).mp4"
        queue = self.queue()
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="INFO"):
            queue._process(self.source, self.target)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.mp4"])

    def test_rescan_cleans_partials_and_skips_junk(self):
        old = time.time() - 3600
        stale = os.path.join(self.processed_dir, "old.mp4.part")
        open(stale, "wb").close()
        os.utime(stale, (old, old))
        for junk in ("Thumbs.db", "desktop.ini"):
            path = os.path.join(self.recorded_dir, junk)
            open(path, "wb").close()
            os.utime(path, (old, old))
        os.remove(self.source)

        queue = self.queue()
        with self.assertLogs(level="INFO"):
            queue._rescan()
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(queue.pending(), 0)

    def test_waits_for_ffmpeg_without_spending_attempts(self):
        # было: без ffmpeg запись сгорала за три попытки и оставалась в recorded навсегда
        available = []
        queue = self.queue(ffmpeg=lambda: available[0] if available else None)
        # файл только что скопирован: без trusted очередь сочла бы, что он ещё пишется
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="WARNING") as logs:
            self.assertIs(queue._process(self.source, self.target), Outcome.NOT_READY)
            self.assertIs(queue._process(self.source, self.target), Outcome.NOT_READY)
        self.assertEqual(len(logs.output), 1)   # сказано один раз, а не на каждой попытке
        self.assertTrue(os.path.exists(self.source))
        # ffmpeg появился — та же запись конвертируется
        available.append(FFMPEG)
        with self.assertLogs(level="INFO"):
            self.assertIs(queue._process(self.source, self.target), Outcome.DONE)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.mp4"])

    def test_disabled_ffmpeg_moves_file(self):
        queue = self.queue(disable_ffmpeg=True)
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="INFO"):
            queue._process(self.source, self.target)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.ts"])

    def test_conversion_switch_and_manual_conversion(self):
        # автоконвертация выключена в настройках — запись переносится в .ts
        settings = {"convert": False}
        done = []
        queue = ProcessingQueue(FFMPEG, lambda recorded=None: not settings["convert"], threading.Event(),
                                root_path=self.root, bus=self.bus,
                                on_done=lambda recorded, target: done.append((recorded, target)))
        queue.submit(self.source, self.target, trusted=True)
        with self.assertLogs(level="INFO"):
            queue._process(self.source, self.target)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.ts"])

        # кнопка в библиотеке: тот же .ts — в mp4 рядом, повторное нажатие не дублирует
        queue.submit(self.target, self.target, trusted=True, convert=True)
        queue.submit(self.target, self.target, trusted=True, convert=True)
        self.assertEqual(queue.pending(), 2)   # перенос и одна ручная конвертация
        with self.assertLogs(level="INFO"):
            self.assertIs(queue._process(self.target, self.target), Outcome.DONE)
        self.assertEqual(os.listdir(self.processed_dir), ["rec.mp4"])
        self.assertEqual(done[-1], (self.target, os.path.join(self.processed_dir, "rec.mp4")))

    def test_faststart_switch(self):
        def boxes(path):
            """Верхние блоки mp4 по порядку: moov перед mdat — оглавление в начале."""
            names = []
            with open(path, "rb") as source:
                while True:
                    header = source.read(8)
                    if len(header) < 8:
                        return names
                    size = int.from_bytes(header[:4], "big")
                    names.append(header[4:].decode("ascii"))
                    if size == 1:
                        size = int.from_bytes(source.read(8), "big") - 8
                    source.seek(size - 8, os.SEEK_CUR)

        orders = {}
        for faststart in (True, False):
            shutil.copy(self.sample, self.source)
            queue = ProcessingQueue(FFMPEG, False, threading.Event(), root_path=self.root,
                                    faststart=lambda recorded: faststart)
            queue.submit(self.source, self.target, trusted=True)
            with self.assertLogs(level="INFO"):
                queue._process(self.source, self.target)
            mp4 = os.path.join(self.processed_dir, "rec.mp4")
            names = boxes(mp4)
            orders[faststart] = names.index("moov") < names.index("mdat")
            os.remove(mp4)
        self.assertEqual(orders, {True: True, False: False})

    @unittest.skipUnless(os.name == "nt", "заглушка ffmpeg сделана для Windows")
    def test_failed_manual_conversion_keeps_ts(self):
        shutil.move(self.source, self.target)
        stop = threading.Event()
        queue = ProcessingQueue(self.fake_ffmpeg(), True, stop, root_path=self.root,
                                max_attempts=1, bus=self.bus)
        with self.assertLogs(level="ERROR") as logs:
            queue.start()
            queue.submit(self.target, self.target, trusted=True, convert=True)
            self.assertTrue(wait_for(lambda: any(event["action"] == "gave_up"
                                                 for event in self.events.of("processing"))))
            stop.set()
            queue.close_inputs()
            queue.join(30)
        self.assertIn("осталась в .ts", logs.output[-1])
        self.assertEqual(os.listdir(self.processed_dir), ["rec.ts"])
        # кнопку можно нажать снова
        self.assertFalse(queue._forced)

    def test_drain_after_stop(self):
        # запись, отданная в очередь в момент остановки, всё равно обрабатывается
        stop = threading.Event()
        queue = self.queue(stop=stop)
        with self.assertLogs(level="INFO"):
            queue.start()
            stop.set()
            queue.submit(self.source, self.target, trusted=True)
            queue.close_inputs()
            queue.join(30)
        self.assertFalse(queue.is_alive())
        self.assertEqual(os.listdir(self.processed_dir), ["rec.mp4"])
        self.assertEqual(queue.pending(), 0)
        self.assertEqual(queue.snapshot(), {"pending": 0, "current": None})


if __name__ == "__main__":
    unittest.main()
