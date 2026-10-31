# -*- coding: utf-8 -*-
"""
yt-dlp: какой запускать и как обновить его без пересборки программы.

Сайты меняются, и yt-dlp перестаёт их понимать, пока его не обновят —
новые версии выходят раз в несколько недель. Обновлённая копия лежит
в tools\\yt-dlp рядом с настройками и запускается вместо встроенного
в программу (или установленного через pip), только если она новее.
"""

import glob
import importlib.metadata
import json
import logging
import os
import re
import subprocess
import threading
import time
import zipfile

from .. import openpgp, util
from ..i18n import tr
from ..installer import InstallError, Installer, Source

OWN_FILE = "yt-dlp-{version}.zip"
VERSION_FILE = "version.json"

# Ключ, которым разработчики yt-dlp подписывают список сумм каждого выпуска
# (public.key в их репозитории; тот же ключ — на keys.openpgp.org).
YTDLP_KEY_FINGERPRINT = "AC0CBBE6848D6A873464AF4E57CF65933B5A7581"
YTDLP_KEY = """\
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBGP78C4BEAD0rF9zjGPAt0thlt5C1ebzccAVX7Nb1v+eqQjk+WEZdTETVCg3
WAM5ngArlHdm/fZqzUgO+pAYrB60GKeg7ffUDf+S0XFKEZdeRLYeAaqqKhSibVal
DjvOBOztu3W607HLETQAqA7wTPuIt2WqmpL60NIcyr27LxqmgdN3mNvZ2iLO+bP0
nKR/C+PgE9H4ytywDa12zMx6PmZCnVOOOu6XZEFmdUxxdQ9fFDqd9LcBKY2LDOcS
Yo1saY0YWiZWHtzVoZu1kOzjnS5Fjq/yBHJLImDH7pNxHm7s/PnaurpmQFtDFruk
t+2lhDnpKUmGr/I/3IHqH/X+9nPoS4uiqQ5HpblB8BK+4WfpaiEg75LnvuOPfZIP
KYyXa/0A7QojMwgOrD88ozT+VCkKkkJ+ijXZ7gHNjmcBaUdKK7fDIEOYI63Lyc6Q
WkGQTigFffSUXWHDCO9aXNhP3ejqFWgGMtCUsrbkcJkWuWY7q5ARy/05HbSM3K4D
U9eqtnxmiV1WQ8nXuI9JgJQRvh5PTkny5LtxqzcmqvWO9TjHBbrs14BPEO9fcXxK
L/CFBbzXDSvvAgArdqqlMoncQ/yicTlfL6qzJ8EKFiqW14QMTdAn6SuuZTodXCTi
InwoT7WjjuFPKKdvfH1GP4bnqdzTnzLxCSDIEtfyfPsIX+9GI7Jkk/zZjQARAQAB
tDdTaW1vbiBTYXdpY2tpICh5dC1kbHAgc2lnbmluZyBrZXkpIDxjb250YWN0QGdy
dWI0ay54eXo+iQJOBBMBCgA4FiEErAy75oSNaoc0ZK9OV89lkztadYEFAmP78C4C
GwMFCwkIBwIGFQoJCAsCBBYCAwECHgECF4AACgkQV89lkztadYEVqQ//cW7TxhXg
7Xbh2EZQzXml0egn6j8QaV9KzGragMiShrlvTO2zXfLXqyizrFP4AspgjSn/4NrI
8mluom+Yi+qr7DXT4BjQqIM9y3AjwZPdywe912Lxcw52NNoPZCm24I9T7ySc8lmR
FQvZC0w4H/VTNj/2lgJ1dwMflpwvNRiWa5YzcFGlCUeDIPskLx9++AJE+xwU3LYm
jQQsPBqpHHiTBEJzMLl+rfd9Fg4N+QNzpFkTDW3EPerLuvJniSBBwZthqxeAtw4M
UiAXh6JvCc2hJkKCoygRfM281MeolvmsGNyQm+axlB0vyldiPP6BnaRgZlx+l6MU
cPqgHblb7RW5j9lfr6OYL7SceBIHNv0CFrt1OnkGo/tVMwcs8LH3Ae4a7UJlIceL
V54aRxSsZU7w4iX+PB79BWkEsQzwKrUuJVOeL4UDwWajp75OFaUqbS/slDDVXvK5
OIeuth3mA/adjdvgjPxhRQjA3l69rRWIJDrqBSHldmRsnX6cvXTDy8wSXZgy51lP
m4IVLHnCy9m4SaGGoAsfTZS0cC9FgjUIyTyrq9M67wOMpUxnuB0aRZgJE1DsI23E
qdvcSNVlO+39xM/KPWUEh6b83wMn88QeW+DCVGWACQq5N3YdPnAJa50617fGbY6I
gXIoRHXkDqe23PZ/jURYCv0sjVtjPoVC+bg=
=bJkn
-----END PGP PUBLIC KEY BLOCK-----
"""


def own_dir_for(settings):
    """Обновлённый yt-dlp — в tools\\yt-dlp рядом с настройками программы."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.path)), "tools", "yt-dlp")


def version_key(text):
    """'2026.10.21' → (2026, 10, 21); непонятное — None."""
    text = str(text or "")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", text):
        return None
    return tuple(int(part) for part in text.split("."))


def bundled_version():
    """Версия yt-dlp, встроенного в сборку или установленного через pip."""
    try:
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        return None


def copy_environment(path, base=None):
    """
    Окружение, в котором yt-dlp берётся из архива path. Из исходников это
    PYTHONPATH; собранный Python его не читает — для него своя переменная,
    её разбирает packaging/entry.py.
    """
    env = dict(base if base is not None else util.child_environment())
    env[util.YTDLP_COPY_ENV] = path
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [path, env.get("PYTHONPATH")]))
    return env


def read_version(path):
    """Запускает yt-dlp из архива path и спрашивает версию. Не запустился — None."""
    try:
        result = subprocess.run([*util.YTDLP, "--version"], capture_output=True, timeout=120,
                                stdin=subprocess.DEVNULL, env=copy_environment(path),
                                creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None
    words = result.stdout.decode("utf-8", "replace").split()
    return words[0] if result.returncode == 0 and words else None


class YtdlpLocator:
    """
    Какой yt-dlp запускать: обновлённую копию из tools\\yt-dlp, если она
    новее встроенного, иначе встроенный (или установленный через pip).
    После обновления — refresh().
    """

    def __init__(self, own_dir):
        self.own_dir = own_dir
        self._lock = threading.Lock()
        self._own = None
        self._read = False
        self._bundled = bundled_version()

    def _load(self):
        try:
            with open(os.path.join(self.own_dir, VERSION_FILE), encoding="utf-8") as source:
                data = json.load(source)
            version = str(data["version"])
            # только имя файла: путь из version.json за пределы папки не уводит
            path = os.path.join(self.own_dir, os.path.basename(str(data["file"])))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if version_key(version) is None or not os.path.isfile(path):
            return None
        return {"version": version, "path": path}

    def own(self):
        """Обновлённая копия: {"version", "path"} или None."""
        with self._lock:
            if not self._read:
                self._own = self._load()
                self._read = True
            return self._own

    def refresh(self):
        with self._lock:
            self._read = False
        return self.own()

    def active_copy(self):
        """Путь к обновлённой копии, если запускать надо её; иначе None."""
        own = self.own()
        if own is None:
            return None
        bundled = version_key(self._bundled)
        if bundled is not None and version_key(own["version"]) <= bundled:
            return None
        return own["path"]

    def status(self):
        own = self.own()
        active = self.active_copy()
        return {"version": own["version"] if active else self._bundled,
                "own": bool(active), "bundled": self._bundled}

    def environment(self, base=None):
        """Окружение для запуска yt-dlp: с обновлённой копией, если она есть."""
        env = dict(base if base is not None else util.child_environment())
        path = self.active_copy()
        return copy_environment(path, env) if path else env

    def cleanup(self):
        """Старые копии — прочь. Занятая идущей загрузкой удалится в следующий раз."""
        own = self.own()
        keep = os.path.basename(own["path"]) if own else None
        for path in glob.glob(os.path.join(self.own_dir, OWN_FILE.format(version="*"))):
            if os.path.basename(path) != keep:
                util.remove_quietly(path)


class Tampering(InstallError):
    """Признак подмены: подпись не сошлась или суммы разошлись. Дальше не идём."""


class YtdlpUpdater(Installer):
    """
    Обновление yt-dlp кнопкой из окна.

    Источники, по порядку:
      1. GitHub, выпуск yt-dlp/yt-dlp — сборка «yt-dlp»: zip с Python-кодом
         (3 МБ), внутри и скрипты yt-dlp-ejs своей версии. Сумма — из списка
         SHA2-256SUMS, подпись которого проверяется встроенным ключом
         разработчиков yt-dlp; сумма, которую посчитал сам GitHub, обязана
         с ней совпасть;
      2. PyPI — wheel той же версии с суммой от PyPI (так проверяет pip):
         если GitHub недоступен или медленный;
      3. снова GitHub, но через API — другой адрес входа к тому же файлу.
    Подпись не сошлась или суммы на GitHub разошлись — обновление
    останавливается совсем: это признак подмены, а не плохой связи.

    Скачанный yt-dlp должен запуститься и назвать ту же версию. Ставится
    рядом со старым: уже идущие загрузки доделываются прежней версией.
    """

    NAME = "yt-dlp"
    URLS = {
        "latest": "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
        "download": "https://github.com/yt-dlp/yt-dlp/releases/download/{version}/{name}",
        "pypi": "https://pypi.org/pypi/yt-dlp/json",
        "pypi_version": "https://pypi.org/pypi/yt-dlp/{version}/json",
    }
    ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                     "release-assets.githubusercontent.com", "pypi.org", "files.pythonhosted.org")
    ASSET = "yt-dlp"
    SUMS = "SHA2-256SUMS"
    MAX_SIZE = 64 * 1024 * 1024
    # файл в 3 МБ скачается и на медленной связи, но 64 КБ/с — уже повод
    # попробовать другой источник
    MIN_SPEED = 64 * 1024

    def __init__(self, locator, on_change=None, urls=None, allowed_hosts=None,
                 require_https=True, key=YTDLP_KEY, fingerprint=YTDLP_KEY_FINGERPRINT):
        """urls, allowed_hosts и ключ подменяются в тестах."""
        super().__init__(on_change, urls, allowed_hosts, require_https)
        self._locator = locator
        self._key = key
        self._fingerprint = fingerprint

    def _work_dir(self):
        return self._locator.own_dir

    def _plan(self):
        problems = []
        github = pypi = None
        try:
            github = self._github()
        except Tampering:
            raise
        except InstallError as error:
            problems.append("GitHub: %s" % error)
        try:
            pypi = self._pypi(github["version"] if github else None)
        except InstallError as error:
            problems.append("PyPI: %s" % error)
        if github is None and pypi is None:
            raise InstallError(tr("не удалось узнать, есть ли новая версия yt-dlp: {problems}",
                                  problems="; ".join(problems)))

        version = (github or pypi)["version"]
        current = self._locator.status()["version"]
        if version_key(current) is not None and version_key(version) <= version_key(current):
            logging.info(tr("yt-dlp {version}: обновлений нет, последний выпуск — {latest}",
                            version=current, latest=version))
            self._set(version=current)
            return None

        sources = []
        if github:
            sources.append(Source("GitHub", github["url"], github["sha256"], github["size"]))
        if pypi:
            sources.append(Source("PyPI", pypi["url"], pypi["sha256"], pypi["size"]))
        if github and github["api_url"]:
            sources.append(Source("GitHub API", github["api_url"], github["sha256"], github["size"],
                                  {"Accept": "application/octet-stream"}))
        return version, sources

    def _github(self):
        """Последний выпуск на GitHub и сумма сборки, подтверждённая подписью yt-dlp."""
        release = self._get_json(self._urls["latest"])
        version = str(release.get("tag_name", ""))
        if version_key(version) is None:
            raise InstallError(tr("GitHub назвал непонятную версию: {version}", version=version[:40]))
        assets = {item.get("name"): item for item in release.get("assets", [])
                  if isinstance(item, dict)}
        asset = assets.get(self.ASSET)
        if asset is None:
            raise InstallError(tr("в выпуске {version} нет файла {name}", version=version,
                                  name=self.ASSET))

        def download(name):
            return self._urls["download"].format(version=version, name=name)

        def fetch(name):
            """С github.com, а если он закрыт — тот же файл через api.github.com."""
            try:
                return self._get(download(name)).content
            except InstallError:
                api_url = (assets.get(name) or {}).get("url")
                if not api_url:
                    raise
                return self._get(api_url, headers={"Accept": "application/octet-stream"}).content

        sums = fetch(self.SUMS)
        signature = fetch(self.SUMS + ".sig")
        try:
            openpgp.verify(sums, signature, self._key, self._fingerprint)
        except openpgp.SignatureError as error:
            raise Tampering(tr("подпись списка сумм yt-dlp не сходится ({reason}) — возможна "
                               "подмена, обновление остановлено", reason=error))
        signed = None
        for line in sums.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == self.ASSET:
                signed = parts[0].lower()
        if not signed or not re.fullmatch(r"[0-9a-f]{64}", signed):
            raise InstallError(tr("в подписанном списке нет суммы для {name}", name=self.ASSET))
        digest = str(asset.get("digest") or "").lower()
        if digest.startswith("sha256:") and digest[len("sha256:"):] != signed:
            raise Tampering(tr("сумма yt-dlp у GitHub не совпадает с подписанной — возможна "
                               "подмена, обновление остановлено"))
        size = int(asset.get("size") or 0) or None
        if size is not None and size > self.MAX_SIZE:
            raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                  name=self.NAME, size=size))
        return {"version": version, "sha256": signed, "size": size, "url": download(self.ASSET),
                "api_url": asset.get("url")}

    def _pypi(self, version=None):
        """Wheel с PyPI: последней версии или ровно version."""
        url = (self._urls["pypi_version"].format(version=version) if version
               else self._urls["pypi"])
        data = self._get_json(url)
        info = data.get("info") or {}
        found = str(info.get("version", ""))
        if version_key(found) is None or (version and found != version) or info.get("yanked"):
            raise InstallError(tr("на PyPI нет подходящей версии yt-dlp"))
        wheel = next((item for item in data.get("urls", [])
                      if isinstance(item, dict) and item.get("packagetype") == "bdist_wheel"
                      and str(item.get("filename", "")).endswith("-py3-none-any.whl")
                      and not item.get("yanked")), None)
        if wheel is None:
            raise InstallError(tr("на PyPI нет сборки yt-dlp {version}", version=found))
        digest = str((wheel.get("digests") or {}).get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise InstallError(tr("контрольная сумма в непонятном виде"))
        size = int(wheel.get("size") or 0) or None
        if size is not None and size > self.MAX_SIZE:
            raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                  name=self.NAME, size=size))
        return {"version": found, "sha256": digest, "size": size, "url": str(wheel.get("url", ""))}

    def _install(self, path, version, source):
        """Сверить версию внутри архива, положить рядом со старым и запустить на пробу."""
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if not {"yt_dlp/__init__.py", "yt_dlp/version.py"} <= names:
                    raise InstallError(tr("в скачанном файле нет yt-dlp"))
                text = archive.read("yt_dlp/version.py").decode("utf-8", "replace")
        except zipfile.BadZipFile:
            raise InstallError(tr("скачанный yt-dlp повреждён"))
        match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", text, re.M)
        found = match.group(1) if match else "?"
        if found != version:
            raise InstallError(tr("в скачанном файле другая версия yt-dlp: {found} вместо {version}",
                                  found=found, version=version))

        final = os.path.join(self._locator.own_dir, OWN_FILE.format(version=version))
        try:
            os.replace(path, final)
        except OSError:
            raise InstallError(tr("yt-dlp {version} сейчас занят — повтори, когда загрузки "
                                  "закончатся", version=version))
        if read_version(final) != version:
            util.remove_quietly(final)
            raise InstallError(tr("скачанный yt-dlp не запустился или назвал другую версию"))

        record = os.path.join(self._locator.own_dir, VERSION_FILE)
        with open(record + ".tmp", "w", encoding="utf-8") as target:
            json.dump({"version": version, "file": os.path.basename(final), "sha256": source.sha256,
                       "source": source.label, "installed": time.time()}, target)
        os.replace(record + ".tmp", record)
        self._locator.refresh()
        self._locator.cleanup()
        return final
