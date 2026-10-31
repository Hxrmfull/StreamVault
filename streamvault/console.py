# -*- coding: utf-8 -*-
"""
Живая строка внизу консоли: что пишется и что качается, обновляется на
месте несколько раз в секунду — как счётчик записи в окне программы.

Строки журнала идут над ней как обычно: перед каждой строка стирается,
после — рисуется снова. В файл журнала она не попадает. Вывод не в
консоль (перенаправлен в файл или в другую программу) — строки нет
вовсе: там она осталась бы мусором из \\r и пробелов.

Рисуется только \\r и пробелами, без управляющих последовательностей:
так она работает в любой консоли Windows, и в старой тоже.
"""

import logging
import os
import shutil
import sys
import threading
import time
import unicodedata

from .i18n import tr

INTERVAL = 0.2             # как часто перерисовывается, секунды
# Windows Terminal рисует брайлевский спиннер; в старой консоли шрифт его не знает
FANCY_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PLAIN_SPINNER = "|/-\\"
BAR_WIDTH = 16


def format_size(value):
    if not value:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit == "Б" else "%.1f %s") % (value, tr(unit))
        value /= 1024.0


def format_duration(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return ("%d:%02d:%02d" % (hours, rest // 60, rest % 60) if hours
            else "%d:%02d" % (rest // 60, rest % 60))


def cells(text):
    """Сколько клеток консоли займёт текст: эмодзи и иероглифы — по две."""
    return sum(2 if unicodedata.east_asian_width(char) in "WF" or ord(char) >= 0x1F000 else 1
               for char in text if not unicodedata.combining(char))


def fit(text, width):
    """Обрезает по ширине консоли: перенос на вторую строку сломал бы \\r."""
    if cells(text) <= width:
        return text
    out, used = [], 0
    for char in text:
        size = 2 if unicodedata.east_asian_width(char) in "WF" or ord(char) >= 0x1F000 else 1
        if used + size > width - 1:
            break
        out.append(char)
        used += size
    return "".join(out) + "…"


def bar(percent, width=BAR_WIDTH):
    filled = max(0, min(width, round((percent or 0) / 100 * width)))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


class StatusLine:
    """
    render(tick, spin) -> текст строки; tick растёт на 1 при каждой
    перерисовке (мигающая точка записи), spin — кадр спиннера. start() и stop() — вокруг долгой
    команды; enabled — False, если вывод не в консоль.
    """

    def __init__(self, render, stream=None, interval=INTERVAL):
        self._render = render
        self._stream = stream or sys.stderr
        self._interval = interval
        self.enabled = bool(self._stream) and _is_console(self._stream)
        self.spinner = FANCY_SPINNER if os.environ.get("WT_SESSION") else PLAIN_SPINNER
        self._lock = threading.RLock()
        self._shown = 0            # сколько клеток занимает нарисованное
        self._tick = 0
        self._stop = threading.Event()
        self._thread = None
        self._wrapped = []

    def start(self):
        if not self.enabled:
            return self
        # строки журнала в ту же консоль — поверх живой строки
        for handler in logging.getLogger().handlers:
            if type(handler) is logging.StreamHandler and handler.stream is self._stream:
                original = handler.emit
                handler.emit = self._around(original)
                self._wrapped.append((handler, original))
        self._thread = threading.Thread(target=self._loop, name="status-line", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        with self._lock:
            for handler, original in self._wrapped:
                handler.emit = original
            self._wrapped = []
            self._clear()

    def _around(self, emit):
        def wrapped(record):
            with self._lock:
                self._clear()
                emit(record)
                self._draw()
        return wrapped

    def _loop(self):
        while not self._stop.wait(self._interval):
            self._tick += 1
            with self._lock:
                self._draw()

    def _clear(self):
        if self._shown:
            self._write("\r" + " " * self._shown + "\r")
            self._shown = 0

    def _draw(self):
        try:
            text = self._render(self._tick, self.spinner[self._tick % len(self.spinner)]) or ""
        except Exception:
            logging.debug("живая строка не нарисовалась", exc_info=True)
            return
        width = max(20, shutil.get_terminal_size((100, 20)).columns - 1)
        text = fit(text, width)
        size = cells(text)
        # хвост прошлого, более длинного текста затирается пробелами
        self._write("\r" + text + " " * max(0, self._shown - size))
        self._shown = max(size, self._shown)

    def _write(self, data):
        try:
            self._stream.write(data)
            self._stream.flush()
        except (OSError, ValueError):
            self.enabled = False


def _is_console(stream):
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


# ------------------------------------------------------------ что рисовать

def recording_status(service):
    """
    record: пишущиеся каналы — время, размер, чат, с мигающей точкой;
    нечего писать — спиннер и сколько каналов в эфире. Плюс обработка.
    """
    def render(tick, spin):
        snapshot = service.snapshot()
        channels = snapshot["channels"]
        now = time.time()
        recording = [channel for channel in channels
                     if channel.get("state") == "recording" and channel.get("recording")]
        if recording:
            dot = "●" if (tick * INTERVAL) % 1.0 < 0.6 else " "
            parts = []
            for channel in recording:
                rec = channel["recording"]
                part = "%s %s · %s" % (channel["id"], format_duration(now - rec["started"]),
                                       format_size(rec.get("bytes")))
                if rec.get("chat_messages") is not None:
                    part += " · " + tr("чат {count}", count=rec["chat_messages"])
                parts.append(part)
            text = "%s REC  %s" % (dot, "  |  ".join(parts))
        else:
            live = sum(1 for channel in channels if channel.get("state") in ("live", "unavailable"))
            text = "%s %s" % (spin, tr("наблюдаю за каналами: {count}, в эфире: {live}",
                                                     count=len(channels), live=live))
        processing = snapshot.get("processing") or {}
        if processing.get("current"):
            text += "  ·  " + tr("обработка: {name}", name=os.path.basename(processing["current"]["file"]))
        elif processing.get("pending"):
            text += "  ·  " + tr("ждут обработки: {count}", count=processing["pending"])
        return text
    return render


def download_status(manager, job_ids, describe):
    """download: полоса первой идущей загрузки, её подробности и сколько ещё идёт."""
    def render(tick, spin):
        jobs = [manager.job(job_id) for job_id in job_ids]
        active = [job for job in jobs if job["state"] in ("downloading", "processing")]
        if not active:
            waiting = sum(1 for job in jobs if job["state"] == "queued")
            return "%s %s" % (spin, tr("в очереди: {count}", count=waiting)) if waiting else ""
        job = active[0]
        percent = (job.get("progress") or {}).get("percent")
        text = "%s %s %s  %s" % (spin, bar(percent), describe(job), job["title"] or job["url"])
        if len(active) > 1:
            text += "  (+%d)" % (len(active) - 1)
        return text
    return render
