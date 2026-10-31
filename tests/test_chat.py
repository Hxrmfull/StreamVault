# -*- coding: utf-8 -*-
"""
Чат Twitch через TwitchDownloader: команды, разбор вывода, шаг чата
в загрузках и установка TwitchDownloader кнопкой. Вместо настоящего
TwitchDownloaderCLI — заглушка tests/fake_twitchdownloader.py.
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from streamvault import twitchdownloader as td
from streamvault.archive import chat
from streamvault.archive import downloads as dl
from tests.helpers import wait_for
from tests.test_downloads import DownloadTestCase

FAKE_TDL = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "fake_twitchdownloader.py")]
VOD = "https://www.twitch.tv/videos/123"


class FakeTdl:
    """Заглушка TwitchDownloaderCLI на время теста: режим и журнал запусков."""

    def __init__(self, mode="ok", log=None):
        self._values = {"FAKE_TDL_MODE": mode, "FAKE_TDL_LOG": log}

    def set_mode(self, mode):
        os.environ["FAKE_TDL_MODE"] = mode

    def __enter__(self):
        self._saved = {key: os.environ.get(key) for key in self._values}
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class StubLocator:
    """Вместо TwitchDownloaderLocator и FFmpegLocator: отвечает заданным."""

    def __init__(self, command=None, path=None):
        self._command = command
        self._path = path

    def command(self):
        return self._command

    def path(self):
        return self._path

    def directory(self):
        return os.path.dirname(self._path) if self._path else None


class CommandsTest(unittest.TestCase):

    def test_text(self):
        steps = chat.commands(["tdl"], VOD, os.path.join("d", "запись.mp4"), chat.TEXT,
                              temp_dir="tmp")
        self.assertEqual([name for name, _ in steps], ["download", "html"])
        download = steps[0][1]
        self.assertEqual(download[:4], ["tdl", "chatdownload", "--id", VOD])
        self.assertEqual(download[download.index("-o") + 1], os.path.join("d", "запись.chat.json"))
        for flag in ("--embed-images", "--banner", "--collision", "--temp-path"):
            self.assertIn(flag, download)
        self.assertNotIn("-b", download)
        self.assertEqual(steps[1][1][steps[1][1].index("-o") + 1], os.path.join("d", "запись.chat.html"))

    def test_section_and_render(self):
        steps = chat.commands(["tdl"], VOD, "v.mp4", chat.RENDER, section=(65.5, 130.2),
                              ffmpeg="C:\\ffmpeg\\bin\\ffmpeg.exe", size=(400, 720, 16))
        download, render = steps[0][1], steps[2][1]
        self.assertEqual(download[download.index("-b") + 1], "65s")
        self.assertEqual(download[download.index("-e") + 1], "131s")
        self.assertEqual(render[render.index("-w") + 1], "400")
        self.assertEqual(render[render.index("-h") + 1], "720")
        self.assertEqual(render[render.index("--font-size") + 1], "16")
        self.assertEqual(render[render.index("--ffmpeg-path") + 1], "C:\\ffmpeg\\bin\\ffmpeg.exe")
        self.assertEqual(render[render.index("-o") + 1], "v.chat.mp4")
        self.assertIn("--offline", render)    # смайлы — из JSON, а не заново из сети

    def test_explain(self):
        self.assertEqual(chat.explain(["[STATUS] - x", "[ERROR] - Не то"]), "Не то")
        dotnet = ["Unhandled exception. System.AggregateException: errors (Invalid VOD, deleted/expired VOD possibly?)",
                  " ---> System.NullReferenceException: Invalid VOD, deleted/expired VOD possibly?"]
        self.assertIn("запись удалена", chat.explain(dotnet))
        clip = [" ---> System.NullReferenceException: Invalid VOD for clip, deleted/expired VOD possibly?"]
        self.assertIn("клип", chat.explain(clip))
        self.assertEqual(chat.explain([" ---> System.IO.IOException: Disk full"]), "Disk full")
        self.assertEqual(chat.explain(["что-то", "последнее"]), "последнее")
        self.assertEqual(chat.explain([]), "")

    def test_supported(self):
        self.assertTrue(chat.supported("twitch", "vod"))
        self.assertTrue(chat.supported("twitch", "clip"))
        self.assertFalse(chat.supported("twitch", "video"))
        self.assertTrue(chat.supported("kick", "vod"))
        self.assertTrue(chat.supported("kick", "clip"))              # чат клипа — кусок истории эфира
        self.assertTrue(chat.supported("youtube", "video", "was_live"))
        self.assertTrue(chat.supported("youtube", "video"))          # неизвестно — пробуем
        self.assertFalse(chat.supported("youtube", "video", "not_live"))
        self.assertTrue(chat.supported("vkvideolive", "vod"))
        self.assertFalse(chat.supported("vkvideolive", "video"))


class RunTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "запись.mp4")
        open(self.video, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_files_and_progress(self):
        seen = []
        with FakeTdl("ok"):
            chat.run(chat.commands(FAKE_TDL, VOD, self.video, chat.RENDER, ffmpeg="ffmpeg"),
                     on_progress=lambda stage, percent: seen.append((stage, percent)))
        self.assertEqual(sorted(chat.existing(self.video)), ["html", "json", "video"])
        self.assertEqual([stage for stage in dict.fromkeys(stage for stage, _ in seen)],
                         ["download", "html", "render"])
        self.assertIn(("download", 50), seen)
        self.assertIn(("render", 100), seen)

    def test_failure_is_explained(self):
        with FakeTdl("fail"), self.assertRaisesRegex(chat.ChatError, "запись удалена"):
            chat.run(chat.commands(FAKE_TDL, VOD, self.video, chat.TEXT))
        self.assertEqual(chat.existing(self.video), {})

    def test_stop_kills_process(self):
        stop = threading.Event()

        def started(process):
            # останавливают, пока идёт первый шаг
            threading.Timer(0.3, lambda: (stop.set(), process.kill())).start()

        with FakeTdl("slow"), self.assertRaises(chat.ChatStopped):
            chat.run(chat.commands(FAKE_TDL, VOD, self.video, chat.TEXT),
                     on_process=started, stopped=stop.is_set)
        # недоделанный файл убран: иначе он сошёл бы за готовый чат
        self.assertEqual(chat.existing(self.video), {})

    def test_missing_program(self):
        with self.assertRaisesRegex(chat.ChatError, "не удалось запустить"):
            chat.run(chat.commands([os.path.join(self.dir, "нет.exe")], VOD, self.video, chat.TEXT))


class DownloadChatTest(DownloadTestCase):
    """Шаг чата в загрузках: после видео, не мешая ему."""

    def setUp(self):
        super().setUp()
        self.tdl_log = os.path.join(self.dir, "tdl.log")
        self.tdl = StubLocator(FAKE_TDL)
        self.ffmpeg = StubLocator(path=os.path.join(self.dir, "ffmpeg.exe"))

    def manager(self, resume=True, start=True):
        manager = dl.DownloadManager(self.settings, bus=self.bus, resume=resume, ffmpeg=self.ffmpeg,
                                     twitchdownloader=self.tdl)
        self.managers.append(manager)
        if start:
            manager.start()
        return manager

    def tdl_calls(self):
        if not os.path.exists(self.tdl_log):
            return []
        with open(self.tdl_log, encoding="utf-8") as source:
            return [json.loads(line)[0] for line in source]

    def downloads_made(self):
        return [call["kind"] for call in self.calls() if call["kind"] == "download"]

    def download(self, chat_mode=chat.TEXT, tdl="ok", ytdlp="ok", info=None, url=VOD, until=(dl.DONE,)):
        with self.fake(ytdlp, info=info), FakeTdl(tdl, self.tdl_log):
            manager = self.manager()
            job_id = manager.add(url, chat_mode=chat_mode)
            job = self.wait_state(manager, job_id, *until)
        return manager, job

    def test_text_chat_next_to_video(self):
        _, job = self.download()
        self.assertEqual(job["state"], dl.DONE)
        self.assertEqual(job["chat_error"], "")
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])
        base = os.path.splitext(job["file"])[0]
        self.assertEqual(job["chat_files"]["html"], base + ".chat.html")
        self.assertTrue(os.path.exists(job["chat_files"]["html"]))
        self.assertEqual(self.tdl_calls(), ["chatdownload", "chatupdate"])
        self.assertIsNone(job["chat_progress"])

    def test_render(self):
        _, job = self.download(chat.RENDER)
        self.assertEqual(sorted(job["chat_files"]), ["html", "json", "video"])
        self.assertEqual(self.tdl_calls(), ["chatdownload", "chatupdate", "chatrender"])

    def test_render_without_ffmpeg(self):
        self.ffmpeg = StubLocator()
        _, job = self.download(chat.RENDER)
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])
        self.assertIn("нет ffmpeg", job["chat_error"])

    def test_chat_failure_keeps_video(self):
        _, job = self.download(tdl="fail")
        self.assertEqual(job["state"], dl.DONE)
        self.assertTrue(os.path.exists(job["file"]))
        self.assertIn("запись удалена", job["chat_error"])
        self.assertEqual(job["chat_files"], {})

    def test_without_twitchdownloader(self):
        self.tdl = StubLocator()
        _, job = self.download()
        self.assertEqual(job["state"], dl.DONE)
        self.assertIn("TwitchDownloader", job["chat_error"])

    def test_no_chat_by_default(self):
        _, job = self.download(chat_mode=None)
        self.assertEqual(job["chat"], chat.NONE)
        self.assertEqual(self.tdl_calls(), [])

    def test_default_from_settings(self):
        self.settings.update(chat_mode=chat.TEXT)
        _, job = self.download(chat_mode=None)
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])

    YOUTUBE = {"_type": "video", "extractor_key": "Youtube", "id": "y1", "title": "видео",
               "uploader": "Кто-то", "duration": 60, "webpage_url": "https://www.youtube.com/watch?v=y1",
               "formats": [{"format_id": "18", "height": 360, "ext": "mp4"}]}

    def test_video_without_chat(self):
        # обычное видео YouTube — не эфир: чата у него не бывает
        info = dict(self.YOUTUBE, live_status="not_live")
        _, job = self.download(info=info, url="https://www.youtube.com/watch?v=y1")
        self.assertEqual(job["state"], dl.DONE)
        self.assertEqual((job["chat_files"], job["chat_error"]), ({}, ""))
        self.assertEqual(self.tdl_calls(), [])

    def test_youtube_stream_chat(self):
        # чат прошедшего эфира скачивает vodchat, HTML делает TwitchDownloader без похода к Twitch
        made = []

        def fake_download(platform, url, target, **options):
            made.append((platform, url, options["section"], options["duration"]))
            options["progress"](50)
            with open(target, "w", encoding="utf-8") as output:
                output.write("{}")
            return 3

        with mock.patch.object(dl.vodchat, "download", fake_download):
            _, job = self.download(info=dict(self.YOUTUBE, live_status="was_live"),
                                   url="https://www.youtube.com/watch?v=y1")
        self.assertEqual(made, [("youtube", "https://www.youtube.com/watch?v=y1", None, 60)])
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])
        self.assertEqual(job["chat_error"], "")
        with open(self.tdl_log, encoding="utf-8") as source:
            (html,) = [json.loads(line) for line in source]
        self.assertEqual(html[0], "chatupdate")
        self.assertNotIn("--embed-missing", html)

    def test_youtube_chat_without_twitchdownloader_and_empty(self):
        self.tdl = StubLocator()

        def fake_download(platform, url, target, **options):
            with open(target, "w", encoding="utf-8") as output:
                output.write("{}")
            return 1

        with mock.patch.object(dl.vodchat, "download", fake_download):
            _, job = self.download(info=dict(self.YOUTUBE, live_status="was_live"),
                                   url="https://www.youtube.com/watch?v=y1")
        # .chat.json хватает для просмотра с чатом; HTML сделает TwitchDownloader потом
        self.assertEqual(sorted(job["chat_files"]), ["json"])
        self.assertIn("TwitchDownloader", job["chat_error"])
        with mock.patch.object(dl.vodchat, "download", lambda *args, **options: 0):
            _, job = self.download(info=dict(self.YOUTUBE, id="y2", live_status=""),
                                   url="https://www.youtube.com/watch?v=y2")
        self.assertEqual(job["chat_files"], {})
        self.assertIn("нет сообщений", job["chat_error"])

    def test_chat_later_without_downloading_again(self):
        manager, job = self.download(chat_mode=chat.NONE)
        with self.fake("ok"), FakeTdl("ok", self.tdl_log):
            manager.fetch_chat(job["id"])
            self.assertTrue(wait_for(lambda: manager.job(job["id"])["chat_files"], timeout=15))
        job = manager.job(job["id"])
        self.assertEqual(job["state"], dl.DONE)
        self.assertFalse(job["chat_only"])
        self.assertEqual(self.downloads_made(), ["download"])

    def test_same_link_again_with_chat(self):
        manager, job = self.download(chat_mode=chat.NONE)
        with self.fake("ok"), FakeTdl("ok", self.tdl_log):
            self.assertEqual(manager.add(VOD, chat_mode=chat.TEXT), job["id"])
            self.assertTrue(wait_for(lambda: manager.job(job["id"])["chat_files"], timeout=15))
        self.assertEqual(self.downloads_made(), ["download"])

    def test_fetch_chat_needs_video_with_chat(self):
        info = {"_type": "video", "extractor_key": "Youtube", "id": "y1", "title": "видео",
                "duration": 60, "formats": [], "live_status": "not_live"}
        manager, job = self.download(chat_mode=chat.NONE, info=info, url="https://www.youtube.com/watch?v=y1")
        with self.assertRaises(ValueError):
            manager.fetch_chat(job["id"])

    def wait_chat(self, manager, job_id):
        self.assertTrue(wait_for(lambda: (manager.job(job_id)["chat_progress"] or {}).get("percent")
                                 is not None, timeout=15))

    def test_cancel_during_chat_keeps_video(self):
        with self.fake("ok"), FakeTdl("slow", self.tdl_log):
            manager = self.manager()
            job_id = manager.add(VOD, chat_mode=chat.TEXT)
            self.wait_chat(manager, job_id)
            self.assertEqual(manager.job(job_id)["state"], dl.PROCESSING)
            manager.cancel(job_id)
            job = self.wait_state(manager, job_id, dl.DONE)
        self.assertTrue(os.path.exists(job["file"]))
        self.assertEqual(job["chat_error"], "чат отменён")
        self.assertFalse(job["chat_only"])

    def test_pause_during_chat_then_chat_only(self):
        with self.fake("ok"), FakeTdl("slow", self.tdl_log) as tdl:
            manager = self.manager()
            job_id = manager.add(VOD, chat_mode=chat.TEXT)
            self.wait_chat(manager, job_id)
            manager.pause(job_id)
            job = self.wait_state(manager, job_id, dl.PAUSED)
            self.assertTrue(job["chat_only"])
            self.assertTrue(os.path.exists(job["file"]))
            tdl.set_mode("ok")
            manager.resume(job_id)
            job = self.wait_state(manager, job_id, dl.DONE)
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])
        self.assertEqual(self.downloads_made(), ["download"])

    def test_cancel_paused_chat_keeps_video(self):
        with self.fake("ok"), FakeTdl("slow", self.tdl_log):
            manager = self.manager()
            job_id = manager.add(VOD, chat_mode=chat.TEXT)
            self.wait_chat(manager, job_id)
            manager.pause(job_id)
            job = self.wait_state(manager, job_id, dl.PAUSED)
            manager.cancel(job_id)
            job = self.wait_state(manager, job_id, dl.DONE)
        self.assertTrue(os.path.exists(job["file"]))
        self.assertEqual(job["chat_error"], "чат отменён")

    def test_chat_continues_after_restart(self):
        with self.fake("ok"), FakeTdl("slow", self.tdl_log) as tdl:
            manager = self.manager()
            job_id = manager.add(VOD, chat_mode=chat.TEXT)
            self.wait_chat(manager, job_id)
            manager.shutdown(timeout=10)
            self.managers.remove(manager)
            tdl.set_mode("ok")
            again = self.manager()
            job = self.wait_state(again, job_id, dl.DONE)
        self.assertEqual(sorted(job["chat_files"]), ["html", "json"])
        self.assertEqual(self.downloads_made(), ["download"])


# ------------------------------------------------------------ установка TwitchDownloader

def make_archive(version, binary=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(td.CLI_EXE, binary if binary is not None else b"FAKETDL " + version.encode())
        archive.writestr("COPYRIGHT.txt", "MIT")
        archive.writestr("THIRD-PARTY-LICENSES.txt", "...")
        archive.writestr("libHarfBuzzSharp.pdb", "not needed")
        archive.writestr("../evil.exe", "evil")
    return buffer.getvalue()


def fake_version(path):
    with open(path, "rb") as source:
        match = re.match(rb"FAKETDL (\d+(?:\.\d+)+)", source.read(64))
    return match.group(1).decode() if match else None


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeSite:
    """GitHub (выпуски lay295/TwitchDownloader) и каталог scoop."""

    def __init__(self):
        self.latest = "1.2.3"
        self.archives = {"1.2.3": make_archive("1.2.3")}
        self.scoop_version = "1.2.3"
        self.scoop_hash = None          # по умолчанию верная
        self.scoop_url = None           # по умолчанию ссылка на GitHub
        self.down = set()               # scoop, latest
        self.modes = {"github": "ok", "api": "ok"}
        self.served = None              # подменённый файл
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def json(self, data):
                self.send(json.dumps(data).encode())

            def do_GET(self):
                path = self.path
                site.requests.append(path)
                if path == "/latest":
                    if "latest" in site.down:
                        return self.send(b"", 503)
                    return self.json({"tag_name": site.latest})
                if path.startswith("/release/"):
                    version = path.rsplit("/", 1)[1]
                    archive = site.archives.get(version)
                    if archive is None:
                        return self.send(b"", 404)
                    return self.json({"assets": [{"name": site.name(version), "size": len(archive),
                                                  "digest": "sha256:" + sha(archive),
                                                  "url": site.base + "/api/" + version}]})
                if path == "/scoop":
                    if "scoop" in site.down:
                        return self.send(b"", 503)
                    version = site.scoop_version
                    return self.json({"version": version, "architecture": {"64bit": {
                        "url": site.scoop_url or site.file_url(version),
                        "hash": site.scoop_hash or sha(site.archives[version])}}})
                if path.startswith("/files/"):
                    return self.file("github", path.split("/")[2])
                if path.startswith("/api/"):
                    if self.headers.get("Accept") != "application/octet-stream":
                        return self.json({"name": "asset"})
                    return self.file("api", path.rsplit("/", 1)[1])
                self.send(b"", 404)

            def file(self, source, version):
                if site.modes[source] == "fail":
                    return self.send(b"", 500)
                body = site.archives[version]
                self.send(site.served if site.served is not None else body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def name(version):
        return td.TwitchDownloaderInstaller.ARCHIVE.format(version=version)

    def file_url(self, version):
        return "%s/files/%s/%s" % (self.base, version, self.name(version))

    def urls(self):
        return {"latest": self.base + "/latest", "release": self.base + "/release/{version}",
                "scoop": self.base + "/scoop", "github": self.base + "/files/{version}/{name}"}

    def downloaded(self):
        return [path for path in self.requests if path.startswith(("/files/", "/api/"))]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ReadVersionTest(unittest.TestCase):

    def test_version_from_error_stream(self):
        # настоящий TwitchDownloaderCLI так отвечает на --version: в stderr и с кодом 1
        answer = mock.Mock(returncode=1, stdout=b"",
                           stderr=b"TwitchDownloaderCLI 1.56.5+f8335cabef7436c3\r\n\r\n")
        with mock.patch("streamvault.twitchdownloader.subprocess.run", return_value=answer):
            self.assertEqual(td.read_version("TwitchDownloaderCLI.exe"), "1.56.5")

    def test_does_not_start(self):
        with mock.patch("streamvault.twitchdownloader.subprocess.run", side_effect=OSError()):
            self.assertIsNone(td.read_version("TwitchDownloaderCLI.exe"))


class InstallerTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.locator = td.TwitchDownloaderLocator(os.path.join(self.dir, "tools", "TwitchDownloader"))
        self.site = FakeSite()
        self.addCleanup(self.site.close)
        patcher = mock.patch.object(td, "read_version", fake_version)
        patcher.start()
        self.addCleanup(patcher.stop)
        which = mock.patch.object(td.shutil, "which", return_value=None)
        which.start()
        self.addCleanup(which.stop)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def install(self, known_sums=None):
        installer = td.TwitchDownloaderInstaller(
            self.locator, urls=self.site.urls(), allowed_hosts=["127.0.0.1"], require_https=False,
            known_sums=known_sums if known_sums is not None else {})
        installer.start()
        installer.wait(30)
        return installer.snapshot()

    def installed(self):
        own = self.locator.own_dir
        return sorted(name for name in os.listdir(own) if not name.endswith(".part")) \
            if os.path.isdir(own) else []

    def assert_installed(self, result, version):
        self.assertEqual(result["state"], "done", result["error"])
        self.assertEqual(self.installed(), sorted([td.CLI_EXE, "COPYRIGHT.txt",
                                                   "THIRD-PARTY-LICENSES.txt", "version.json"]))
        self.assertEqual(self.locator.status()["version"], version)
        self.assertEqual(self.locator.command(), [os.path.abspath(self.locator.own_path)])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tools", "evil.exe")))

    def assert_failed(self, result, message):
        self.assertEqual(result["state"], "failed")
        self.assertIn(message, result["error"])
        self.assertEqual(self.installed(), [])

    def test_checked_on_github_and_scoop(self):
        self.assert_installed(self.install(), "1.2.3")

    def test_scoop_behind_installs_checked_version(self):
        # новейшую 1.2.4 scoop ещё не знает — ставится 1.2.3, сверенная в двух местах
        self.site.latest = "1.2.4"
        self.site.archives["1.2.4"] = make_archive("1.2.4")
        self.assert_installed(self.install(), "1.2.3")

    def test_known_sum_when_scoop_is_down(self):
        self.site.down.add("scoop")
        self.assert_installed(self.install({"1.2.3": sha(self.site.archives["1.2.3"])}), "1.2.3")

    def test_one_place_is_not_enough(self):
        self.site.down.add("scoop")
        self.assert_failed(self.install(), "в двух местах")

    def test_sums_must_agree(self):
        self.site.scoop_hash = "0" * 64
        self.assert_failed(self.install(), "не совпадают")

    def test_scoop_pointing_elsewhere_is_ignored(self):
        self.site.scoop_url = "https://example.com/" + self.site.name("1.2.3")
        self.assert_failed(self.install(), "в двух местах")

    def test_tampered_archive(self):
        # той же длины: больший файл отвергся бы раньше, по размеру
        archive = self.site.archives["1.2.3"]
        self.site.served = archive[:-4] + b"evil"
        self.assert_failed(self.install(), "контрольной суммой")

    def test_api_route(self):
        self.site.modes["github"] = "fail"
        result = self.install()
        self.assert_installed(result, "1.2.3")
        self.assertEqual(result["source"], "GitHub API")

    def test_must_run(self):
        self.site.archives["1.2.3"] = make_archive("1.2.3", binary=b"MZ not really")
        self.assert_failed(self.install(), "не запустился")

    def test_already_current(self):
        self.assert_installed(self.install(), "1.2.3")
        downloads = len(self.site.downloaded())
        result = self.install()
        self.assertEqual(result["state"], "current")
        self.assertEqual(len(self.site.downloaded()), downloads)


if __name__ == "__main__":
    unittest.main()
