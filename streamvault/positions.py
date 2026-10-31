# -*- coding: utf-8 -*-
"""
Где остановился просмотр каждой записи в окне программы.

Позиции хранятся у программы (cache/positions.json), а не в памяти окна:
окно в pywebview и в браузере — разные страницы, а запись одна. Досмотренное
(последние 2 %) не запоминается — его открывают с начала.
"""

import json
import logging
import os
import threading
import time

# Досмотрено, если осталось меньше этой доли записи.
WATCHED_SHARE = 0.02
# Короче этого от начала — не повод предлагать «продолжить».
MIN_POSITION = 10
# Сколько записей помнить: самые давние забываются.
LIMIT = 500


def _key(path):
    return os.path.normcase(os.path.abspath(path))


class Positions:
    """Позиции просмотра: path -> секунды. path=None — только в памяти (тесты)."""

    def __init__(self, path=None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._data = self._load()

    def get(self, video):
        """С какого места продолжить запись или None — с начала."""
        with self._lock:
            entry = self._data.get(_key(video))
        return entry["position"] if entry else None

    def save(self, video, position, duration=None):
        """Запомнить место. Досмотренное и самое начало — забываются."""
        try:
            position = float(position)
            duration = float(duration) if duration else None
        except (TypeError, ValueError):
            raise ValueError("position")
        key = _key(video)
        with self._lock:
            if position < MIN_POSITION or (duration and position >= duration * (1 - WATCHED_SHARE)):
                changed = self._data.pop(key, None) is not None
            else:
                self._data[key] = {"position": round(position, 1), "duration": duration,
                                   "updated": self._clock()}
                changed = True
                if len(self._data) > LIMIT:
                    oldest = min(self._data, key=lambda name: self._data[name]["updated"])
                    del self._data[oldest]
            if changed:
                self._write()

    def _load(self):
        if not self._path:
            return {}
        try:
            with open(self._path, encoding="utf-8") as source:
                data = json.load(source)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {key: value for key, value in data.items()
                if isinstance(value, dict) and isinstance(value.get("position"), (int, float))
                and isinstance(value.get("updated", 0), (int, float))}

    def _write(self):
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            temporary = self._path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump(self._data, target, ensure_ascii=False)
            os.replace(temporary, self._path)
        except OSError as error:
            logging.debug("позиции просмотра не сохранились: %s", error)
