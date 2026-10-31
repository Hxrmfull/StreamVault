# -*- coding: utf-8 -*-
"""Логи: файл с ротацией, консоль и пересылка строк в окно программы."""

import collections
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback

from .i18n import tr


def make_console_output_safe():
    """
    Заголовки стримов часто содержат эмодзи, а консоль Windows — это cp1251.
    Без этого logging валился бы на UnicodeEncodeError. Кодировку не меняем,
    только разрешаем заменять непечатаемое: в файл всё равно пишется utf-8.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """
    На Windows ротация не может переименовать файл, пока его держит другой
    запущенный экземпляр программы. Обычный RotatingFileHandler на этом сыпал
    трассировками "--- Logging error ---". Здесь неудачная ротация откладывается
    на пять минут, а запись продолжается в тот же файл.
    """

    RETRY_AFTER = 300

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._blocked_until = 0.0

    def shouldRollover(self, record):
        if time.time() < self._blocked_until:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self._blocked_until = time.time() + self.RETRY_AFTER
            if self.stream is None:
                self.stream = self._open()


class BusLogHandler(logging.Handler):
    """
    Пересылает строки лога в шину событий: окно программы показывает журнал,
    не читая файл. Повторный вход отсекается, иначе ошибка подписчика,
    записанная в лог, порождала бы новое событие без конца.
    """

    def __init__(self, bus, level=logging.INFO):
        super().__init__(level)
        self._bus = bus
        self._guard = threading.local()

    def emit(self, record):
        if getattr(self._guard, "busy", False):
            return
        self._guard.busy = True
        try:
            self._bus.emit("log", level=record.levelname.lower(),
                           message=record.getMessage(), created=record.created)
        except Exception:
            self.handleError(record)
        finally:
            self._guard.busy = False


class MemoryLogHandler(logging.Handler):
    """Последние строки лога: окно программы показывает их сразу при открытии."""

    def __init__(self, capacity=500, level=logging.INFO):
        super().__init__(level)
        self._lines = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self._lines.append({"level": record.levelname.lower(),
                                "message": record.getMessage(),
                                "created": record.created})
        except Exception:
            self.handleError(record)

    def lines(self):
        with self.lock:
            return list(self._lines)


def log_thread_crash(args):
    """Падение потока — в файл лога, а не только трассировкой в консоль."""
    if issubclass(args.exc_type, SystemExit):
        return
    logging.critical(tr("поток {name} упал", name=args.thread.name if args.thread else "?")
                     + "\n" + "".join(traceback.format_exception(
                         args.exc_type, args.exc_value, args.exc_traceback)).rstrip())


def setup_logging(level, log_path, max_bytes, backup_count, console=True):
    """Ротация файла логов, один уровень и для файла, и для консоли."""
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                  datefmt="%Y-%m-%d %H:%M:%S")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    file_handler = SafeRotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if console and sys.stderr is not None:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    # при -l debug urllib3 пишет строку на каждый запрос к Twitch — это шум
    logging.getLogger("urllib3").setLevel(max(level, logging.INFO))
    threading.excepthook = log_thread_crash
