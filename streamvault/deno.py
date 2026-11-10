# -*- coding: utf-8 -*-
"""
Deno: JavaScript для yt-dlp.

YouTube прячет ссылки на видео за задачками на JavaScript; браузер решает
их сам, а yt-dlp — только с JavaScript-средой рядом (по умолчанию ищет Deno
в PATH). Без неё yt-dlp пока обходится запасными путями, но предупреждает,
что это устарело и часть форматов пропадает.

Программа распространяется без Deno. Свою копию (один deno.exe) можно
скачать кнопкой из окна; уже установленный находится в PATH. Своя копия
добавляется в PATH запускаемых программ (util.child_environment), так
yt-dlp и находит её. Node.js, если он есть, yt-dlp тоже разрешено брать
(util.YTDLP: --js-runtimes node), но Deno у него в приоритете.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile

from . import util
from .i18n import tr
from .installer import InstallError, Installer, Source, agreed_sum

DENO_EXE = "deno.exe" if os.name == "nt" else "deno"


def own_dir_for(settings):
    """Своя копия — в tools\\Deno рядом с настройками программы."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.path)), "tools", "Deno")


def read_version(path):
    """'deno 2.9.8 (stable, release, x86_64-pc-windows-msvc)…' -> '2.9.8'. Не запустился — None."""
    try:
        result = subprocess.run([path, "--version"], capture_output=True, timeout=30,
                                stdin=subprocess.DEVNULL, creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"deno (\d+(?:\.\d+)+)", (result.stdout + result.stderr).decode("utf-8", "replace"))
    return match.group(1) if match else None


def _version_key(text):
    """'2.9.8' или 'v2.9.8' → (2, 9, 8); непонятное — самое старое."""
    try:
        return tuple(int(part) for part in str(text or "").lstrip("v").split("."))
    except ValueError:
        return ()


class DenoLocator:
    """Где Deno: своя копия программы, иначе PATH. После установки — refresh()."""

    def __init__(self, own_dir):
        self.own_dir = own_dir
        self._lock = threading.Lock()
        self._found = None
        self._searched = False
        self._versions = {}
        # своя копия — в PATH запускаемых программ: yt-dlp ищет Deno там
        util.add_child_path(own_dir)

    @property
    def own_path(self):
        return os.path.join(self.own_dir, DENO_EXE)

    def path(self):
        with self._lock:
            if not self._searched:
                self._found = self._find()
                self._searched = True
            return self._found

    def refresh(self):
        with self._lock:
            self._searched = False
        return self.path()

    def _find(self):
        for candidate in (self.own_path, shutil.which("deno")):
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def status(self):
        """
        {"path", "version", "own", "node"}: node — Deno нет, но есть Node.js,
        и yt-dlp возьмёт его (окну — сказать, что и так годится).
        """
        path = self.path()
        version = None
        if path:
            try:
                key = (path, os.path.getmtime(path))
            except OSError:
                key = None
            if key not in self._versions:
                self._versions[key] = read_version(path)
            version = self._versions[key]
        own = bool(path) and os.path.normcase(path) == os.path.normcase(os.path.abspath(self.own_path))
        return {"path": path, "version": version, "own": own,
                "node": None if path else shutil.which("node")}


class DenoInstaller(Installer):
    """
    Скачивание Deno кнопкой из окна — с GitHub, из выпусков denoland/deno,
    а если режется github.com — через api.github.com.

    Как проверяется, что ничего не подменили (как у TwitchDownloader):
      1. сумма sha256 архива — у GitHub (он считает её сам) и в каталоге
         scoop (его робот скачивает архив независимо); нужны обе и одинаковые;
      2. скачанный архив обязан дать ровно эту сумму;
      3. только https и только известные адреса;
      4. из архива берётся только deno.exe;
      5. установленный Deno должен запуститься и назвать ту же версию.
    """

    NAME = "Deno"
    URLS = {
        "latest": "https://api.github.com/repos/denoland/deno/releases/latest",
        "release": "https://api.github.com/repos/denoland/deno/releases/tags/v{version}",
        "scoop": "https://raw.githubusercontent.com/ScoopInstaller/Main/master/bucket/deno.json",
        "github": "https://github.com/denoland/deno/releases/download/v{version}/{name}",
    }
    ALLOWED_HOSTS = ("api.github.com", "github.com", "raw.githubusercontent.com",
                     "objects.githubusercontent.com", "release-assets.githubusercontent.com")
    ARCHIVE = "deno-x86_64-pc-windows-msvc.zip"
    MANUAL_PAGE = "https://github.com/denoland/deno/releases"
    MAX_SIZE = 300 * 1024 * 1024

    def __init__(self, locator, on_change=None, urls=None, allowed_hosts=None, require_https=True):
        """urls и allowed_hosts подменяются в тестах."""
        super().__init__(on_change, urls, allowed_hosts, require_https)
        self._locator = locator

    def _work_dir(self):
        return self._locator.own_dir

    def _latest(self):
        tag = str(self._get_json(self._urls["latest"]).get("tag_name", ""))
        if not re.fullmatch(r"v?\d+(\.\d+){1,3}", tag):
            raise InstallError("GitHub: %s" % tag[:40])
        return tag.lstrip("v")

    def newer_version(self):
        """Есть ли Deno новее своей копии: версия или None (не стоит или стоит свой у человека — None)."""
        current = self._locator.status()
        if self.busy() or not current["path"] or not current["own"]:
            return None
        version = self._latest()
        return version if _version_key(version) > _version_key(current["version"]) else None

    def _plan(self):
        problems = []
        latest = None
        try:
            latest = self._latest()
        except InstallError as error:
            problems.append(str(error))
        scoop_version, scoop_sum = self._scoop(problems)

        current = self._locator.status()
        for version in dict.fromkeys(filter(None, [latest, scoop_version])):
            if current["own"] and _version_key(current["version"]) >= _version_key(version):
                # своя копия не старее — ставить нечего (и откатывать на старую не надо)
                self._set(version=current["version"])
                return None
            sums, size, api_url = {}, None, None
            try:
                release = self._get_json(self._urls["release"].format(version=version))
                asset = next(a for a in release.get("assets", []) if a.get("name") == self.ARCHIVE)
                digest = str(asset.get("digest") or "").lower()
                if digest.startswith("sha256:"):
                    sums["GitHub"] = digest[len("sha256:"):]
                size = int(asset.get("size") or 0) or None
                api_url = asset.get("url")
            except (InstallError, StopIteration) as error:
                problems.append("GitHub: %s" % (error if str(error) else self.ARCHIVE))
            if version == scoop_version and scoop_sum:
                sums["scoop"] = scoop_sum
            if len(sums) < 2:
                # эту версию в двух местах не сверить — может, выйдет со следующей
                problems.append(tr("{version}: сумма есть только у {sources}", version=version,
                                   sources=", ".join(sums) or "—"))
                continue
            expected = agreed_sum(sums, problems)
            if size is not None and not 0 < size <= self.MAX_SIZE:
                raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                      name=self.NAME, size=size))
            sources = [Source("GitHub", self._urls["github"].format(version=version, name=self.ARCHIVE),
                              expected, size)]
            if api_url:
                sources.append(Source("GitHub API", api_url, expected, size,
                                      {"Accept": "application/octet-stream"}))
            return version, sources
        raise InstallError(tr("не удалось сверить контрольную сумму в двух местах: {problems}",
                              problems="; ".join(problems)))

    def _scoop(self, problems):
        """Версия и сумма из каталога scoop — если он ссылается ровно на архив с GitHub."""
        try:
            manifest = self._get_json(self._urls["scoop"])
            version = str(manifest.get("version", ""))
            entry = (manifest.get("architecture") or {}).get("64bit") or {}
            expected_url = self._urls["github"].format(version=version, name=self.ARCHIVE)
            digest = str(entry.get("hash") or "").lower()
            if entry.get("url") != expected_url or not re.fullmatch(r"\d+(\.\d+){1,3}", version):
                problems.append(tr("scoop: ссылка не на выпуск {name}", name=self.NAME))
                return None, None
            return version, digest
        except InstallError as error:
            problems.append("scoop: %s" % error)
            return None, None

    def _install(self, archive_path, version, source):
        """Только deno.exe по точному пути — и сразу проверка."""
        target_dir = self._locator.own_dir
        new = os.path.join(target_dir, DENO_EXE + ".new")
        try:
            with zipfile.ZipFile(archive_path) as archive:
                if DENO_EXE not in archive.namelist():
                    raise InstallError(tr("в архиве нет {name}", name=DENO_EXE))
                with archive.open(DENO_EXE) as packed, open(new, "wb") as target:
                    shutil.copyfileobj(packed, target, self.CHUNK)
        except zipfile.BadZipFile:
            util.remove_quietly(new)
            raise InstallError(tr("архив {name} повреждён", name=self.NAME))
        if read_version(new) != version:
            util.remove_quietly(new)
            raise InstallError(tr("скачанный {name} не запустился или назвал другую версию", name=self.NAME))
        try:
            os.replace(new, self._locator.own_path)
        except OSError:
            util.remove_quietly(new)
            raise InstallError(tr("Deno сейчас занят — повтори, когда закончатся загрузки с YouTube"))
        with open(os.path.join(target_dir, "version.json"), "w", encoding="utf-8") as target:
            json.dump({"version": version, "installed": time.time()}, target)
        self._locator.refresh()
        return self._locator.own_path
