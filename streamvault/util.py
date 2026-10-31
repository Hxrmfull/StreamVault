# -*- coding: utf-8 -*-
"""Файлы и внешние процессы: общее для записи эфиров и скачивания."""

import itertools
import logging
import os
import re
import subprocess
import sys
import threading
import time

from .i18n import tr


def console_python():
    """
    Python для запуска yt-dlp. У окна программы sys.executable — pythonw.exe,
    у которого нет стандартного вывода, а рядом всегда лежит python.exe.
    """
    folder, name = os.path.split(sys.executable)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return sys.executable


# Собранная программа (exe PyInstaller), а не запуск из исходников.
FROZEN = bool(getattr(sys, "frozen", False))

# Скрытые ключи консольного exe: им окно запускает streamlink и yt-dlp,
# встроенные в сборку. Отдельный процесс, как и из исходников: его можно
# прервать, не задев окно, а вывод читается как обычно.
RUN_STREAMLINK = "--run-streamlink"
RUN_YTDLP = "--run-ytdlp"
# а этим новая версия ставит себя на место старой (update.py)
APPLY_UPDATE = "--apply-update"
CONSOLE_EXE = "StreamVaultCLI.exe"
# Обновлённый yt-dlp (архив из toolsyt-dlp): путь к нему передаётся
# помощнику этой переменной — собранный Python не читает PYTHONPATH.
YTDLP_COPY_ENV = "STREAMVAULT_YTDLP"


def helper_exe():
    """Консольный exe рядом с окном программы."""
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), CONSOLE_EXE)


# Как запускать внешние программы. Списки, а не строки: тесты подставляют
# свои заглушки. yt-dlp берётся из того же Python, что и программа, —
# через yt_dlp.main(), а не "-m yt_dlp": у обновлённой копии (сборка
# «yt-dlp» с GitHub) в пакете нет __main__.py.
# Свои расширения yt-dlp (клипы VK Video Live): папка рядом с модулями
# программы, в сборке — среди её данных.
YTDLP_PLUGINS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ytdlp_plugins")
if FROZEN:
    STREAMLINK = [helper_exe(), RUN_STREAMLINK]
    YTDLP = [helper_exe(), RUN_YTDLP, "--plugin-dirs", YTDLP_PLUGINS]
else:
    STREAMLINK = ["streamlink"]
    YTDLP = [console_python(), "-c", "import yt_dlp; yt_dlp.main()", "--plugin-dirs", YTDLP_PLUGINS]

_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")

MAX_PATH_LEN = 240

# Расширение, под которым ffmpeg пишет результат, пока не закончит.
PARTIAL_SUFFIX = ".part"

# Папки внутри папки записей: записи, которые пишутся или ждут конвертации,
# и готовые. До версии 0.1.0 включительно они назывались recorded и processed,
# и в processed (по смыслу «готовые») видели промежуточную папку.
IN_PROGRESS_DIR = "in-progress"
FINISHED_DIR = "finished"
OLD_RECORDING_DIRS = {"recorded": IN_PROGRESS_DIR, "processed": FINISHED_DIR}


def recording_channel(root_path, path):
    """
    Площадка и папка канала записи: <папка записей>/in-progress|finished/
    <площадка>/<канал>/…; не оттуда — (None, None).
    """
    try:
        relative = os.path.relpath(os.path.abspath(path), os.path.abspath(root_path))
    except ValueError:               # другой диск
        return None, None
    parts = relative.split(os.sep)
    if len(parts) < 4 or parts[0] not in (IN_PROGRESS_DIR, FINISHED_DIR):
        return None, None
    return parts[1], parts[2]


def sanitize_filename(name):
    """Убирает только то, что реально запрещено. Кириллица и пунктуация остаются."""
    cleaned = _INVALID_FILENAME_CHARS.sub("", name)
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    return cleaned.rstrip(". ")


def unique_path(path):
    """Подбирает свободное имя. Иначе ffmpeg спросит подтверждение и повиснет."""
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(path)
    for index in itertools.count(2):
        candidate = "%s (%d)%s" % (stem, index, extension)
        if not os.path.exists(candidate):
            return candidate


def remove_quietly(path):
    """Удаляет файл, если он есть и не занят. Неудача — не повод прерывать работу."""
    try:
        os.remove(path)
    except OSError:
        pass


def build_recording_path(directory, label, title, moment, part, extension=".ts"):
    """Собирает имя записи, обрезая заголовок стрима под лимит длины пути."""
    stamp = moment.strftime("%Y-%m-%d %Hh%Mm%Ss")
    prefix = sanitize_filename("%s - %s" % (label, stamp)) or "stream"
    # первая часть — без пометки; part00 — начало эфира, взятое из архива площадки
    part_tag = " - part%02d" % part if part != 1 else ""
    title = sanitize_filename(title or "")

    budget = (MAX_PATH_LEN - len(directory) - len(os.sep) - len(prefix)
              - len(part_tag) - len(extension) - len(" - "))
    if title and budget > 0:
        title = title[:budget].rstrip(". ")
    else:
        title = ""

    if title:
        name = "%s - %s%s%s" % (prefix, title, part_tag, extension)
    else:
        name = "%s%s%s" % (prefix, part_tag, extension)
    return os.path.join(directory, name)


def has_console():
    """Есть ли у программы консоль. У окна, запущенного через pythonw, её нет."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except (ImportError, AttributeError, OSError):
        return True


def subprocess_flags():
    """
    Флаги запуска streamlink, ffmpeg и taskkill.

    Без консоли каждый такой запуск открывал бы своё чёрное окно, поэтому
    им нужен CREATE_NO_WINDOW. С консолью флаг вреден: Ctrl+C должен
    доходить до streamlink, чтобы тот сам закрыл файл записи.
    """
    if os.name == "nt" and not has_console():
        return subprocess.CREATE_NO_WINDOW
    return 0


def child_environment():
    """
    Окружение для streamlink и yt-dlp. Их вывод идёт в файл или канал,
    а не в консоль, и без явной кодировки Python писал бы его в cp1251.
    """
    return dict(os.environ, PYTHONIOENCODING="utf-8")


def kill_process_tree(process):
    """Гасит процесс вместе с потомками: streamlink для Kick поднимает браузер."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess_flags())
    else:
        process.kill()


_instance_lock = None


def _kernel32():
    """kernel32 с прототипами: без них дескриптор на 64-битной Windows обрезался бы."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                                      wintypes.LPCWSTR)
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    return kernel32


def single_instance(name="StreamVault"):
    """
    Не даёт запустить программу дважды: две копии писали бы одни и те же
    каналы в разные файлы и затирали бы друг другу очередь загрузок.
    True — эта копия первая. Блокировка держится до выхода из программы.
    """
    global _instance_lock
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = _kernel32()
    handle = kernel32.CreateMutexW(None, False, "Local\\" + name)
    if not handle:
        return True   # не удалось проверить — не мешаем работать
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _instance_lock = handle
    return True


class ShowSignal:
    """
    Просьба к уже запущенной копии показать окно. Повторный запуск программы
    не ругается, что она уже запущена, а будит первую копию: та
    разворачивается из значка в трее.
    """

    def __init__(self, name="StreamVault-show"):
        self._name = "Local\\" + name
        self._handle = None

    def listen(self, callback):
        if os.name != "nt":
            return
        kernel32 = _kernel32()
        # автосброс: одно срабатывание — один показ окна
        handle = kernel32.CreateEventW(None, False, False, self._name)
        if not handle:
            return
        self._handle = handle

        def wait():
            while kernel32.WaitForSingleObject(handle, 0xFFFFFFFF) == 0:
                try:
                    callback()
                except Exception:
                    logging.exception(tr("не удалось показать окно"))

        threading.Thread(target=wait, name="show-signal", daemon=True).start()

    def fire(self):
        """Будит первую копию. True — она есть и услышала."""
        if os.name != "nt":
            return False
        kernel32 = _kernel32()
        handle = kernel32.OpenEventW(0x0002, False, self._name)   # EVENT_MODIFY_STATE
        if not handle:
            return False
        kernel32.SetEvent(handle)
        kernel32.CloseHandle(handle)
        return True


def send_to_recycle_bin(path):
    """В корзину, а не насовсем: удалённое из библиотеки можно вернуть."""
    if os.name != "nt":
        raise OSError(tr("корзина поддерживается только в Windows"))
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    fo_delete = 3
    # молча, без вопросов и окон ошибок, но с возможностью вернуть
    flags = 0x0004 | 0x0010 | 0x0040 | 0x0400
    # список файлов заканчивается двумя нулями: один дописываем, второй — ctypes
    operation = SHFILEOPSTRUCTW(None, fo_delete, os.path.abspath(path) + "\0", None,
                                flags, False, None, None)
    result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
    if result != 0 or operation.fAnyOperationsAborted or os.path.exists(path):
        raise OSError(tr("не удалось отправить в корзину (код {code}): {path}",
                         code=result, path=path))


def notify(message, title="StreamVault"):
    """Сообщение человеку: в консоль, а без неё — окошком Windows."""
    if has_console() or os.name != "nt":
        print(message, file=sys.stderr)
        return
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, title, 0x40)


def join_interruptibly(thread, timeout):
    """
    Thread.join на Windows не прерывается по Ctrl+C, пока не выйдет таймаут:
    повторный Ctrl+C во время доработки очереди ждал бы до пятнадцати минут.
    Короткие ожидания в цикле дают сигналу проскочить между ними.
    """
    deadline = time.monotonic() + timeout
    while thread.is_alive():
        left = deadline - time.monotonic()
        if left <= 0:
            return
        thread.join(min(left, 0.5))
