# -*- coding: utf-8 -*-
"""
ffmpeg: найти уже установленный или скачать свою копию.

Программа распространяется без ffmpeg. Если он уже есть на компьютере —
в PATH или в обычных местах установки, — программа находит его сама.
Если нет, его можно скачать кнопкой из окна.
"""

import glob
import json
import logging
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

FFMPEG_EXE = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
FFPROBE_EXE = "ffprobe.exe" if os.name == "nt" else "ffprobe"

# Так раньше писался путь по умолчанию: "найди в PATH".
_AUTO_VALUES = ("", "ffmpeg", "ffmpeg.exe")


def own_dir_for(settings):
    """Своя копия ffmpeg — в tools\\ffmpeg рядом с настройками программы."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.path)), "tools", "ffmpeg")


def common_locations():
    """Где ffmpeg оказывается после установки руками, через winget, scoop или chocolatey."""
    local = os.environ.get("LOCALAPPDATA", "")
    home = os.path.expanduser("~")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_data = os.environ.get("ProgramData", r"C:\ProgramData")
    winget = os.path.join(local, "Microsoft", "WinGet")
    return [
        os.path.join(winget, "Links", FFMPEG_EXE),
        *sorted(glob.glob(os.path.join(winget, "Packages", "*FFmpeg*", "*", "bin", FFMPEG_EXE)),
                reverse=True),
        os.path.join(home, "scoop", "shims", FFMPEG_EXE),
        os.path.join(home, "scoop", "apps", "ffmpeg", "current", "bin", FFMPEG_EXE),
        os.path.join(program_data, "chocolatey", "bin", FFMPEG_EXE),
        os.path.join("C:" + os.sep, "ffmpeg", "bin", FFMPEG_EXE),
        os.path.join(program_files, "ffmpeg", "bin", FFMPEG_EXE),
    ]


def read_version(path):
    """'ffmpeg version 9.0.2-essentials_build-www.gyan.dev ...' -> '9.0.2-essentials_build-...'."""
    try:
        result = subprocess.run([path, "-hide_banner", "-version"], capture_output=True,
                                timeout=15, creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = result.stdout.decode("utf-8", "replace").splitlines()[:1]
    match = re.match(r"ffmpeg version (\S+)", first[0]) if first else None
    return match.group(1) if match else None


class FFmpegLocator:
    """
    Где ffmpeg сейчас. Путь из настроек важнее всего, затем своя копия
    программы, затем PATH и обычные места установки. Ответ запоминается:
    поиск ходит по диску, а ffmpeg нужен часто. После установки — refresh().
    """

    def __init__(self, settings, own_dir):
        self._settings = settings
        self.own_dir = own_dir
        self._lock = threading.Lock()
        self._found = None
        self._searched = False
        self._versions = {}

    @property
    def own_path(self):
        return os.path.join(self.own_dir, FFMPEG_EXE)

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

    def directory(self):
        """Папка с ffmpeg: yt-dlp ищет в ней и ffprobe."""
        path = self.path()
        return os.path.dirname(path) if path else None

    def _find(self):
        configured = str(self._settings["ffmpeg_path"] or "").strip()
        if configured.lower() not in _AUTO_VALUES:
            chosen = shutil.which(configured) or (configured if os.path.isfile(configured) else None)
            if chosen:
                return os.path.abspath(chosen)
            logging.warning(tr("ffmpeg по пути из настроек не найден ({path}), ищу сама",
                               path=configured))
        for candidate in [self.own_path, shutil.which("ffmpeg"), *common_locations()]:
            if candidate and os.path.isfile(candidate):
                return os.path.abspath(candidate)
        return None

    def status(self):
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
        return {"path": path, "version": version, "own": own}


# Суммы архивов известных версий: сверены с GitHub и gyan.dev при выпуске
# программы. Если один из сайтов недоступен, сумма всё равно сверяется
# в двух местах — на оставшемся сайте и здесь.
KNOWN_SUMS = {
    "9.0.2": "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
}


class FFmpegInstaller(Installer):
    """
    Скачивание ffmpeg кнопкой из окна.

    Откуда: сборки gyan.dev, на которые ссылается ffmpeg.org. Один и тот же
    архив лежит на GitHub (GyanD/codexffmpeg) и на самом gyan.dev. Порядок:
    GitHub, gyan.dev, затем снова GitHub, но через API — другой адрес входа,
    на случай если режется именно github.com. Источник недоступен, рвёт связь
    или качает медленнее MIN_SPEED — следующий, с того же места.

    Как проверяется, что ничего не подменили:
      1. сумма sha256 архива берётся из независимых мест — у GitHub (он считает
         её сам, когда файл выкладывают), на сайте gyan.dev и из KNOWN_SUMS;
         нужно хотя бы два совпадения и ни одного расхождения;
      2. скачанный архив обязан дать ровно эту сумму;
      3. только https и только известные адреса; перенаправление проверяется
         до подключения;
      4. из архива берутся только ffmpeg.exe и ffprobe.exe по точному пути,
         остальное не распаковывается;
      5. установленный ffmpeg должен запуститься и назвать ту же версию.
    """

    NAME = "ffmpeg"
    URLS = {
        "version": "https://www.gyan.dev/ffmpeg/builds/release-version",
        "latest": "https://api.github.com/repos/GyanD/codexffmpeg/releases/latest",
        "release": "https://api.github.com/repos/GyanD/codexffmpeg/releases/tags/{version}",
        "checksum": "https://www.gyan.dev/ffmpeg/builds/packages/{name}.sha256",
        "github": "https://github.com/GyanD/codexffmpeg/releases/download/{version}/{name}",
        "gyan": "https://www.gyan.dev/ffmpeg/builds/packages/{name}",
    }
    ALLOWED_HOSTS = ("www.gyan.dev", "api.github.com", "github.com",
                     "objects.githubusercontent.com", "release-assets.githubusercontent.com")
    ARCHIVE = "ffmpeg-{version}-essentials_build.zip"
    MANUAL_PAGE = "https://www.gyan.dev/ffmpeg/builds/"

    def __init__(self, locator, on_change=None, urls=None, allowed_hosts=None,
                 require_https=True, known_sums=None):
        """urls, allowed_hosts и known_sums подменяются в тестах."""
        super().__init__(on_change, urls, allowed_hosts, require_https)
        self._locator = locator
        self._known_sums = KNOWN_SUMS if known_sums is None else known_sums

    def _work_dir(self):
        return self._locator.own_dir

    def _version(self):
        """Текущая версия: с gyan.dev, а если он недоступен — последний выпуск на GitHub."""
        problems = []
        try:
            version = self._get(self._urls["version"]).text.strip()
            if re.fullmatch(r"\d+(\.\d+){0,3}", version):
                return version
            problems.append("gyan.dev: %s" % version[:40])
        except InstallError as error:
            problems.append(str(error))
        try:
            version = str(self._get_json(self._urls["latest"]).get("tag_name", ""))
            if re.fullmatch(r"\d+(\.\d+){0,3}", version):
                return version
            problems.append("GitHub: %s" % version[:40])
        except InstallError as error:
            problems.append(str(error))
        raise InstallError(tr("не удалось узнать версию ffmpeg: {problems}",
                              problems="; ".join(problems)))

    def _plan(self):
        """Версия и источники архива с суммой, сверенной хотя бы в двух местах."""
        version = self._version()
        name = self.ARCHIVE.format(version=version)
        sums, problems, size, api_url = {}, [], None, None

        try:
            release = self._get_json(self._urls["release"].format(version=version))
            asset = next(a for a in release.get("assets", []) if a.get("name") == name)
            digest = str(asset.get("digest") or "").lower()
            if digest.startswith("sha256:"):
                sums["GitHub"] = digest[len("sha256:"):]
            size = int(asset.get("size") or 0) or None
            api_url = asset.get("url")
        except (InstallError, StopIteration) as error:
            problems.append("GitHub: %s" % (error if str(error) else name))

        try:
            text = self._get(self._urls["checksum"].format(name=name)).text.strip().lower()
            if text:
                sums["gyan.dev"] = text.split()[0]
        except InstallError as error:
            problems.append(str(error))

        if version in self._known_sums:
            sums[tr("программа")] = self._known_sums[version]

        expected = agreed_sum(sums, problems)
        if size is not None and not 0 < size <= self.MAX_SIZE:
            raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                  name=self.NAME, size=size))
        sources = [Source("GitHub", self._urls["github"].format(version=version, name=name),
                          expected, size),
                   Source("gyan.dev", self._urls["gyan"].format(name=name), expected, size)]
        if api_url:
            sources.append(Source("GitHub API", api_url, expected, size,
                                  {"Accept": "application/octet-stream"}))
        return version, sources

    def _install(self, archive_path, version, source):
        """Только ffmpeg и ffprobe по точному пути внутри архива — и сразу проверка."""
        folder = "ffmpeg-%s-essentials_build/" % version
        wanted = {folder + "bin/" + FFMPEG_EXE: FFMPEG_EXE,
                  folder + "bin/" + FFPROBE_EXE: FFPROBE_EXE,
                  folder + "LICENSE": "LICENSE.txt"}
        target_dir = self._locator.own_dir
        fresh = []
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                for inner in list(wanted)[:2]:
                    if inner not in names:
                        raise InstallError(tr("в архиве нет {name}", name=inner))
                for inner, outer in wanted.items():
                    if inner not in names:
                        continue
                    new = os.path.join(target_dir, outer + ".new")
                    with archive.open(inner) as packed, open(new, "wb") as target:
                        shutil.copyfileobj(packed, target, self.CHUNK)
                    fresh.append((new, os.path.join(target_dir, outer)))
        except zipfile.BadZipFile:
            raise InstallError(tr("архив ffmpeg повреждён"))

        found = read_version(fresh[0][0]) if fresh else None
        if not found or not found.startswith(version):
            for new, _ in fresh:
                util.remove_quietly(new)
            raise InstallError(tr("скачанный ffmpeg не запустился или назвал другую версию"))
        try:
            for new, final in fresh:
                os.replace(new, final)
        except OSError:
            for new, _ in fresh:
                util.remove_quietly(new)
            raise InstallError(tr("ffmpeg сейчас занят конвертацией — повтори, когда она закончится"))
        with open(os.path.join(target_dir, "version.json"), "w", encoding="utf-8") as target:
            json.dump({"version": version, "installed": time.time()}, target)
        self._locator.refresh()
        return self._locator.own_path
