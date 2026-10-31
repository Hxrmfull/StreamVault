# -*- coding: utf-8 -*-
"""
Обновление самой программы: проверка выпуска, скачивание со сверкой
суммы, распаковка и замена файлов программы. GitHub изображает локальный
сервер.
"""
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from streamvault import update
from tests.helpers import wait_for

NEW = "0.2.0"


def make_release(version=NEW, extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("StreamVault/StreamVault.exe", "окно " + version)
        archive.writestr("StreamVault/StreamVaultCLI.exe", "консоль " + version)
        archive.writestr("StreamVault/_internal/python313.dll", "библиотека " + version)
        archive.writestr("StreamVault/_internal/streamvault/web/app.js", "// " + version)
        for name, text in (extra or {}).items():
            archive.writestr(name, text)
    return buffer.getvalue()


class FakeGitHub:
    """Последний выпуск и его архив; поля — чем вредничать."""

    def __init__(self):
        self.version = NEW
        self.archive = make_release()
        self.served = None              # подменённый архив
        self.digest = None              # что GitHub говорит о сумме
        self.latest_status = 200
        self.with_digest = True
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

            def do_GET(self):
                site.requests.append(self.path)
                name = update.ASSET.format(version=site.version)
                if self.path == "/latest":
                    if site.latest_status != 200:
                        return self.send(b"{}", site.latest_status)
                    asset = {"name": name, "size": len(site.archive),
                             "browser_download_url": site.base + "/download/" + name,
                             "url": site.base + "/api/" + name}
                    if site.with_digest:
                        asset["digest"] = "sha256:" + (site.digest or hashlib.sha256(site.archive).hexdigest())
                    assets = [asset]
                    return self.send(json.dumps({
                        "tag_name": "v" + site.version, "html_url": "https://github.com/x/y/releases/v1",
                        "body": "что нового", "assets": assets}).encode())
                if self.path == "/download/" + name:
                    return self.send(site.served if site.served is not None else site.archive)
                self.send(b"", 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class VersionTest(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(update.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(update.parse_version("1.2"), (1, 2, 0))
        self.assertEqual(update.parse_version("0.3.0-beta"), (0, 3, 0))
        self.assertIsNone(update.parse_version("latest"))

    def test_newer(self):
        self.assertTrue(update.is_newer("v0.2.0", "0.1.9"))
        self.assertTrue(update.is_newer("0.10.0", "0.9.0"))
        self.assertFalse(update.is_newer("0.1.0", "0.1.0"))
        self.assertFalse(update.is_newer("что-то", "0.1.0"))


class UpdaterTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.site = FakeGitHub()
        self.addCleanup(self.site.close)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def updater(self, folder=True, current="0.1.0"):
        return update.AppUpdater(self.dir if folder else None, latest_url=self.site.base + "/latest",
                                 allowed_hosts=["127.0.0.1"], require_https=False, current=current)

    def install(self, **kwargs):
        updater = self.updater(**kwargs)
        updater.start()
        updater.wait(30)
        return updater, updater.snapshot()

    def staged(self):
        return os.path.join(self.dir, "update", "StreamVault-" + NEW, "StreamVault")

    # ------------------------------------------------------------ проверка

    def test_newer_available(self):
        result = self.updater().check()
        self.assertEqual((result["state"], result["version"], result["notes"]), ("available", NEW, "что нового"))
        self.assertTrue(result["can_install"])

    def test_current(self):
        self.assertEqual(self.updater(current=NEW).check()["state"], "current")

    def test_no_releases(self):
        # и у закрытого репозитория GitHub отвечает 404
        self.site.latest_status = 404
        self.assertEqual(self.updater().check()["state"], "none")

    def test_github_unavailable(self):
        self.site.latest_status = 503
        result = self.updater().check()
        self.assertEqual(result["state"], "failed")
        self.assertIn("503", result["error"])

    def test_check_in_background_reports(self):
        changes = []
        updater = update.AppUpdater(self.dir, on_change=changes.append, latest_url=self.site.base + "/latest",
                                    allowed_hosts=["127.0.0.1"], require_https=False)
        updater.check_in_background()
        self.assertTrue(wait_for(lambda: changes and changes[-1]["state"] == "available"))

    # ------------------------------------------------------------ скачивание

    def test_download_and_stage(self):
        updater, result = self.install()
        self.assertEqual(result["state"], "done", result["error"])
        self.assertEqual(result["staged"], self.staged())
        self.assertEqual(sorted(os.listdir(self.staged())), sorted(update.PROGRAM))
        with open(os.path.join(self.staged(), "StreamVault.exe"), encoding="utf-8") as source:
            self.assertEqual(source.read(), "окно " + NEW)
        # во временных файлах ничего не осталось
        self.assertEqual(os.listdir(os.path.join(self.dir, "update")), ["StreamVault-" + NEW])

    def test_already_current(self):
        _, result = self.install(current=NEW)
        self.assertEqual(result["state"], "current")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "update")))

    def test_from_sources_not_installed(self):
        _, result = self.install(folder=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("не обновить", result["error"])

    def assert_refused(self, result, message):
        self.assertEqual(result["state"], "failed")
        self.assertIn(message, result["error"])
        self.assertFalse(os.path.exists(self.staged()))

    def test_github_sum_is_required(self):
        # без суммы от GitHub сверить нечем — не ставим
        self.site.with_digest = False
        self.assert_refused(self.install()[1], "контрольную сумму")

    def test_sum_mismatch(self):
        self.site.digest = "0" * 64
        self.assert_refused(self.install()[1], "контрольной суммой")

    def test_tampered_archive(self):
        archive = self.site.archive
        self.site.served = archive[:-4] + b"evil"
        self.assert_refused(self.install()[1], "контрольной суммой")

    def test_archive_with_foreign_files(self):
        for extra in ({"StreamVault/settings.json": "{}"}, {"../evil.exe": "x"},
                      {"Другое/StreamVault.exe": "x"}):
            self.site.archive = make_release(extra=extra)
            self.assert_refused(self.install()[1], "лишнее")

    def test_apply_needs_download(self):
        self.assertFalse(self.updater().apply())

    def test_apply_starts_new_version_installer(self):
        updater, result = self.install()
        with mock.patch("streamvault.update.subprocess.Popen") as popen:
            self.assertTrue(updater.apply())
        command = popen.call_args.args[0]
        self.assertEqual(command[0], os.path.join(self.staged(), "StreamVaultCLI.exe"))
        self.assertEqual(command[1:], ["--apply-update", self.dir, str(os.getpid())])


class ApplyTest(unittest.TestCase):
    """Новая версия ставит себя на место старой; личное в папке программы остаётся."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.target = os.path.join(self.dir, "StreamVault")
        self.staged = os.path.join(self.target, "update", "StreamVault-" + NEW, "StreamVault")
        self.write_program(self.target, "старая")
        self.write_program(self.staged, "новая")
        self.personal = {
            "settings.json": json.dumps({"channels": [{"channel": "qewbite", "quality": "480p"}]}),
            "downloads.json": "[]",
            os.path.join("logs", "streamvault.log"): "журнал",
            os.path.join("tools", "ffmpeg", "ffmpeg.exe"): "ffmpeg",
            os.path.join("tools", "TwitchDownloader", "TwitchDownloaderCLI.exe"): "tdl",
        }
        for name, text in self.personal.items():
            self.write(os.path.join(self.target, name), text)
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def write(path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)

    def write_program(self, folder, label):
        for name in ("StreamVault.exe", "StreamVaultCLI.exe", os.path.join("_internal", "lib.dll")):
            self.write(os.path.join(folder, name), label)

    def read(self, *parts):
        with open(os.path.join(self.target, *parts), encoding="utf-8") as source:
            return source.read()

    def finished_pid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return str(process.pid)

    def apply(self, **kwargs):
        pid = self.finished_pid()      # до подмены: sys.executable подменяется для всех
        helper = os.path.join(self.staged, "StreamVaultCLI.exe")
        with mock.patch.object(update.sys, "executable", helper):
            return update.apply_update([self.target, pid], relaunch=False, **kwargs)

    def result(self):
        with open(os.path.join(self.target, "update", "result.json"), encoding="utf-8") as source:
            return json.load(source)

    def test_new_version_in_place_personal_kept(self):
        self.assertEqual(self.apply(), 0)
        for name in ("StreamVault.exe", "StreamVaultCLI.exe", os.path.join("_internal", "lib.dll")):
            self.assertEqual(self.read(name), "новая")
        for name, text in self.personal.items():
            self.assertEqual(self.read(name), text, name)
        self.assertEqual(json.loads(self.read("settings.json"))["channels"][0]["channel"], "qewbite")
        self.assertTrue(self.result()["ok"])
        self.assertFalse([name for name in os.listdir(self.target) if name.endswith((".old", ".new"))])

    def test_locked_file_rolls_back(self):
        # файл старой версии занят — замена не проходит, старая остаётся целой
        with open(os.path.join(self.target, "_internal", "lib.dll"), "rb"):
            self.assertEqual(self.apply(attempts=2, pause=0), 1)
        for name in ("StreamVault.exe", "StreamVaultCLI.exe", os.path.join("_internal", "lib.dll")):
            self.assertEqual(self.read(name), "старая")
        self.assertFalse(self.result()["ok"])
        self.assertFalse([name for name in os.listdir(self.target) if name.endswith((".old", ".new"))])

    def test_next_start_reports_and_cleans(self):
        self.apply()
        result = update.finish_previous(self.target)
        self.assertTrue(result["ok"])
        self.assertTrue(wait_for(lambda: not os.path.exists(os.path.join(self.target, "update")), timeout=10))
        self.assertIsNone(update.finish_previous(self.target))
        self.assertIsNone(update.finish_previous(None))

    def test_in_use(self):
        self.assertFalse(update.program_in_use(self.target))
        with open(os.path.join(self.target, "_internal", "lib.dll"), "rb"):
            self.assertTrue(update.program_in_use(self.target))


if __name__ == "__main__":
    unittest.main()
