# -*- coding: utf-8 -*-
"""
TwitchDownloader: чат записей и клипов Twitch.

Программа распространяется без него. Свою копию TwitchDownloaderCLI можно
скачать кнопкой из окна; уже установленный (например, через scoop)
находится в PATH. Сам чат качает archive/chat.py.
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

CLI_EXE = "TwitchDownloaderCLI.exe" if os.name == "nt" else "TwitchDownloaderCLI"


def own_dir_for(settings):
    """Своя копия — в tools\\TwitchDownloader рядом с настройками программы."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.path)), "tools", "TwitchDownloader")


def read_version(path):
    """
    'TwitchDownloaderCLI 1.56.5+f8335ca…' -> '1.56.5'. Не запустился — None.
    Версию он печатает в поток ошибок и выходит с кодом 1 — так и задумано.
    """
    try:
        result = subprocess.run([path, "--version"], capture_output=True, timeout=30,
                                stdin=subprocess.DEVNULL, creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout + result.stderr).decode("utf-8", "replace")
    match = re.search(r"TwitchDownloaderCLI (\d+(?:\.\d+)+)", output)
    return match.group(1) if match else None


def _version_key(text):
    """'1.56.5' → (1, 56, 5); непонятное — самое старое."""
    try:
        return tuple(int(part) for part in str(text or "").split("."))
    except ValueError:
        return ()


class TwitchDownloaderLocator:
    """Где TwitchDownloaderCLI: своя копия программы, иначе PATH. После установки — refresh()."""

    def __init__(self, own_dir):
        self.own_dir = own_dir
        self._lock = threading.Lock()
        self._found = None
        self._searched = False
        self._versions = {}

    @property
    def own_path(self):
        return os.path.join(self.own_dir, CLI_EXE)

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

    def command(self):
        """Как запускать TwitchDownloaderCLI: список для subprocess или None, если его нет."""
        path = self.path()
        return [path] if path else None

    def _find(self):
        for candidate in (self.own_path, shutil.which("TwitchDownloaderCLI")):
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


# Суммы архивов известных версий: сверены с GitHub и scoop при выпуске
# программы. Если одно из мест недоступно, сумма всё равно сверяется
# в двух — в оставшемся и здесь.
KNOWN_SUMS = {
    "1.56.5": "8b1b0695f2b1b6bf0d2535fab4b84032951cded8cf4078dfdf4d58e391c813a0",
}


class TwitchDownloaderInstaller(Installer):
    """
    Скачивание TwitchDownloaderCLI кнопкой из окна — с GitHub, из выпусков
    lay295/TwitchDownloader, а если режется github.com — через api.github.com.

    Как проверяется, что ничего не подменили:
      1. сумма sha256 архива — у GitHub (он считает её сам), в каталоге
         scoop (его робот скачивает архив независимо и записывает сумму)
         и из KNOWN_SUMS; нужно хотя бы два совпадения и ни одного
         расхождения. Новейшую версию scoop может ещё не знать — тогда
         ставится та, что сверяется в двух местах;
      2. скачанный архив обязан дать ровно эту сумму;
      3. только https и только известные адреса;
      4. из архива берётся только TwitchDownloaderCLI.exe и лицензии;
      5. установленный TwitchDownloaderCLI должен запуститься и назвать
         ту же версию.
    """

    NAME = "TwitchDownloader"
    URLS = {
        "latest": "https://api.github.com/repos/lay295/TwitchDownloader/releases/latest",
        "release": "https://api.github.com/repos/lay295/TwitchDownloader/releases/tags/{version}",
        "scoop": "https://raw.githubusercontent.com/ScoopInstaller/Main/master/bucket/twitchdownloader-cli.json",
        "github": "https://github.com/lay295/TwitchDownloader/releases/download/{version}/{name}",
    }
    ALLOWED_HOSTS = ("api.github.com", "github.com", "raw.githubusercontent.com",
                     "objects.githubusercontent.com", "release-assets.githubusercontent.com")
    ARCHIVE = "TwitchDownloaderCLI-{version}-Windows-x64.zip"
    MANUAL_PAGE = "https://github.com/lay295/TwitchDownloader/releases"
    MAX_SIZE = 300 * 1024 * 1024
    # что берётся из архива и как называется у нас
    MEMBERS = {CLI_EXE: CLI_EXE, "COPYRIGHT.txt": "COPYRIGHT.txt",
               "THIRD-PARTY-LICENSES.txt": "THIRD-PARTY-LICENSES.txt"}

    def __init__(self, locator, on_change=None, urls=None, allowed_hosts=None,
                 require_https=True, known_sums=None):
        """urls, allowed_hosts и known_sums подменяются в тестах."""
        super().__init__(on_change, urls, allowed_hosts, require_https)
        self._locator = locator
        self._known_sums = KNOWN_SUMS if known_sums is None else known_sums

    def _work_dir(self):
        return self._locator.own_dir

    def _plan(self):
        problems = []
        latest = None
        try:
            latest = str(self._get_json(self._urls["latest"]).get("tag_name", ""))
            if not re.fullmatch(r"\d+(\.\d+){1,3}", latest):
                problems.append("GitHub: %s" % latest[:40])
                latest = None
        except InstallError as error:
            problems.append(str(error))
        scoop_version, scoop_sum = self._scoop(problems)

        current = self._locator.status()
        for version in dict.fromkeys(filter(None, [latest, scoop_version])):
            if current["own"] and _version_key(current["version"]) >= _version_key(version):
                # своя копия не старее — ставить нечего (и откатывать на старую не надо)
                self._set(version=current["version"])
                return None
            name = self.ARCHIVE.format(version=version)
            sums, size, api_url = {}, None, None
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
            if version == scoop_version and scoop_sum:
                sums["scoop"] = scoop_sum
            if version in self._known_sums:
                sums[tr("программа")] = self._known_sums[version]
            if len(sums) < 2:
                # эту версию в двух местах не сверить — может, выйдет со следующей
                problems.append(tr("{version}: сумма есть только у {sources}", version=version,
                                   sources=", ".join(sums) or "—"))
                continue
            expected = agreed_sum(sums, problems)
            if size is not None and not 0 < size <= self.MAX_SIZE:
                raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                      name=self.NAME, size=size))
            sources = [Source("GitHub", self._urls["github"].format(version=version, name=name),
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
            expected_url = self._urls["github"].format(version=version,
                                                       name=self.ARCHIVE.format(version=version))
            digest = str(entry.get("hash") or "").lower()
            if entry.get("url") != expected_url or not re.fullmatch(r"\d+(\.\d+){1,3}", version):
                problems.append(tr("scoop: ссылка не на выпуск TwitchDownloader"))
                return None, None
            return version, digest
        except InstallError as error:
            problems.append("scoop: %s" % error)
            return None, None

    def _install(self, archive_path, version, source):
        """Только TwitchDownloaderCLI.exe и лицензии по точному пути — и сразу проверка."""
        target_dir = self._locator.own_dir
        fresh = []
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = set(archive.namelist())
                if CLI_EXE not in names:
                    raise InstallError(tr("в архиве нет {name}", name=CLI_EXE))
                for inner, outer in self.MEMBERS.items():
                    if inner not in names:
                        continue
                    new = os.path.join(target_dir, outer + ".new")
                    with archive.open(inner) as packed, open(new, "wb") as target:
                        shutil.copyfileobj(packed, target, self.CHUNK)
                    fresh.append((new, os.path.join(target_dir, outer)))
        except zipfile.BadZipFile:
            raise InstallError(tr("архив TwitchDownloader повреждён"))

        if read_version(fresh[0][0]) != version:
            for new, _ in fresh:
                util.remove_quietly(new)
            raise InstallError(tr("скачанный TwitchDownloader не запустился или назвал другую версию"))
        try:
            for new, final in fresh:
                os.replace(new, final)
        except OSError:
            for new, _ in fresh:
                util.remove_quietly(new)
            raise InstallError(tr("TwitchDownloader сейчас занят чатом — повтори, когда он закончит"))
        with open(os.path.join(target_dir, "version.json"), "w", encoding="utf-8") as target:
            json.dump({"version": version, "installed": time.time()}, target)
        self._locator.refresh()
        return self._locator.own_path
