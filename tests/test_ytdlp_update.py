# -*- coding: utf-8 -*-
"""
Какой yt-dlp запускать и его обновление кнопкой. Обновление проверяется
на локальном сервере, который изображает GitHub и PyPI: подписывает список
сумм тестовым ключом и умеет вредничать — падать, подменять файл, сумму
и подпись.
"""
import hashlib
import io
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from streamvault import util
from streamvault.archive import ytdlp_update as yu
from tests import pgp_testkey

BUNDLED = "2026.01.01"
VERSION = "2026.11.05"


def make_zipapp(version=VERSION, extra=b""):
    """Как сборка «yt-dlp» с GitHub: строка запуска, а за ней zip с кодом."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "import yt_dlp\n")
        archive.writestr("yt_dlp/__init__.py", "def main(argv=None): pass\n")
        archive.writestr("yt_dlp/version.py", "__version__ = '%s'\nVARIANT = None\n%s"
                         % (version, extra.decode()))
        archive.writestr("yt_dlp_ejs/__init__.py", "")
    return b"#!/usr/bin/env python3\n" + buffer.getvalue()


def make_wheel(version=VERSION):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("yt_dlp/__init__.py", "def main(argv=None): pass\n")
        archive.writestr("yt_dlp/version.py", "__version__ = '%s'\nVARIANT = 'pip'\n" % version)
        archive.writestr("yt_dlp-%s.dist-info/METADATA" % version, "Name: yt-dlp\n")
    return buffer.getvalue()


def fake_read_version(path):
    """Вместо запуска: версия из yt_dlp/version.py внутри архива."""
    with zipfile.ZipFile(path) as archive:
        text = archive.read("yt_dlp/version.py").decode()
    return re.search(r"__version__ = '([^']+)'", text).group(1)


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeSite:
    """
    GitHub и PyPI сразу. modes[github|pypi|api]: ok, fail, tampered (отдать
    другой файл). down — недоступные адреса: latest, sums, pypi.
    """

    def __init__(self):
        self.zipapp = make_zipapp()
        self.wheel = make_wheel()
        self.version = VERSION
        self.modes = {"github": "ok", "pypi": "ok", "api": "ok"}
        self.down = set()
        self.github_digest = None       # что GitHub говорит о сумме, по умолчанию правда
        self.sign_other = False         # подписать не тот список
        self.requests = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, content_type="application/octet-stream"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def json(self, data):
                self.send(json.dumps(data).encode(), content_type="application/json")

            def do_GET(self):
                path = self.path
                site.requests.append(path)
                download = "/download/%s/" % site.version
                if path == "/latest":
                    if "latest" in site.down:
                        return self.send(b"", 503)
                    return self.json({"tag_name": site.version, "assets": [
                        {"name": "yt-dlp", "size": len(site.zipapp),
                         "digest": "sha256:" + (site.github_digest or sha(site.zipapp)),
                         "url": site.base + "/api/assets/1"},
                        {"name": "SHA2-256SUMS", "url": site.base + "/api/assets/2"},
                        {"name": "SHA2-256SUMS.sig", "url": site.base + "/api/assets/3"}]})
                if path.startswith("/download/") and "download" in site.down:
                    return self.send(b"", 503)      # github.com закрыт целиком
                if path in (download + "SHA2-256SUMS", "/api/assets/2"):
                    return self.sums(path, site.sums())
                if path in (download + "SHA2-256SUMS.sig", "/api/assets/3"):
                    sums = site.sums()
                    return self.sums(path, pgp_testkey.sign(sums + b"x" if site.sign_other else sums))
                if path == download + "yt-dlp":
                    return self.file("github", site.zipapp)
                if path == "/api/assets/1":
                    if self.headers.get("Accept") != "application/octet-stream":
                        return self.json({"name": "yt-dlp"})
                    return self.file("api", site.zipapp)
                if path in ("/pypi", "/pypi/" + site.version):
                    if "pypi" in site.down:
                        return self.send(b"", 503)
                    name = "yt_dlp-%s-py3-none-any.whl" % site.version
                    return self.json({"info": {"version": site.version}, "urls": [
                        {"packagetype": "sdist", "filename": "yt_dlp-%s.tar.gz" % site.version},
                        {"packagetype": "bdist_wheel", "filename": name, "size": len(site.wheel),
                         "digests": {"sha256": sha(site.wheel)}, "url": site.base + "/files/" + name}]})
                if path.startswith("/files/"):
                    return self.file("pypi", site.wheel)
                self.send(b"", 404)

            def sums(self, path, body):
                if "sums" in site.down:
                    return self.send(b"", 503)
                if path.startswith("/api/") and self.headers.get("Accept") != "application/octet-stream":
                    return self.json({"name": "SHA2-256SUMS"})
                self.send(body)

            def file(self, source, body):
                mode = site.modes[source]
                if mode == "fail":
                    return self.send(b"", 500)
                if mode == "tampered":
                    body = body[:-4] + b"evil"
                self.send(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def sums(self):
        return ("%s  yt-dlp\n%s  yt-dlp.exe\n" % (sha(self.zipapp), "1" * 64)).encode()

    def urls(self):
        return {"latest": self.base + "/latest",
                "download": self.base + "/download/{version}/{name}",
                "pypi": self.base + "/pypi", "pypi_version": self.base + "/pypi/{version}"}

    def downloaded(self):
        """Какие файлы yt-dlp у сайта спрашивали (не считая сведений о выпуске)."""
        return [path for path in self.requests
                if path.endswith("/yt-dlp") or path.startswith(("/files/", "/api/"))]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class UpdaterTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.own = os.path.join(self.dir, "tools", "yt-dlp")
        self.site = FakeSite()
        self.addCleanup(self.site.close)
        for target, value in (("read_version", fake_read_version),
                              ("bundled_version", lambda: BUNDLED)):
            patcher = mock.patch.object(yu, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.locator = yu.YtdlpLocator(self.own)
        self.states = []
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def update(self):
        updater = yu.YtdlpUpdater(
            self.locator, on_change=lambda state: self.states.append(dict(state)),
            urls=self.site.urls(), allowed_hosts=["127.0.0.1"], require_https=False,
            key=pgp_testkey.armored_key(), fingerprint=pgp_testkey.fingerprint())
        updater.start()
        updater.wait(30)
        return updater.snapshot()

    def files(self):
        return sorted(os.listdir(self.own)) if os.path.isdir(self.own) else []

    def assert_updated(self, result, source):
        self.assertEqual(result["state"], "done", result["error"])
        self.assertEqual(result["source"], source)
        self.assertEqual(self.files(), ["version.json", "yt-dlp-%s.zip" % VERSION])
        self.assertEqual(self.locator.status(), {"version": VERSION, "own": True, "bundled": BUNDLED})
        with open(os.path.join(self.own, "version.json"), encoding="utf-8") as record:
            self.assertEqual(json.load(record)["source"], source)

    def assert_nothing_installed(self, result, message):
        self.assertEqual(result["state"], "failed")
        self.assertIn(message, result["error"])
        self.assertEqual(self.files(), [])
        self.assertIsNone(self.locator.refresh())

    # ------------------------------------------------------------ обычный путь

    def test_updates_from_github(self):
        self.assert_updated(self.update(), "GitHub")
        self.assertEqual(self.site.downloaded(), ["/download/%s/yt-dlp" % VERSION])
        env = self.locator.environment({})
        path = os.path.join(self.own, "yt-dlp-%s.zip" % VERSION)
        self.assertEqual(env[util.YTDLP_COPY_ENV], path)
        self.assertTrue(env["PYTHONPATH"].startswith(path))

    def test_already_latest(self):
        self.site.version = BUNDLED
        result = self.update()
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["version"], BUNDLED)
        self.assertEqual(self.site.downloaded(), [])
        self.assertEqual(self.files(), [])

    def test_older_copies_are_removed(self):
        os.makedirs(self.own)
        old = os.path.join(self.own, "yt-dlp-2026.02.02.zip")
        with open(old, "wb") as target:
            target.write(make_zipapp("2026.02.02"))
        self.assert_updated(self.update(), "GitHub")

    # ------------------------------------------------------------ запасные пути

    def test_github_file_down_uses_pypi(self):
        self.site.modes["github"] = "fail"
        self.assert_updated(self.update(), "PyPI")
        with zipfile.ZipFile(os.path.join(self.own, "yt-dlp-%s.zip" % VERSION)) as archive:
            self.assertIn("yt_dlp-%s.dist-info/METADATA" % VERSION, archive.namelist())

    def test_github_api_down_uses_pypi(self):
        self.site.down.add("latest")
        self.assert_updated(self.update(), "PyPI")
        self.assertIn("/pypi", self.site.requests)

    def test_signed_list_unavailable_uses_pypi(self):
        self.site.down.add("sums")
        self.assert_updated(self.update(), "PyPI")

    def test_api_route_when_both_downloads_fail(self):
        self.site.modes.update(github="fail", pypi="fail")
        self.assert_updated(self.update(), "GitHub API")

    def test_github_com_closed_entirely(self):
        # и список сумм с подписью, и сам файл — через api.github.com
        self.site.down.add("download")
        self.site.modes["pypi"] = "fail"
        self.assert_updated(self.update(), "GitHub API")
        self.assertIn("/api/assets/2", self.site.requests)
        self.assertIn("/api/assets/3", self.site.requests)

    def test_everything_down(self):
        self.site.modes.update(github="fail", pypi="fail", api="fail")
        self.assert_nothing_installed(self.update(), "ни с одного источника")

    def test_nothing_reachable(self):
        self.site.down.update({"latest", "pypi"})
        self.assert_nothing_installed(self.update(), "есть ли новая версия")

    # ------------------------------------------------------------ подмена

    def test_bad_signature_stops_everything(self):
        self.site.sign_other = True
        self.assert_nothing_installed(self.update(), "подпись")
        # признак подмены — не повод идти за файлом на PyPI
        self.assertEqual(self.site.downloaded(), [])

    def test_github_digest_must_match_signed(self):
        self.site.github_digest = "0" * 64
        self.assert_nothing_installed(self.update(), "не совпадает с подписанной")

    def test_tampered_file(self):
        self.site.modes["github"] = "tampered"
        self.assert_nothing_installed(self.update(), "контрольной суммой")
        self.assertEqual(self.site.downloaded(), ["/download/%s/yt-dlp" % VERSION])

    def test_yt_dlp_signing_key_rejects_test_signature(self):
        updater = yu.YtdlpUpdater(self.locator, urls=self.site.urls(), allowed_hosts=["127.0.0.1"],
                                  require_https=False)
        updater.start()
        updater.wait(30)
        self.assert_nothing_installed(updater.snapshot(), "подпись")

    def test_other_version_inside(self):
        self.site.zipapp = make_zipapp("2026.10.01")
        self.assert_nothing_installed(self.update(), "другая версия")

    def test_must_run(self):
        with mock.patch.object(yu, "read_version", return_value=None):
            self.assert_nothing_installed(self.update(), "не запустился")

    def test_https_required_by_default(self):
        updater = yu.YtdlpUpdater(self.locator, urls=self.site.urls(), allowed_hosts=["127.0.0.1"])
        updater.start()
        updater.wait(30)
        self.assertEqual(updater.snapshot()["state"], "failed")
        self.assertEqual(self.site.requests, [])


class LocatorTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        patcher = mock.patch.object(yu, "bundled_version", lambda: BUNDLED)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def own_copy(self, version, file=None):
        name = file or "yt-dlp-%s.zip" % version
        if file is None:
            with open(os.path.join(self.dir, name), "wb") as target:
                target.write(make_zipapp(version))
        with open(os.path.join(self.dir, "version.json"), "w", encoding="utf-8") as target:
            json.dump({"version": version, "file": name}, target)

    def test_no_own_copy(self):
        locator = yu.YtdlpLocator(self.dir)
        self.assertEqual(locator.status(), {"version": BUNDLED, "own": False, "bundled": BUNDLED})
        self.assertEqual(locator.environment({"A": "1"}), {"A": "1"})

    def test_newer_copy_is_used(self):
        self.own_copy("2026.03.03")
        locator = yu.YtdlpLocator(self.dir)
        self.assertEqual(locator.status()["version"], "2026.03.03")
        self.assertTrue(locator.status()["own"])
        env = locator.environment({"PYTHONPATH": "C:\\other"})
        self.assertEqual(env["PYTHONPATH"], locator.active_copy() + os.pathsep + "C:\\other")

    def test_older_or_same_copy_is_ignored(self):
        # например, после пересборки программы со свежим yt-dlp
        for version in ("2025.12.31", BUNDLED):
            self.own_copy(version)
            locator = yu.YtdlpLocator(self.dir)
            self.assertIsNone(locator.active_copy())
            self.assertEqual(locator.status()["version"], BUNDLED)

    def test_without_bundled_any_copy_is_used(self):
        self.own_copy("2026.03.03")
        with mock.patch.object(yu, "bundled_version", lambda: None):
            locator = yu.YtdlpLocator(self.dir)
        self.assertIsNotNone(locator.active_copy())

    def test_record_cannot_point_outside(self):
        outside = os.path.join(os.path.dirname(self.dir), "evil.zip")
        self.own_copy("2026.03.03", file=os.path.join("..", "evil.zip"))
        self.assertFalse(os.path.exists(outside))
        self.assertIsNone(yu.YtdlpLocator(self.dir).own())

    def test_broken_record(self):
        with open(os.path.join(self.dir, "version.json"), "w", encoding="utf-8") as target:
            target.write("{broken")
        self.assertIsNone(yu.YtdlpLocator(self.dir).own())

    def test_version_key(self):
        self.assertLess(yu.version_key("2026.9.30"), yu.version_key("2026.10.01"))
        self.assertLess(yu.version_key("2026.10.21"), yu.version_key("2026.10.21.1"))
        self.assertIsNone(yu.version_key("2026.10.21-dev"))
        self.assertIsNone(yu.version_key(None))


if __name__ == "__main__":
    unittest.main()
