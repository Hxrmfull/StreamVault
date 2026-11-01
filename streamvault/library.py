# -*- coding: utf-8 -*-
"""
Библиотека: записанное и скачанное на диске.

Записи эфиров лежат в processed/<платформа>/<канал>, скачанное — в папке
загрузок по той же схеме. Сведения о файле берутся из его имени (его строит
сама программа), а для скачанного — ещё из очереди загрузок: там название,
ссылка и превью с сайта. Кадр-превью и длительность записи эфира делает
ffmpeg, один раз: дальше они берутся из кэша.
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import threading

from . import util
from .archive import chat
from .i18n import tr

MEDIA_EXTENSIONS = (".mp4", ".ts", ".m4a", ".mkv", ".webm", ".mp3")

# Промежуточные файлы ffmpeg и yt-dlp: пока они есть, видео не готово.
# .joining — склейка начала эфира с записью (live/joining.py)
_UNFINISHED = re.compile(r"\.part\b|\.ytdl$|\.temp\.|\.f\d+\.\w+$|\.joining\.(ts|mp4)\b", re.IGNORECASE)

_RECORDING_NAME = re.compile(
    r"^(?P<label>.+?) - (?P<date>\d{4}-\d{2}-\d{2}) (?P<h>\d{2})h(?P<m>\d{2})m(?P<s>\d{2})s"
    r"(?: - (?P<title>.*?))?(?: - part(?P<part>\d+))?"
    r"(?: - piece (?P<piece>\d+) of (?P<pieces>\d+))?(?: \(\d+\))?$")
_DOWNLOAD_NAME = re.compile(
    r"^(?P<label>.+?) - (?P<date>\d{4}-\d{2}-\d{2})(?: - (?P<title>.*?))?"
    r"(?: \[(?P<id>[^\]]+)\].*)?$")
_DURATION = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)(?:, start: (-?\d+(?:\.\d+)?))?")
_MP4_INPUT = re.compile(r"^Input #0, mov,", re.MULTILINE)

THUMB_WIDTH = 320
# Как считана длительность в кэше превью; 2 — с поправкой на начало трансляции.
PROBE_VERSION = 2
# Сколько ffmpeg может работать над превью одновременно.
PREVIEW_WORKERS = 2


# Метка правки в имени файла (editing.py): клип отрезка или видео с чатом
# поверх. «(2)» — если такое имя уже было.
EDIT_MARK = re.compile(r" \[(?:clip (\d\d)h(\d\d)m(\d\d)s-(\d\d)h(\d\d)m(\d\d)s|chat)\](?: \(\d+\))?$")


def edit_kind(stem):
    """clip, chat или None — что это за правка, по метке в имени файла."""
    match = EDIT_MARK.search(stem)
    if not match:
        return None
    return "clip" if match.group(1) else "chat"


def parse_recording_name(stem):
    """
    'xqc - 2026-10-25 12h00m00s - заголовок - part02 - piece 1 of 3' -> заголовок,
    время, часть эфира (после обрыва) и кусок части (запись поделили при конвертации).
    """
    match = _RECORDING_NAME.match(stem)
    if not match:
        return {}
    moment = datetime.datetime.strptime(
        "%s %s:%s:%s" % (match["date"], match["h"], match["m"], match["s"]), "%Y-%m-%d %H:%M:%S")
    return {"title": match["title"] or "", "timestamp": moment.timestamp(),
            "part": int(match["part"]) if match["part"] else None,
            "piece": int(match["piece"]) if match["piece"] else None}


def broadcast_key(stem):
    """
    Чем различаются эфиры: канал и время начала записи. Части одного эфира
    («… - part02») пишутся с тем же временем, а название бывает новым —
    его меняют посреди эфира.
    """
    match = _RECORDING_NAME.match(stem)
    if not match:
        return None
    return (match["label"].lower(), match["date"], match["h"], match["m"], match["s"])


def broadcast_parts(path):
    """
    Все части эфира, к которому относится запись, по порядку: [(номер, путь)].
    Части после обрывов и куски поделённой записи идут подряд, номер — по
    порядку с единицы. Одна запись без частей — список из неё самой.
    """
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    key = None if EDIT_MARK.search(stem) else broadcast_key(stem)
    if key is None:
        return [(1, path)]
    parts = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return [(1, path)]
    for name in names:
        stem, extension = os.path.splitext(name)
        if extension.lower() not in MEDIA_EXTENSIONS or _UNFINISHED.search(name) \
                or name.lower().endswith(chat.SUFFIXES["video"]) or EDIT_MARK.search(stem) \
                or broadcast_key(stem) != key:
            continue
        parsed = parse_recording_name(stem)
        # part00 — начало эфира, скачанное из архива площадки: идёт первым
        part = parsed.get("part")
        order = (1 if part is None else part, parsed.get("piece") or 0)
        candidate = os.path.join(folder, name)
        # часть уже сконвертирована, а .ts ещё лежит — смотреть mp4
        if order not in parts or candidate.lower().endswith(".mp4"):
            parts[order] = candidate
    if not parts:
        return [(1, path)]
    return [(number, parts[order]) for number, order in enumerate(sorted(parts), 1)]


def parse_download_name(stem):
    """'xqc - 2026-10-24 - заголовок [v123] 720' -> заголовок, дата, id."""
    match = _DOWNLOAD_NAME.match(stem)
    if not match:
        return {}
    moment = datetime.datetime.strptime(match["date"], "%Y-%m-%d")
    return {"title": match["title"] or "", "timestamp": moment.timestamp(),
            "video_id": match["id"] or ""}


def walk_media(base):
    """(платформа, канал, путь) для каждого готового видео в base/<платформа>/<канал>."""
    try:
        platforms = sorted(os.listdir(base))
    except OSError:
        return
    for platform in platforms:
        platform_dir = os.path.join(base, platform)
        if not os.path.isdir(platform_dir):
            continue
        try:
            labels = sorted(os.listdir(platform_dir))
        except OSError:
            continue
        for label in labels:
            channel_dir = os.path.join(platform_dir, label)
            if not os.path.isdir(channel_dir):
                continue
            try:
                names = os.listdir(channel_dir)
            except OSError:
                continue
            for name in names:
                # видео чата — не отдельная запись, а часть своего видео
                if name.lower().endswith(MEDIA_EXTENSIONS) and not _UNFINISHED.search(name) \
                        and not name.lower().endswith(chat.SUFFIXES["video"]):
                    yield platform, label, os.path.join(channel_dir, name)


def delete_with_chat(path):
    """В корзину Windows: случайно удалённое можно вернуть. Чат видео — туда же."""
    for target in [path, *chat.existing(path).values()]:
        util.send_to_recycle_bin(target)
        logging.info(tr("отправлено в корзину: {path}", path=target))


def parse_duration(output):
    """Длительность из того, что ffmpeg печатает, открыв файл; не нашлась — None."""
    match = _DURATION.search(output)
    if not match:
        return None
    hours, minutes, seconds, start = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    # Twitch бывает отдаёт эфир кусками mp4, и streamlink пишет их в .ts как есть.
    # Время в них идёт от начала трансляции, и ffmpeg считает длительность
    # от нуля: полторы минуты записи становились восемью часами.
    if start and _MP4_INPUT.search(output) and 1 <= float(start) < duration:
        duration -= float(start)
    return duration


def probe_duration(ffmpeg, path):
    """
    Длительность видео. Годится и для записи, которая ещё пишется: у .ts
    ffmpeg смотрит время последних кадров. Не узнать — None.
    """
    if not ffmpeg:
        return None
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True,
                                timeout=30, creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired) as error:
        logging.debug("длительность %s не узнать: %s", path, error)
        return None
    return parse_duration(result.stderr.decode("utf-8", "replace"))


class Library:
    """Список записей и загрузок, превью, длительность, удаление в корзину."""

    def __init__(self, settings, downloads, cache_dir, ffmpeg, integrity=None):
        """
        ffmpeg — FFmpegLocator: его могут установить, пока программа работает.
        integrity — итоги проверок записей на пропуски (IntegrityStore).
        """
        self._settings = settings
        self._downloads = downloads
        self._ffmpeg_locator = ffmpeg
        self._cache_dir = cache_dir
        self._cache_path = os.path.join(cache_dir, "library.json")
        self._lock = threading.Lock()
        self._workers = threading.Semaphore(PREVIEW_WORKERS)
        self._cache = self._load_cache()
        self._integrity = integrity

    # ------------------------------------------------------------ список

    def folders(self):
        return {"record": os.path.join(self._settings["root_path"], util.FINISHED_DIR),
                "download": self._downloads.downloads_dir()}

    def items(self):
        """Всё, что есть на диске, от новых к старым."""
        jobs = {}
        for job in self._downloads.snapshot()["jobs"]:
            if job.get("file"):
                jobs[os.path.normcase(os.path.abspath(job["file"]))] = job

        items = []
        for kind, base in self.folders().items():
            for platform, label, path in walk_media(base):
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                items.append(self._describe(kind, platform, label, path, stat, jobs))
        items.sort(key=lambda item: item["timestamp"] or 0, reverse=True)
        return items

    def _describe(self, kind, platform, label, path, stat, jobs):
        stem = os.path.splitext(os.path.basename(path))[0]
        item = {
            "path": path,
            "name": os.path.basename(path),
            "kind": kind,             # record — запись эфира, download — скачанное
            "video_kind": "stream" if kind == "record" else "",
            "platform": platform,
            "channel": label,
            "channel_id": "%s:%s" % (platform, label),
            "title": "",
            "timestamp": None,
            "part": None,
            "size": stat.st_size,
            "duration": None,
            "thumbnail": "",
            "url": "",
            "chat": chat.existing(path),   # чат рядом: html, json, video
            "broadcast": "",               # ключ эфира: у его частей он общий
            "edit": "",                    # clip или chat — сделано в окне из другой записи
            "gaps": None,                  # пропуски: {"count", "lost"}; None — не проверялась
        }
        checked = self._integrity.get(path) if self._integrity is not None else None
        if checked is not None:
            from .integrity import summary
            item["gaps"] = summary(checked["gaps"])
        cached = self._cache.get(self._key(path, stat))
        # устаревшую длительность .ts не отдаём — её пересчитает превью
        if cached and (cached.get("probe") == PROBE_VERSION or not path.lower().endswith(".ts")):
            item["duration"] = cached.get("duration")

        # клип или видео с чатом, сделанные в окне: сведения — как у исходника
        edit = edit_kind(stem)
        item["edit"] = edit or ""
        source_stem = EDIT_MARK.sub("", stem)
        if kind == "record":
            item.update(parse_recording_name(source_stem))
            # части одного эфира окно показывает одной карточкой; правка — не часть
            key = None if edit else broadcast_key(stem)
            item["broadcast"] = "|".join((os.path.dirname(path),) + key) if key else ""
        else:
            job = jobs.get(os.path.normcase(os.path.abspath(path)))
            if job:
                item.update(title=job.get("title") or "", channel=job.get("channel") or label,
                            thumbnail=job.get("thumbnail") or "", url=job.get("url") or "",
                            timestamp=job.get("timestamp"), video_kind=job.get("kind") or "")
                if job.get("duration") and not job.get("section"):
                    item["duration"] = item["duration"] or job["duration"]
            else:
                parsed = parse_download_name(source_stem)
                parsed.pop("video_id", None)
                item.update(parsed)
        if edit == "clip":
            item["video_kind"] = "clip"
        item["title"] = item["title"] or stem
        item["timestamp"] = item["timestamp"] or stat.st_mtime
        return item

    # ------------------------------------------------------------ превью

    def preview(self, path):
        """
        Кадр из видео (data:-ссылка) и длительность. У файла только со звуком
        кадра нет — тогда пустая строка. Готовое берётся из кэша.
        """
        path = os.path.abspath(path)
        stat = os.stat(path)
        key = self._key(path, stat)
        target = os.path.join(self._cache_dir, "thumbs",
                              hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")
        with self._lock:
            cached = dict(self._cache.get(key) or {})
        if "frame" not in cached and not self._ffmpeg_locator.path():
            # без ffmpeg кадр не достать; не запоминаем — после установки получится
            return {"thumbnail": "", "duration": cached.get("duration")}
        if "frame" not in cached:
            with self._workers:
                cached["duration"] = self._probe_duration(path)
                cached["frame"] = self._extract_frame(path, target, cached["duration"])
                cached["probe"] = PROBE_VERSION
            with self._lock:
                self._cache[key] = cached
                self._save_cache()
        elif cached.get("probe") != PROBE_VERSION and path.lower().endswith(".ts") \
                and self._ffmpeg_locator.path():
            # длительность .ts из кусков mp4 раньше считалась неверно — пересчитываем
            with self._workers:
                cached["duration"] = self._probe_duration(path)
                cached["probe"] = PROBE_VERSION
            with self._lock:
                self._cache[key] = cached
                self._save_cache()
        thumbnail = ""
        if cached.get("frame") and os.path.exists(target):
            with open(target, "rb") as source:
                thumbnail = "data:image/jpeg;base64," + base64.b64encode(source.read()).decode("ascii")
        return {"thumbnail": thumbnail, "duration": cached.get("duration")}

    def _ffmpeg(self, args, timeout):
        return subprocess.run([self._ffmpeg_locator.path(), "-hide_banner", "-nostdin", *args],
                              capture_output=True, timeout=timeout,
                              creationflags=util.subprocess_flags())

    def _probe_duration(self, path):
        """Длительность из заголовка, который ffmpeg печатает, открыв файл."""
        try:
            result = self._ffmpeg(["-i", path], timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.debug("длительность %s не узнать: %s", path, error)
            return None
        return parse_duration(result.stderr.decode("utf-8", "replace"))

    def _extract_frame(self, path, target, duration):
        """Кадр на десятой части видео, но не дальше минуты: начало часто бывает заставкой."""
        moment = min(duration * 0.1, 60) if duration else 0
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            self._ffmpeg(["-v", "error", "-y", "-ss", "%.2f" % moment, "-i", path,
                          "-frames:v", "1", "-vf", "scale=%d:-2" % THUMB_WIDTH,
                          "-q:v", "5", target], timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            logging.debug("кадр из %s не получен: %s", path, error)
            return False
        return os.path.exists(target) and os.path.getsize(target) > 0

    # ------------------------------------------------------------ удаление

    def delete(self, path):
        delete_with_chat(path)

    # ------------------------------------------------------------ кэш

    @staticmethod
    def _key(path, stat):
        # файл перезаписали — размер или время изменились, превью делается заново
        return "%s|%d|%d" % (os.path.normcase(path), stat.st_size, int(stat.st_mtime))

    def _load_cache(self):
        try:
            with open(self._cache_path, encoding="utf-8") as source:
                data = json.load(source)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        try:
            os.makedirs(self._cache_dir, exist_ok=True)
            temporary = self._cache_path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump(self._cache, target, ensure_ascii=False)
            os.replace(temporary, self._cache_path)
        except OSError as error:
            logging.debug("кэш библиотеки не сохранился: %s", error)
