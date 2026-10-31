# -*- coding: utf-8 -*-
"""
Проверка записи: есть ли в ней пропуски.

Когда streamlink не успевает за площадкой (сеть, перегруженный диск) или
выбрасывает рекламу Twitch, в записи нет куска эфира — время кадров
прыгает вперёд. Иногда время прыгает назад: площадка начала отсчёт заново,
сколько потеряно — не узнать. ffmpeg читает время каждого кадра без
декодирования (-c copy -f framecrc), это быстро: один проход по файлу.

Конвертация .ts в mp4 сама склеивает прыжки дольше DISCONTINUITY секунд
(у mpegts ffmpeg считает их обрывом потока), и в mp4 такой пропуск уже не
виден: проверять надо .ts до конвертации. Поэтому очередь обработки
проверяет запись сама, а места пропусков переносит на время готового
файла — или его кусков, если запись поделена.

Итог хранится в кэше (cache/integrity.json) по пути, размеру и времени
файла: {"gaps": [{"at", "length", "kept"}], "duration", "checked"}. at — секунда
готового видео, length — сколько пропало (None — не узнать), kept —
пропуск остался во времени видео (видео на нём замирает), а не склеен.
duration — сколько длится видео со склеенными пропусками: у .ts заголовок
файла их не учитывает, и шкала проигрывателя без поправки была бы длиннее.
"""

import json
import logging
import os
import queue
import re
import subprocess
import threading
import time

from . import util
from .i18n import tr

# Прыжок времени больше этого — пропуск, секунды. Меньше — обычная неровность потока.
GAP_MIN = 2.0
# Прыжок больше этого ffmpeg у mpegts склеивает (-dts_delta_threshold по умолчанию).
DISCONTINUITY = 10.0
_TIMEBASE = re.compile(r"^#tb (\d+): (\d+)/(\d+)")


def integrity_path(settings):
    """Где итоги проверок: cache рядом с settings.json — как у остального кэша программы."""
    return os.path.join(os.path.dirname(os.path.abspath(settings.path)), "cache", "integrity.json")


def scan_command(ffmpeg, path, stream):
    # -copyts — время кадров как в файле: иначе ffmpeg сам склеил бы прыжки
    return [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-copyts", "-i", path,
            "-map", "0:%s:0" % stream, "-c", "copy", "-f", "framecrc", "-"]


def analyse(lines, discontinuity=True):
    """
    Пропуски по строкам framecrc. discontinuity — файл mpegts: его прыжки
    дольше DISCONTINUITY конвертация склеит. Возвращает (пропуски,
    длительность готового видео) — время пропусков уже на ней.

    В .ts пропуск — прыжок времени между кадрами, в mp4 — кадр, который
    длится весь пропуск: поэтому смотрится шаг между кадрами, а не их
    длительность.
    """
    numerator, denominator = 1, 90000
    gaps = []
    last = None                # время прошлого кадра
    step = None                # обычный шаг между кадрами, в единицах файла
    timeline = 0               # где сейчас время готового видео, в единицах файла
    for line in lines:
        if line.startswith("#"):
            match = _TIMEBASE.match(line)
            if match:
                numerator, denominator = int(match.group(2)), int(match.group(3))
            continue
        fields = line.split(",")
        if len(fields) < 4:
            continue
        try:
            dts, duration = int(fields[1]), int(fields[3])
        except ValueError:
            continue
        seconds = numerator / denominator
        if last is None:
            last = dts
            if 0 < duration * seconds <= GAP_MIN:
                step = duration
            continue
        delta = dts - last
        if step is None:
            step = delta if 0 < delta * seconds <= GAP_MIN else 0
        jump = (delta - step) * seconds
        if jump > GAP_MIN:
            kept = not (discontinuity and jump > DISCONTINUITY)
            gaps.append({"at": round((timeline + step) * seconds, 3), "length": round(jump, 3), "kept": kept})
            timeline += delta if kept else step
        elif delta < -step:
            # время пошло заново: сколько пропало, не узнать; ffmpeg такое склеивает
            gaps.append({"at": round((timeline + step) * seconds, 3), "length": None, "kept": False})
            timeline += step
        else:
            timeline += delta
            if delta > 0:
                step = delta
        last = dts
    total = (timeline + (step or 0)) * numerator / denominator if last is not None else 0.0
    return gaps, round(total, 3)


def scan(ffmpeg, path, stopped=None):
    """
    Пропуски записи и длительность готового видео — или None: ffmpeg нет,
    файл не читается, проверку остановили. Смотрит видео, а у файла только
    со звуком — звук.
    """
    if not ffmpeg:
        return None
    discontinuity = path.lower().endswith(".ts")
    for stream in ("v", "a"):
        try:
            process = subprocess.Popen(scan_command(ffmpeg, path, stream), stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                       creationflags=util.subprocess_flags())
        except OSError as error:
            logging.debug("проверка %s не запустилась: %s", path, error)
            return None
        lines = []
        stopped_early = False
        try:
            for raw in process.stdout:
                if stopped is not None and stopped():
                    stopped_early = True
                    break
                lines.append(raw.decode("ascii", "replace").strip())
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait(30)
        if stopped_early:
            return None
        if any(line and not line.startswith("#") for line in lines):
            gaps, duration = analyse(lines, discontinuity)
            return {"gaps": gaps, "duration": duration}
    return None


def map_to_pieces(gaps, lengths):
    """
    Пропуски поделённой записи — по кускам: [[пропуски первого куска], …],
    время — от начала своего куска. lengths — длительность каждого куска.
    """
    result = [[] for _ in lengths]
    start = 0.0
    for index, length in enumerate(lengths):
        last = index == len(lengths) - 1
        for gap in gaps:
            if start <= gap["at"] < start + (length or 0) or (last and gap["at"] >= start):
                result[index].append(dict(gap, at=round(max(0.0, gap["at"] - start), 3)))
        start += length or 0
    return result


def summary(gaps):
    """Сколько пропусков и сколько секунд пропало (сколько узнать удалось)."""
    return {"count": len(gaps), "lost": round(sum(gap["length"] or 0 for gap in gaps), 3)}


class IntegrityStore:
    """Итоги проверок по файлам. Один на файл кэша: его делят очередь обработки и окно."""

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def at(cls, path):
        path = os.path.abspath(path)
        with cls._instances_lock:
            if path not in cls._instances:
                cls._instances[path] = cls(path)
            return cls._instances[path]

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._data = None

    @staticmethod
    def _key(path):
        stat = os.stat(path)
        # файл переписали — размер или время изменились, проверка устарела
        return "%s|%d|%d" % (os.path.normcase(os.path.abspath(path)), stat.st_size, int(stat.st_mtime))

    def _load(self):
        if self._data is None:
            try:
                with open(self.path, encoding="utf-8") as source:
                    data = json.load(source)
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}

    def get(self, path):
        """Итог проверки файла или None — не проверялся (или с тех пор изменился)."""
        try:
            key = self._key(path)
        except OSError:
            return None
        with self._lock:
            self._load()
            found = self._data.get(key)
            return json.loads(json.dumps(found)) if found else None

    def put(self, path, gaps, duration=None):
        try:
            key = self._key(path)
        except OSError:
            return
        with self._lock:
            self._load()
            self._data[key] = {"gaps": gaps, "duration": duration, "checked": time.time()}
            # проверки удалённых файлов больше не нужны
            for old in [old for old in self._data if not os.path.exists(old.split("|", 1)[0])]:
                del self._data[old]
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                temporary = self.path + ".tmp"
                with open(temporary, "w", encoding="utf-8") as target:
                    json.dump(self._data, target, ensure_ascii=False)
                os.replace(temporary, self.path)
            except OSError as error:
                logging.debug("итоги проверок не сохранились: %s", error)


class IntegrityChecker(threading.Thread):
    """
    Проверка записей по просьбе окна — по одной, в своём потоке. Итог —
    событием "integrity" в шину: {path, gaps}; gaps — None, если не проверилось.
    """

    def __init__(self, ffmpeg, store, bus=None):
        """ffmpeg() — путь к ffmpeg или None."""
        super().__init__(name="integrity", daemon=True)
        self._ffmpeg = ffmpeg
        self._store = store
        self._bus = bus
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._stopping = threading.Event()

    def check(self, path):
        """В очередь на проверку. True — поставлено, False — уже стоит."""
        with self._lock:
            if path in self._queued:
                return False
            self._queued.add(path)
        self._queue.put(path)
        return True

    def stop(self):
        self._stopping.set()

    def run(self):
        while not self._stopping.is_set():
            try:
                path = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                result = scan(self._ffmpeg(), path, stopped=self._stopping.is_set)
                if result is not None:
                    self._store.put(path, result["gaps"], result["duration"])
                    if result["gaps"]:
                        logging.info(tr("в записи {name} пропусков: {count}", name=os.path.basename(path),
                                        count=len(result["gaps"])))
                if self._bus is not None and not self._stopping.is_set():
                    self._bus.emit("integrity", path=path, gaps=result["gaps"] if result else None)
            except Exception:
                logging.exception(tr("проверка записи: сбой"))
            finally:
                with self._lock:
                    self._queued.discard(path)
