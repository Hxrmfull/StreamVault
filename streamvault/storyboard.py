# -*- coding: utf-8 -*-
"""
Кадры для шкалы проигрывателя: наведёшь на шкалу — видно, что в этом месте.

ffmpeg один раз проходит запись и собирает маленькие кадры через равные
промежутки в листы (tile 10×10) — как раскадровки на Twitch и YouTube.
Декодируются только ключевые кадры (-skip_frame nokey), поэтому даже
многочасовой эфир готов за минуту-две, а не за время перекодирования.

Листы лежат в cache/storyboards/<хэш файла>/ и делаются заново, только
если файл изменился. Старые раскадровки убираются: их не больше MAX_KEPT.
"""

import hashlib
import json
import logging
import math
import os
import shutil
import subprocess
import threading

from . import util

TILE_WIDTH = 160
COLUMNS = ROWS = 10
# Кадров на запись не больше этого: у шестичасового эфира — кадр раз в 45 секунд.
MAX_TILES = 500
MIN_INTERVAL = 2
MAX_KEPT = 100
# Дольше этого ffmpeg над раскадровкой не работает, секунды.
TIMEOUT = 30 * 60
META = "storyboard.json"


def interval_for(duration):
    """Через сколько секунд записи — следующий кадр раскадровки."""
    return max(MIN_INTERVAL, math.ceil((duration or 0) / MAX_TILES))


def storyboard_command(ffmpeg, path, interval, pattern):
    # Плитка k — первый ключевой кадр не раньше k·interval: проигрыватель показывает её
    # на отрезке [k·interval, (k+1)·interval). Фильтр fps брал последний кадр своего
    # отрезка ±interval/2, и плитка опережала место под мышью на 6–8 секунд.
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-skip_frame", "nokey", "-i", path,
            "-an", "-sn", "-dn", "-vf", "select=gte(t\\,selected_n*%d),scale=%d:-2,tile=%dx%d"
            % (interval, TILE_WIDTH, COLUMNS, ROWS),
            "-fps_mode", "passthrough", "-q:v", "6", pattern]


class Storyboards:
    """
    get(путь, длительность) — раскадровка записи: готова, делается или её
    не будет. Делается по одной, в своём потоке: окно спрашивает снова.
    """

    def __init__(self, cache_dir, ffmpeg):
        """ffmpeg() — путь к ffmpeg или None: его могут поставить, пока программа работает."""
        self._root = os.path.join(cache_dir, "storyboards")
        self._ffmpeg = ffmpeg
        self._lock = threading.Lock()
        self._working = set()
        self._worker = threading.Semaphore(1)

    def _folder(self, path):
        stat = os.stat(path)
        key = "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)), stat.st_size, int(stat.st_mtime))
        return os.path.join(self._root, hashlib.sha1(key.encode("utf-8")).hexdigest())

    def get(self, path, duration):
        """
        {"state": "ready", "interval", "columns", "rows", "sheets": [пути листов]},
        {"state": "working"} или {"state": "none"} — без ffmpeg, у файла
        только со звуком, ffmpeg не справился.
        """
        try:
            folder = self._folder(path)
        except OSError:
            return {"state": "none"}
        try:
            with open(os.path.join(folder, META), encoding="utf-8") as source:
                meta = json.load(source)
        except (OSError, ValueError):
            meta = None
        if meta is not None:
            if not meta.get("sheets"):
                return {"state": "none"}
            try:
                os.utime(folder)     # смотрели — значит, нужна: убирается та, что давно не открывали
            except OSError:
                pass
            return {"state": "ready", "interval": meta["interval"], "columns": COLUMNS, "rows": ROWS,
                    "sheets": [os.path.join(folder, name) for name in meta["sheets"]]}
        ffmpeg = self._ffmpeg()
        if not ffmpeg or not duration:
            return {"state": "none"}
        with self._lock:
            if folder not in self._working:
                self._working.add(folder)
                threading.Thread(target=self._make, args=(ffmpeg, path, duration, folder),
                                 name="storyboard", daemon=True).start()
        return {"state": "working"}

    def _make(self, ffmpeg, path, duration, folder):
        try:
            with self._worker:
                self._build(ffmpeg, path, duration, folder)
        except Exception:
            logging.exception("раскадровка %s: сбой", path)
        finally:
            with self._lock:
                self._working.discard(folder)

    def _build(self, ffmpeg, path, duration, folder):
        # листы — во временную папку: недоделанная раскадровка не выдаётся за готовую
        temporary = folder + ".part"
        shutil.rmtree(temporary, ignore_errors=True)
        os.makedirs(temporary)
        interval = interval_for(duration)
        sheets = []
        try:
            result = subprocess.run(storyboard_command(ffmpeg, path, interval, os.path.join(temporary, "%03d.jpg")),
                                    capture_output=True, timeout=TIMEOUT, creationflags=util.subprocess_flags())
            if result.returncode == 0:
                sheets = sorted(name for name in os.listdir(temporary) if name.endswith(".jpg"))
            else:
                logging.debug("раскадровка %s: ffmpeg %s", path, result.stderr.decode("utf-8", "replace")[-300:])
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.debug("раскадровка %s не сделана: %s", path, error)
        # и неудача запоминается: у файла только со звуком кадров не будет и потом
        with open(os.path.join(temporary, META), "w", encoding="utf-8") as target:
            json.dump({"interval": interval, "sheets": sheets}, target)
        shutil.rmtree(folder, ignore_errors=True)
        os.replace(temporary, folder)
        self._prune()

    def _prune(self):
        """Старые раскадровки — прочь: держим MAX_KEPT последних."""
        try:
            folders = [os.path.join(self._root, name) for name in os.listdir(self._root)
                       if not name.endswith(".part")]
        except OSError:
            return
        folders.sort(key=lambda folder: os.path.getmtime(folder), reverse=True)
        for old in folders[MAX_KEPT:]:
            shutil.rmtree(old, ignore_errors=True)
