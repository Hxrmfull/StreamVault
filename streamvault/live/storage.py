# -*- coding: utf-8 -*-
"""
Сохранность записей на диске: место на диске под записи и срок хранения.

DiskGuard следит, хватает ли места: меньше порога — предупреждение в журнал,
окно и трей; меньше пятой части порога — запись останавливается. Лучше
остановить запись, чем получить битый файл и забитый диск.

Retention убирает в корзину записи старше N дней — вместе с чатом, как
кнопка «Удалить» в библиотеке. Срок общий или свой у канала.
"""

import logging
import os
import shutil
import threading
import time

from .. import util
from ..i18n import tr
from ..platforms import channel_label

GB = 1024 ** 3

OK, LOW, CRITICAL = "ok", "low", "critical"
# Какая доля порога считается критической: запись останавливается.
CRITICAL_SHARE = 5
# Как часто проверять место во время записи, секунды.
DISK_CHECK_INTERVAL = 60


def human_size(size):
    """Размер для журнала: 4.7 ГБ, 820 МБ."""
    if size >= GB:
        return tr("{value} ГБ", value=round(size / GB, 1))
    return tr("{value} МБ", value=round(size / 1024 ** 2))


class DiskGuard:
    """
    Место на диске для записей. Общий у всех каналов: о смене уровня
    говорится один раз, а не каждым каналом. floor() — порог в байтах
    (0 — не следить). События в шину — "disk" с уровнем ok, low или critical.
    """

    def __init__(self, floor, bus=None, usage=shutil.disk_usage):
        self._floor = floor
        self._bus = bus
        self._usage = usage
        self._lock = threading.Lock()
        self._levels = {}      # диск -> последний уровень
        self._state = {"level": OK, "free": None, "floor": 0, "path": ""}

    def check(self, path):
        """Уровень места для папки path: ok, low или critical, и свободно байт."""
        floor = max(0, int(self._floor() or 0))
        try:
            free = self._usage(path).free
        except OSError:
            # папки ещё нет или диск отключён: решает сама запись
            return OK, None
        if not floor:
            level = OK
        elif free < floor / CRITICAL_SHARE:
            level = CRITICAL
        elif free < floor:
            level = LOW
        else:
            level = OK
        drive = os.path.splitdrive(os.path.abspath(path))[0] or os.path.abspath(path)
        with self._lock:
            before = self._levels.get(drive, OK)
            self._levels[drive] = level
            changed = before != level
            if changed or level != OK:
                self._state = {"level": level, "free": free, "floor": floor, "path": path}
        if changed:
            self._announce(level, free, floor, path)
        return level, free

    def _announce(self, level, free, floor, path):
        if level == CRITICAL:
            logging.error(tr("на диске с записями осталось {free} — запись останавливается, "
                             "пока место не освободится", free=human_size(free)))
        elif level == LOW:
            logging.warning(tr("на диске с записями осталось {free} (порог {floor}) — "
                               "освободи место, иначе запись остановится",
                               free=human_size(free), floor=human_size(floor)))
        else:
            logging.info(tr("места на диске с записями снова хватает: {free}", free=human_size(free)))
        if self._bus is not None:
            self._bus.emit("disk", level=level, free=free, floor=floor, path=path)

    def snapshot(self):
        with self._lock:
            return dict(self._state)


def channel_days(settings, platform, label):
    """Сколько дней хранить записи канала: своё у канала или общее, 0 — всегда."""
    for entry in settings["channels"]:
        if entry["platform"] == platform and channel_label(entry["channel"]).lower() == label.lower():
            if entry.get("keep_days") is not None:
                return max(0, int(entry["keep_days"]))
            break
    return max(0, int(settings["keep_days"] or 0))


def recording_moment(path):
    """Когда сделана запись: по имени файла (его строит программа), иначе по времени файла."""
    from ..library import parse_recording_name
    stem = os.path.splitext(os.path.basename(path))[0]
    moment = parse_recording_name(stem).get("timestamp")
    if moment:
        return moment
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def expired_recordings(settings, now=None):
    """Записи эфиров старше срока своего канала: [(путь, дней хранить)]."""
    from ..library import walk_media
    now = now if now is not None else time.time()
    finished = os.path.join(settings["root_path"], util.FINISHED_DIR)
    found = []
    for platform, label, path in walk_media(finished):
        days = channel_days(settings, platform, label)
        if not days:
            continue
        moment = recording_moment(path)
        if moment is not None and now - moment > days * 86400:
            found.append((path, days))
    return found


class Retention(threading.Thread):
    """Раз в час убирает в корзину записи старше срока. Срок 0 — хранить всегда."""

    INTERVAL = 3600

    def __init__(self, settings, delete=None, stop_event=None):
        super().__init__(name="retention", daemon=True)
        self._settings = settings
        self._remove = delete
        # не _stop: так называется внутренний метод threading.Thread
        self._halt = stop_event or threading.Event()

    def sweep(self, now=None):
        """Один проход. Возвращает удалённые пути."""
        from ..library import delete_with_chat
        delete = self._remove or delete_with_chat
        removed = []
        for path, days in expired_recordings(self._settings, now):
            try:
                delete(path)
            except OSError as error:
                logging.warning(tr("старая запись не удалена: {path} ({error})", path=path, error=error))
                continue
            removed.append(path)
            logging.info(tr("запись старше {days} дн. убрана в корзину: {path}", days=days, path=path))
        return removed

    def run(self):
        while not self._halt.is_set():
            try:
                self.sweep()
            except Exception:
                logging.exception(tr("уборка старых записей не удалась"))
            self._halt.wait(self.INTERVAL)

    def stop(self):
        self._halt.set()
