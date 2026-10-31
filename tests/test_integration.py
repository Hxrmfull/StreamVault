# -*- coding: utf-8 -*-
"""
Проверки с настоящими streamlink, yt-dlp и ffmpeg. Видео отдаёт локальный
HTTP-сервер, так что сеть не нужна.
"""

import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

from streamvault import util
from streamvault.archive import downloads as dl
from streamvault.archive import ytdlp
from streamvault.live.probes import Status
from streamvault.live.service import LiveService
from streamvault.platforms import PLATFORMS
from streamvault.settings import Settings
from tests.helpers import OFFLINE, FakeProcessing, ScriptedProbe, wait_for
from tests.test_processing import make_sample
from tests.test_recorder import InstantRecorder

TOOLS = shutil.which("streamlink") and shutil.which("ffmpeg")
YTDLP_TOOLS = importlib.util.find_spec("yt_dlp") and shutil.which("ffmpeg")


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class LocalServerTestCase(unittest.TestCase):
    """Папка с записью, которую раздаёт локальный HTTP-сервер."""

    @classmethod
    def setUpClass(cls):
        cls.www = tempfile.mkdtemp()
        cls.sample = os.path.join(cls.www, "x.ts")
        make_sample(cls.sample, seconds=3)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", cls.sample, "-c", "copy",
                        os.path.join(cls.www, "x.mp4")], check=True)
        cls.port = free_port()
        cls.server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(cls.port), "--bind", "127.0.0.1",
             "--directory", cls.www],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.url = "http://127.0.0.1:%d/x.ts" % cls.port

        def reachable():
            try:
                urllib.request.urlopen(cls.url, timeout=1).close()
                return True
            except OSError:
                return False
        if not wait_for(reachable, timeout=10):
            cls.tearDownClass()
            raise unittest.SkipTest("локальный HTTP-сервер не поднялся")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        shutil.rmtree(cls.www, ignore_errors=True)


@unittest.skipUnless(YTDLP_TOOLS, "нужны yt-dlp и ffmpeg")
class RealYtdlpTest(LocalServerTestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.mp4_url = "http://127.0.0.1:%d/x.mp4" % self.port

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_inspect(self):
        info = ytdlp.inspect(self.mp4_url)
        self.assertEqual((info["type"], info["id"]), ("video", "x"))
        self.assertEqual(info["qualities"][0]["quality"], "best")

    def test_download(self):
        settings = Settings(os.path.join(self.root, "settings.json"),
                            {"root_path": self.root})
        manager = dl.DownloadManager(settings)
        with self.assertLogs(level="INFO"):
            manager.start()
            job_id = manager.add(self.mp4_url)
            done = wait_for(lambda: manager.job(job_id)["state"] in (dl.DONE, dl.FAILED),
                            timeout=60)
            manager.shutdown()
        job = manager.job(job_id)
        self.assertTrue(done)
        self.assertEqual(job["state"], dl.DONE, job["error"])
        self.assertTrue(job["file"].endswith("[x].mp4"))
        self.assertEqual(os.path.getsize(job["file"]),
                         os.path.getsize(os.path.join(self.www, "x.mp4")))


@unittest.skipUnless(TOOLS, "нужны streamlink и ffmpeg")
class RealToolsTest(LocalServerTestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        patcher = mock.patch("streamvault.live.recorder.stream_url",
                             return_value="httpstream://" + self.url)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_braces_in_title_with_real_streamlink(self):
        # было: streamlink падал на непарной скобке, и стрим не писался вообще
        processing = FakeProcessing()
        for title in ["смайл :} и {", "Стрим {author} в деле", "{title}"]:
            probe = ScriptedProbe((Status.ONLINE, {"title": title}), OFFLINE)
            recorder = InstantRecorder("twitch", "c", probe, "best", 15, self.root,
                                       processing)
            with self.assertLogs(level="INFO"):
                recorder._iteration()
            recorded = processing.submitted[-1][0]
            self.assertEqual(os.path.getsize(recorded), os.path.getsize(self.sample), title)
            expected = title.replace(":", "").replace("  ", " ")
            self.assertIn(expected, os.path.basename(recorded))

    def test_service_records_and_converts(self):
        probe = ScriptedProbe((Status.ONLINE, {"title": "конец к концу"}), OFFLINE)
        probes = {name: probe for name in PLATFORMS}
        settings = Settings(os.path.join(self.root, "settings.json"),
                            {"root_path": self.root,
                             "channels": [{"platform": "twitch", "channel": "e2e", "enabled": True}]})
        with mock.patch("streamvault.live.service.build_probes",
                        return_value=(probes, probe)), self.assertLogs(level="INFO"):
            service = LiveService(settings)
            service.start()
            processed = os.path.join(self.root, util.FINISHED_DIR, "twitch", "e2e")
            done = wait_for(lambda: os.path.isdir(processed)
                            and any(name.endswith(".mp4") for name in os.listdir(processed)),
                            timeout=60)
            left = service.shutdown(grace=0)
        self.assertTrue(done)
        self.assertEqual(left, 0)
        names = os.listdir(processed)
        self.assertEqual(len(names), 1)
        self.assertIn("конец к концу", names[0])
        self.assertGreater(os.path.getsize(os.path.join(processed, names[0])), 0)


if __name__ == "__main__":
    unittest.main()
