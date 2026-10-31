# -*- coding: utf-8 -*-
"""
Поиск ffmpeg и его установка кнопкой. Установщик проверяется на локальном
сервере, который изображает GitHub и gyan.dev и умеет вредничать: падать,
рвать связь, тормозить, не уметь продолжать, подменять архив и сумму.
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
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from streamvault import ffmpeg as ff
from streamvault.settings import Settings
from tests.helpers import wait_for

VERSION = "1.2.3"
NAME = "ffmpeg-%s-essentials_build.zip" % VERSION
FOLDER = "ffmpeg-%s-essentials_build/" % VERSION


def make_archive(with_ffmpeg=True, binary=b"FAKEFFMPEG " + VERSION.encode()):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        if with_ffmpeg:
            archive.writestr(FOLDER + "bin/" + ff.FFMPEG_EXE, binary)
        archive.writestr(FOLDER + "bin/" + ff.FFPROBE_EXE, b"FAKEPROBE")
        archive.writestr(FOLDER + "LICENSE", b"GPL")
        archive.writestr(FOLDER + "bin/ffplay.exe", b"not needed")
        # путь наружу из папки установки — распаковываться не должен
        archive.writestr("../../evil.exe", b"evil")
    return buffer.getvalue()


def fake_version(path):
    """Вместо запуска: подставной ffmpeg — это метка "FAKEFFMPEG 1.2.3" и что угодно после."""
    with open(path, "rb") as source:
        match = re.match(rb"FAKEFFMPEG (\d+(?:\.\d+)*)", source.read(64))
    return match.group(1).decode("ascii") if match else None


class FakeSite:
    """
    Изображает GitHub и gyan.dev сразу. Поведение источника архива —
    modes["github"] / modes["gyan"]: ok, fail, drop (отдать половину
    и оборвать), slow, norange (не уметь продолжать), redirect (на чужой адрес).
    """

    def __init__(self):
        self.archive = make_archive()
        self.served = None           # что отдавать вместо архива (подмена)
        self.github_sum = None       # сумма у GitHub, по умолчанию верная
        self.site_sum = None         # сумма на gyan.dev, по умолчанию верная
        self.down = set()            # недоступные адреса: version, latest, release, checksum
        self.modes = {"github": "ok", "gyan": "ok", "api": "ok"}
        self.requests = []           # (источник, Range)
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, status=200, headers=(), slow=False, drop=False):
                self.send_response(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if drop:
                    body = body[:len(body) // 2]
                try:
                    if slow:
                        for start in range(0, len(body), 2000):
                            self.wfile.write(body[start:start + 2000])
                            time.sleep(0.05)
                    else:
                        self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass
                if drop:
                    self.close_connection = True

            def do_GET(self):
                digest = hashlib.sha256(site.archive).hexdigest()
                path = self.path
                for key, prefix in (("version", "/version"), ("latest", "/latest"),
                                    ("release", "/release/"), ("checksum", "/checksum/")):
                    if path.startswith(prefix) and key in site.down:
                        return self.send(b"", 503)
                if path == "/version":
                    return self.send(VERSION.encode())
                if path == "/latest":
                    return self.send(json.dumps({"tag_name": VERSION}).encode())
                if path == "/release/" + VERSION:
                    asset = {"name": NAME, "size": len(site.archive),
                             "digest": "sha256:" + (site.github_sum or digest),
                             "url": site.base + "/api/" + NAME}
                    return self.send(json.dumps({"assets": [asset]}).encode())
                if path == "/checksum/" + NAME:
                    return self.send(("%s *%s" % (site.site_sum or digest, NAME)).encode())
                for source in ("github", "gyan"):
                    if path == "/%s/%s" % (source, NAME):
                        return self.file(source)
                if path == "/api/" + NAME:
                    # как у GitHub: без этого заголовка API отвечает сведениями, а не файлом
                    if self.headers.get("Accept") != "application/octet-stream":
                        return self.send(json.dumps({"name": NAME}).encode())
                    return self.file("api")
                self.send(b"", 404)

            def file(self, source):
                mode = site.modes[source]
                wanted = self.headers.get("Range")
                site.requests.append((source, wanted))
                body = site.served if site.served is not None else site.archive
                if mode == "fail":
                    return self.send(b"", 500)
                if mode == "redirect":
                    port = site.server.server_address[1]
                    return self.send(b"", 302, [("Location", "http://localhost:%d/x" % port)])
                if wanted and mode != "norange":
                    offset = int(wanted.split("=")[1].split("-")[0])
                    return self.send(body[offset:], 206,
                                     [("Content-Range", "bytes %d-%d/%d"
                                       % (offset, len(body) - 1, len(body)))],
                                     slow=mode == "slow", drop=mode == "drop")
                self.send(body, slow=mode == "slow", drop=mode == "drop")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def urls(self):
        return {"version": self.base + "/version", "latest": self.base + "/latest",
                "release": self.base + "/release/{version}",
                "checksum": self.base + "/checksum/{name}",
                "github": self.base + "/github/{name}", "gyan": self.base + "/gyan/{name}"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class InstallerTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.dir, "settings.json"))
        self.locator = ff.FFmpegLocator(self.settings, os.path.join(self.dir, "tools", "ffmpeg"))
        self.site = FakeSite()
        self.addCleanup(self.site.close)
        self.states = []
        patcher = mock.patch("streamvault.ffmpeg.read_version", fake_version)
        patcher.start()
        self.addCleanup(patcher.stop)
        # журнал установщика здесь не проверяется — только мешал бы выводу тестов
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def installer(self, known_sums=None, **tuning):
        installer = ff.FFmpegInstaller(
            self.locator, on_change=lambda state: self.states.append(dict(state)),
            urls=self.site.urls(), allowed_hosts=["127.0.0.1"], require_https=False,
            known_sums=known_sums if known_sums is not None else {})
        for key, value in tuning.items():
            setattr(installer, key, value)
        return installer

    def install(self, **kwargs):
        installer = self.installer(**kwargs)
        installer.start()
        installer.wait(60)
        return installer.snapshot()

    def installed(self):
        own = self.locator.own_dir
        return sorted(os.listdir(own)) if os.path.isdir(own) else []

    def sources_used(self):
        return [state["source"] for state in self.states if state.get("source")]

    def assert_installed(self, result):
        self.assertEqual(result["state"], "done", result["error"])
        self.assertEqual(self.installed(),
                         sorted([ff.FFMPEG_EXE, ff.FFPROBE_EXE, "LICENSE.txt", "version.json"]))

    # ------------------------------------------------------------ обычный путь

    def test_installs_only_what_is_needed(self):
        result = self.install()
        self.assert_installed(result)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil.exe")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "tools", "evil.exe")))
        self.assertEqual(self.locator.path(), os.path.abspath(self.locator.own_path))
        self.assertEqual(set(self.sources_used()), {"GitHub"})
        self.assertEqual(self.states[-1]["state"], "done")

    # ------------------------------------------------------------ запасной источник

    def test_github_down_uses_gyan(self):
        self.site.modes["github"] = "fail"
        result = self.install()
        self.assert_installed(result)
        self.assertEqual(self.sources_used()[-1], "gyan.dev")

    def test_broken_connection_resumes_from_same_place(self):
        # GitHub каждый раз отдаёт половину запрошенного и обрывается:
        # вторая попытка продолжает с середины, gyan.dev докачивает остаток
        self.site.archive = make_archive(binary=b"FAKEFFMPEG " + VERSION.encode() + os.urandom(20_000))
        self.site.modes["github"] = "drop"
        result = self.install()
        self.assert_installed(result)
        ranges = [wanted for _, wanted in self.site.requests]
        self.assertIsNone(ranges[0])
        self.assertTrue(all(wanted and wanted.startswith("bytes=") for wanted in ranges[1:]))
        self.assertEqual([source for source, _ in self.site.requests],
                         ["github", "github", "gyan"])

    def test_slow_source_is_left_for_the_next(self):
        self.site.archive = make_archive(binary=b"FAKEFFMPEG " + VERSION.encode() + os.urandom(150_000))
        self.site.modes["github"] = "slow"
        result = self.install(SPEED_WINDOW=0.3, MIN_SPEED=1_000_000)
        self.assert_installed(result)
        self.assertEqual(self.sources_used()[-1], "gyan.dev")
        # продолжили с того, что уже скачали с GitHub, а не с нуля
        self.assertTrue(self.site.requests[-1][1].startswith("bytes="))

    def test_server_without_resume_starts_over(self):
        self.site.archive = make_archive(binary=b"FAKEFFMPEG " + VERSION.encode() + os.urandom(20_000))
        self.site.modes.update(github="drop", gyan="norange")
        result = self.install()
        self.assert_installed(result)
        self.assertEqual(self.site.requests[-1][0], "gyan")
        self.assertTrue(self.site.requests[-1][1].startswith("bytes="))

    def test_api_route_last(self):
        # github.com и gyan.dev не отдают — тот же файл через api.github.com
        self.site.modes.update(github="fail", gyan="fail")
        self.assert_installed(self.install())
        self.assertEqual(self.sources_used()[-1], "GitHub API")
        self.assertEqual(self.site.requests[-1][0], "api")

    def test_all_sources_down(self):
        self.site.modes.update(github="fail", gyan="fail", api="fail")
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("ни с одного источника", result["error"])
        self.assertEqual(self.installed(), [])

    def test_redirect_to_unknown_host(self):
        # к чужому адресу не подключаемся; запасной источник выручает
        self.site.modes["github"] = "redirect"
        self.assert_installed(self.install())
        self.site.modes.update(github="redirect", gyan="redirect", api="redirect")
        shutil.rmtree(self.locator.own_dir)
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("чужой адрес", result["error"])

    # ------------------------------------------------------------ контрольная сумма

    def test_github_api_down_with_known_sum(self):
        digest = hashlib.sha256(self.site.archive).hexdigest()
        self.site.down.update({"release", "latest"})
        result = self.install(known_sums={VERSION: digest})
        self.assert_installed(result)

    def test_github_api_down_without_known_sum(self):
        self.site.down.update({"release", "latest"})
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("в двух местах", result["error"])
        self.assertEqual(self.installed(), [])

    def test_gyan_down_completely(self):
        digest = hashlib.sha256(self.site.archive).hexdigest()
        self.site.down.update({"version", "checksum"})
        self.site.modes["gyan"] = "fail"
        result = self.install(known_sums={VERSION: digest})
        self.assert_installed(result)

    def test_sums_must_agree(self):
        self.site.site_sum = "0" * 64
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("не совпадают", result["error"])
        self.assertEqual(self.installed(), [])
        # встроенная сумма тоже голосует: расходится с сайтами — не ставим
        self.site.site_sum = None
        result = self.install(known_sums={VERSION: "1" * 64})
        self.assertIn("не совпадают", result["error"])

    def test_tampered_archive_is_rejected(self):
        tampered = make_archive(binary=b"FAKEFFMPEG " + VERSION.encode() + b" evil")
        self.site.served = tampered[:len(self.site.archive)].ljust(len(self.site.archive), b"x")
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("контрольной суммой", result["error"])
        # ни установленного, ни недокачанного
        self.assertEqual(self.installed(), [])

    # ------------------------------------------------------------ архив и запуск

    def test_archive_without_ffmpeg(self):
        self.site.archive = make_archive(with_ffmpeg=False)
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("в архиве нет", result["error"])

    def test_binary_must_run(self):
        self.site.archive = make_archive(binary=b"MZ not really ffmpeg")
        result = self.install()
        self.assertEqual(result["state"], "failed")
        self.assertIn("не запустился", result["error"])
        self.assertEqual(self.installed(), [])

    def test_cancel(self):
        self.site.archive = make_archive(binary=b"FAKEFFMPEG " + VERSION.encode() + os.urandom(200_000))
        self.site.modes.update(github="slow", gyan="slow")
        installer = self.installer()
        installer.start()
        self.assertTrue(wait_for(lambda: installer.snapshot()["state"] == "downloading"))
        installer.cancel()
        installer.wait(15)
        self.assertEqual(installer.snapshot()["state"], "cancelled")
        self.assertEqual(self.installed(), [])

    def test_https_required_by_default(self):
        installer = ff.FFmpegInstaller(self.locator, urls=self.site.urls(),
                                       allowed_hosts=["127.0.0.1"])
        installer.start()
        installer.wait(15)
        self.assertEqual(installer.snapshot()["state"], "failed")

    def test_known_sum_matches_both_sites(self):
        # встроенная сумма 9.0.2 — та, что опубликована на GitHub и gyan.dev
        self.assertEqual(ff.KNOWN_SUMS["9.0.2"],
                         "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba")


class LocatorTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.dir, "settings.json"))
        self.own = os.path.join(self.dir, "own")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_binary(self, folder):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, ff.FFMPEG_EXE)
        open(path, "wb").close()
        return path

    def test_manual_path_wins(self):
        manual = self.fake_binary(os.path.join(self.dir, "manual"))
        self.fake_binary(self.own)
        self.settings.update(ffmpeg_path=manual)
        self.assertEqual(ff.FFmpegLocator(self.settings, self.own).path(), os.path.abspath(manual))

    def test_own_copy_before_system(self):
        own = self.fake_binary(self.own)
        locator = ff.FFmpegLocator(self.settings, self.own)
        self.assertEqual(locator.path(), os.path.abspath(own))
        with mock.patch("streamvault.ffmpeg.read_version", return_value="9.0.2"):
            self.assertEqual(locator.status(), {"path": os.path.abspath(own), "version": "9.0.2",
                                                "own": True})

    def test_missing_manual_path_falls_back(self):
        own = self.fake_binary(self.own)
        self.settings.update(ffmpeg_path=os.path.join(self.dir, "нет", "ffmpeg.exe"))
        with self.assertLogs(level="WARNING"):
            found = ff.FFmpegLocator(self.settings, self.own).path()
        self.assertEqual(found, os.path.abspath(own))

    def test_nothing_anywhere(self):
        with mock.patch("streamvault.ffmpeg.shutil.which", return_value=None), \
                mock.patch("streamvault.ffmpeg.common_locations", return_value=[]):
            locator = ff.FFmpegLocator(self.settings, self.own)
            self.assertIsNone(locator.path())
            self.assertIsNone(locator.directory())
            # появился — после refresh() виден без перезапуска
            own = self.fake_binary(self.own)
            self.assertIsNone(locator.path())
            self.assertEqual(locator.refresh(), os.path.abspath(own))

    def test_old_default_means_search(self):
        self.settings.update(ffmpeg_path="ffmpeg")
        own = self.fake_binary(self.own)
        self.assertEqual(ff.FFmpegLocator(self.settings, self.own).path(), os.path.abspath(own))

    @unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
    def test_reads_real_version(self):
        self.assertRegex(ff.read_version(shutil.which("ffmpeg")), r"^\d+")


if __name__ == "__main__":
    unittest.main()
