# -*- coding: utf-8 -*-
"""
Обновление самой программы с GitHub — как в DTTConvert.

При запуске (если не отключено в настройках) и по кнопке программа
спрашивает у GitHub последний выпуск. Нашёлся новее — окно показывает
ненавязчивую кнопку; по щелчку скачивается архив выпуска.

Как проверяется, что ничего не подменили:
  1. архив обязан совпасть по размеру и сумме sha256 с тем, что сообщает
     GitHub (он считает сумму сам, когда файл выкладывают);
  2. только https и только адреса GitHub;
  3. внутри — только файлы программы, по своим местам.

Как ставится. Запущенную программу не заменить: её библиотеки открыты.
Новая версия распаковывается рядом, в update\\, программа закрывается,
а StreamVaultCLI.exe новой версии ждёт её выхода, меняет файлы программы
(StreamVault.exe, StreamVaultCLI.exe, _internal) и запускает обновлённую.
Настройки, загрузки, журналы и скачанные кнопками инструменты остаются.
Не вышло — старые файлы возвращаются на место.

Установленную копию (рядом с ней unins000.exe) обновляет установщик новой
версии — как в DTTConvert: он знает, куда и для кого программа поставлена,
и сам попросит права администратора, если её ставили для всех. Программа
скачивает его во временную папку, сверяет так же и запускает с /SILENT.
"""

import ctypes
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from urllib.parse import urlsplit

from . import __version__, util
from .i18n import tr
from .installer import HttpError, InstallError, Installer, Source

REPO = "Hxrmfull/StreamVault"
LATEST_URL = "https://api.github.com/repos/%s/releases/latest" % REPO
RELEASES_PAGE = "https://github.com/%s/releases" % REPO
ASSET = "StreamVault-{version}-windows.zip"
SETUP_ASSET = "StreamVault-{version}-setup.exe"
# /SILENT — без вопросов мастера, только полоса прогресса; /RELAUNCH —
# запустить программу после установки (StreamVault.iss); /CLOSEAPPLICATIONS —
# закрыть то, что ещё держит файлы, не спрашивая: программа и так выходит.
SETUP_ARGUMENTS = "/SILENT /SP- /CLOSEAPPLICATIONS /RELAUNCH"
UNINSTALLER = "unins000.exe"
# Проверка руками: программа спрашивает о выпуске свой сервер вместо GitHub.
URL_ENV = "STREAMVAULT_UPDATE_URL"

# файлы самой программы; всё остальное в её папке — личное
PROGRAM = ("StreamVault.exe", "StreamVaultCLI.exe", "_internal")
UPDATE_DIR = "update"
RESULT_FILE = "result.json"


def parse_version(text):
    """«v1.2.3», «1.2» или «1.2.3-beta» → (1, 2, 3). Не версия — None."""
    match = re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
    return tuple(int(part or 0) for part in match.groups()) if match else None


def is_newer(remote, local=__version__):
    remote, local = parse_version(remote), parse_version(local)
    return remote is not None and local is not None and remote > local


def program_dir():
    """Папка собранной программы; из исходников — None: их обновляют через git."""
    return os.path.dirname(os.path.abspath(sys.executable)) if util.FROZEN else None


def installed(folder):
    """Поставлена ли программа установщиком: рядом лежит его деинсталлятор."""
    return bool(folder) and os.path.isfile(os.path.join(folder, UNINSTALLER))


def setup_dir():
    """Куда установленная копия качает установщик: в её папку писать может быть нельзя."""
    return os.path.join(tempfile.gettempdir(), "StreamVault-update")


# ------------------------------------------------------------ замена файлов программы

def _remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def program_in_use(folder):
    """Запущена ли программа из folder: папку с открытыми библиотеками Windows не переименует."""
    internal = os.path.join(folder, "_internal")
    if not os.path.isdir(internal):
        return False
    probe = internal + ".probe"
    try:
        os.rename(internal, probe)
    except OSError:
        return True
    os.rename(probe, internal)
    return False


def install_program(staged, target):
    """
    Файлы программы из staged — в target поверх старых. Сначала всё
    копируется рядом с суффиксом .new, потом быстро переименовывается;
    не вышло — старое возвращается на место (OSError). Личное не трогается.
    """
    os.makedirs(target, exist_ok=True)
    for name in PROGRAM:
        for suffix in (".new", ".old"):    # хвосты прерванного обновления
            _remove(os.path.join(target, name + suffix))
    for name in PROGRAM:
        source, fresh = os.path.join(staged, name), os.path.join(target, name + ".new")
        if os.path.isdir(source):
            shutil.copytree(source, fresh)
        else:
            shutil.copy2(source, fresh)
    swapped = []
    try:
        for name in PROGRAM:
            current = os.path.join(target, name)
            if os.path.exists(current):
                os.rename(current, current + ".old")
            swapped.append(name)
            os.rename(current + ".new", current)
    except OSError:
        for name in reversed(swapped):
            current = os.path.join(target, name)
            if os.path.exists(current + ".old"):
                _remove(current)
                os.rename(current + ".old", current)
        for name in PROGRAM:
            _remove(os.path.join(target, name + ".new"))
        raise
    for name in PROGRAM:
        _remove(os.path.join(target, name + ".old"))


# ------------------------------------------------------------ скачивание выпуска

class AppUpdater(Installer):
    """
    Новая версия программы: check() — есть ли она, start() — скачать
    и подготовить, apply() — закрыться и поставить.

    Состояния сверх общих: none — выпусков на GitHub пока нет, available —
    есть новее (version, notes, page). done — новая версия скачана,
    проверена и ждёт перезапуска.
    """

    NAME = "StreamVault"
    ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com",
                     "release-assets.githubusercontent.com")
    MANUAL_PAGE = RELEASES_PAGE
    MAX_SIZE = 600 * 1024 * 1024

    def __init__(self, folder, on_change=None, latest_url=LATEST_URL, allowed_hosts=None,
                 require_https=True, current=__version__):
        """folder — папка программы (None — запуск из исходников, ставить некуда)."""
        super().__init__(on_change, {"latest": latest_url}, allowed_hosts, require_https)
        self._folder = folder
        self._current = current
        self._check_lock = threading.Lock()
        self._state.update(current=current, notes="", page=RELEASES_PAGE,
                           can_install=self.can_install())

    @classmethod
    def for_this_program(cls, on_change=None):
        """Обычный: GitHub. С STREAMVAULT_UPDATE_URL — свой сервер (для проверки руками)."""
        url = os.environ.get(URL_ENV)
        if not url:
            return cls(program_dir(), on_change)
        host = urlsplit(url).hostname
        return cls(program_dir(), on_change, latest_url=url,
                   allowed_hosts=cls.ALLOWED_HOSTS + (host,),
                   require_https=host not in ("127.0.0.1", "localhost"))

    def can_install(self):
        """
        Ставить можно собранную программу из папки, куда разрешено писать,
        и любую установленную — права попросит её установщик.
        """
        if not self._folder:
            return False
        if installed(self._folder):
            return True
        try:
            probe = os.path.join(self._folder, ".update-probe")
            with open(probe, "wb"):
                pass
            os.remove(probe)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------ проверка

    def check(self):
        """Спрашивает GitHub. Возвращает снимок состояния."""
        if self.busy() or not self._check_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            self._set(state="checking", error="")
            try:
                release = self._latest()
            except HttpError as error:
                if error.status != 404:
                    return self._check_failed(error)
                # у закрытого репозитория для чужих глаз выпусков тоже «нет»
                logging.info(tr("обновления: выпусков на GitHub пока нет"))
                self._set(state="none")
                return self.snapshot()
            except InstallError as error:
                return self._check_failed(error)
            if not is_newer(release["version"], self._current):
                self._set(state="current", version=release["version"])
            else:
                logging.info(tr("доступна новая версия StreamVault: {version}",
                                version=release["version"]))
                self._set(state="available", version=release["version"],
                          notes=release["notes"], page=release["page"])
            return self.snapshot()
        finally:
            self._check_lock.release()

    def _check_failed(self, error):
        # нет сети или GitHub недоступен — не беда, проверим в другой раз
        logging.info(tr("обновления: проверить не удалось — {error}", error=error))
        self._set(state="failed", error=str(error))
        return self.snapshot()

    def check_in_background(self, delay=0):
        def run():
            time.sleep(delay)
            try:
                self.check()
            except Exception:
                logging.exception(tr("проверка обновлений упала"))
        threading.Thread(target=run, name="update-check", daemon=True).start()

    def _latest(self):
        """Последний выпуск: версия, описание, страница и файлы архива."""
        data = self._get_json(self._urls["latest"])
        version = str(data.get("tag_name") or "").lstrip("vV")
        page = str(data.get("html_url") or "")
        if parse_version(version) is None or data.get("draft") or data.get("prerelease"):
            raise InstallError(tr("GitHub назвал непонятную версию: {version}", version=version[:40]))
        assets = {item.get("name"): item for item in data.get("assets") or [] if isinstance(item, dict)}
        return {"version": version, "notes": str(data.get("body") or "")[:4000],
                "page": page if page.startswith("https://github.com/") else RELEASES_PAGE,
                "assets": assets}

    # ------------------------------------------------------------ скачивание

    def _work_dir(self):
        return setup_dir() if installed(self._folder) else os.path.join(self._folder, UPDATE_DIR)

    def _plan(self):
        if not self.can_install():
            raise InstallError(tr("эту копию программы не обновить отсюда — скачай выпуск "
                                  "со страницы GitHub"))
        release = self._latest()
        version = release["version"]
        if not is_newer(version, self._current):
            self._set(version=version)
            return None
        name = (SETUP_ASSET if installed(self._folder) else ASSET).format(version=version)
        archive = release["assets"].get(name)
        if not archive:
            raise InstallError(tr("в выпуске {version} нет файла {name}", version=version, name=name))
        digest = str(archive.get("digest") or "").lower()
        if not digest.startswith("sha256:"):
            raise InstallError(tr("GitHub не сообщил контрольную сумму архива"))
        size = int(archive.get("size") or 0) or None
        if size is not None and size > self.MAX_SIZE:
            raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                  name=self.NAME, size=size))
        expected = digest[len("sha256:"):]
        sources = [Source("GitHub", str(archive.get("browser_download_url") or ""), expected, size)]
        if archive.get("url"):
            sources.append(Source("GitHub API", archive["url"], expected, size,
                                  {"Accept": "application/octet-stream"}))
        self._set(notes=release["notes"], page=release["page"])
        return version, sources

    def _install(self, path, version, source):
        """Распаковка в update\\StreamVault-<версия> — только файлы программы. Сумма уже сверена."""
        if installed(self._folder):
            # установщик ставится как есть: недокачанное убирается, поэтому — под своим именем
            setup = os.path.join(self._work_dir(), SETUP_ASSET.format(version=version))
            _remove(setup)
            os.replace(path, setup)
            self._set(staged=setup)
            return setup
        staging = os.path.join(self._work_dir(), "StreamVault-" + version)
        _remove(staging)
        try:
            with zipfile.ZipFile(path) as archive:
                members = []
                for info in archive.infolist():
                    parts = info.filename.replace("\\", "/").split("/")
                    if parts[0] != "StreamVault" or ".." in parts or info.filename.startswith(("/", "\\")) \
                            or ":" in info.filename:
                        raise InstallError(tr("в архиве выпуска лишнее: {name}", name=info.filename))
                    if len(parts) > 1 and parts[1] and parts[1] not in PROGRAM:
                        raise InstallError(tr("в архиве выпуска лишнее: {name}", name=info.filename))
                    members.append(info)
                names = {info.filename for info in members}
                for required in ("StreamVault/StreamVault.exe", "StreamVault/StreamVaultCLI.exe"):
                    if required not in names:
                        raise InstallError(tr("в архиве нет {name}", name=required))
                for info in members:
                    if info.is_dir():
                        continue
                    target = os.path.join(staging, *info.filename.split("/"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with archive.open(info) as packed, open(target, "wb") as unpacked:
                        shutil.copyfileobj(packed, unpacked, self.CHUNK)
        except zipfile.BadZipFile:
            _remove(staging)
            raise InstallError(tr("архив выпуска повреждён"))
        except BaseException:
            _remove(staging)
            raise
        self._set(staged=os.path.join(staging, "StreamVault"))
        return staging

    def _log_done(self, version, path):
        logging.info(tr("обновление {version} скачано и проверено — поставится при перезапуске",
                        version=version))

    # ------------------------------------------------------------ установка

    def apply(self):
        """
        Запускает установщик из новой версии и возвращает True — после этого
        программа должна закрыться. Новая версия ещё не скачана — False.
        """
        snapshot = self.snapshot()
        staged = snapshot.get("staged")
        if snapshot["state"] != "done" or not staged or not os.path.exists(staged):
            return False
        if os.path.isfile(staged):
            # установщик: через оболочку — так Windows сама спросит права администратора,
            # если программу ставили для всех
            os.startfile(staged, "open", SETUP_ARGUMENTS)
            logging.info(tr("обновление до {version}: программа закроется и запустится заново",
                            version=snapshot["version"]))
            return True
        helper = os.path.join(staged, util.CONSOLE_EXE)
        subprocess.Popen([helper, util.APPLY_UPDATE, self._folder, str(os.getpid())], cwd=staged,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True,
                         creationflags=util.subprocess_flags())
        logging.info(tr("обновление до {version}: программа закроется и запустится заново",
                        version=snapshot["version"]))
        return True


# ------------------------------------------------------------ установщик (новая версия)

def wait_for_exit(pid, timeout):
    """Ждёт, пока процесс pid закончится. Закончился (или его и не было) — True."""
    if os.name != "nt":
        return True
    synchronize = 0x00100000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


def apply_update(args, attempts=20, pause=1.5, relaunch=True):
    """
    StreamVaultCLI.exe --apply-update <папка программы> <pid>. Запускается
    из update\\StreamVault-<версия>\\StreamVault — то есть уже новая версия.
    Ждёт выхода старой, меняет файлы программы и запускает обновлённую;
    итог — в update\\result.json, его прочтёт запущенная программа.
    """
    target, pid = args[0], args[1]
    staged = os.path.dirname(os.path.abspath(sys.executable))
    result_path = os.path.join(target, UPDATE_DIR, RESULT_FILE)
    result = {"version": __version__, "ok": False, "error": ""}
    try:
        wait_for_exit(pid, 300)
        # библиотеки старой версии отпускаются не мгновенно, а антивирус любит
        # заглянуть в новые файлы — поэтому несколько попыток
        for attempt in range(attempts):
            try:
                install_program(staged, target)
                result["ok"] = True
                break
            except OSError as error:
                result["error"] = str(error)
                time.sleep(pause)
    except Exception as error:
        result["error"] = str(error)
    try:
        with open(result_path, "w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False)
    except OSError:
        pass
    if relaunch:
        # обновилось или нет — программа запускается снова: новая или старая
        subprocess.Popen([os.path.join(target, "StreamVault.exe")], cwd=target, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    return 0 if result["ok"] else 1


def finish_previous(folder):
    """
    При запуске: чем кончилось прошлое обновление (None — его не было)
    и уборка — скачанный архив и распакованная версия больше не нужны.
    """
    if not folder:
        return None
    update_dir = os.path.join(folder, UPDATE_DIR)
    result = None
    result_path = os.path.join(update_dir, RESULT_FILE)
    try:
        with open(result_path, encoding="utf-8") as source:
            result = json.load(source)
    except (OSError, ValueError):
        pass
    if result is not None:
        if result.get("ok"):
            logging.info(tr("StreamVault обновлён до {version}", version=result.get("version")))
        else:
            logging.error(tr("обновление до {version} не удалось: {error}",
                             version=result.get("version"), error=result.get("error")))

    def clean():
        # установщик мог ещё не выйти из update\ — несколько попыток
        for _ in range(20):
            for name in PROGRAM:
                _remove(os.path.join(folder, name + ".old"))
                _remove(os.path.join(folder, name + ".new"))
            shutil.rmtree(update_dir, ignore_errors=True)
            if not os.path.exists(update_dir):
                return
            time.sleep(1.5)

    if os.path.isdir(update_dir):
        threading.Thread(target=clean, name="update-cleanup", daemon=True).start()
    if installed(folder) and os.path.isdir(setup_dir()):
        # установщик прошлого обновления больше не нужен
        threading.Thread(target=shutil.rmtree, args=(setup_dir(), True), name="setup-cleanup",
                         daemon=True).start()
    return result

