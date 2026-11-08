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
_UNFINISHED = re.compile(r"\.part\b|\.ytdl$|\.temp\.|\.f\d+\.\w+$|\.joining\.(tail\.)?(ts|mp4|mkv|m4a)\b", re.IGNORECASE)

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
# Сводка чата без видео для карточки: столбиков графика; версия — чтобы пересчитать кэш.
CHAT_FACTS_BARS = 48
CHAT_FACTS_VERSION = 1
# Сколько ffmpeg может работать над превью одновременно.
PREVIEW_WORKERS = 2


# Метка правки в имени файла (editing.py): клип отрезка или видео с чатом
# поверх. «(2)» — если такое имя уже было.
EDIT_MARK = re.compile(r" \[(?:clip (\d\d)h(\d\d)m(\d\d)s-(\d\d)h(\d\d)m(\d\d)s|chat)\](?: \(\d+\))?$")
# Кусок поделённой записи: при конвертации или кнопкой «Разделить» (editing.py).
PIECE_MARK = re.compile(r" - piece (\d+) of (\d+)(?: \(\d+\))?$")


def is_whole(path):
    """Целая запись, оставленная после «Разделить»: лежит в подпапке whole."""
    return os.path.basename(os.path.dirname(os.path.abspath(path))) == util.WHOLE_DIR


def piece_mark(stem):
    """Метка куска в имени файла (match: номер, сколько всего) или None."""
    return PIECE_MARK.search(stem)


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


def piece_total(stem):
    """Сколько всего кусков в делении, к которому относится файл; не кусок — None."""
    mark = PIECE_MARK.search(stem)
    return int(mark.group(2)) if mark else None


def split_groups(entries):
    """
    Одну запись можно поделить ещё раз, на другое число кусков («из 2» и «из 3»
    рядом): такие деления — разные, вместе это не части одного эфира. entries —
    [(часть эфира, всего кусков или None)]; возвращает метку деления для каждой:
    "" — общая карточка эфира, «of N» — своё деление. Пока деление у части одно,
    его куски идут вместе с остальными частями эфира, как раньше.
    """
    totals = {}
    for part, total in entries:
        if total:
            totals.setdefault(part, set()).add(total)
    clashing = {part for part, found in totals.items() if len(found) > 1}
    return ["of %d" % total if total and part in clashing else "" for part, total in entries]


def _separate_splits(items):
    """Записи эфира, поделённые по-разному: каждое деление — своей карточкой (split_groups)."""
    by_broadcast = {}
    for item in items:
        if item["kind"] == "record" and item["broadcast"]:
            by_broadcast.setdefault(item["broadcast"], []).append(item)
    for group in by_broadcast.values():
        totals = [piece_total(os.path.splitext(os.path.basename(item["path"]))[0]) for item in group]
        for item, label in zip(group, split_groups([(item.get("part"), total) for item, total in zip(group, totals)])):
            if label:
                item["broadcast"] += "|" + label


def broadcast_parts(path):
    """
    Все части эфира, к которому относится запись, по порядку: [(номер, путь)].
    Части после обрывов и куски поделённой записи идут подряд, номер — по
    порядку с единицы. Одна запись без частей — список из неё самой.
    """
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    if EDIT_MARK.search(stem):
        return [(1, path)]
    key = broadcast_key(stem)
    if key is None:
        # имя не по образцу программы, но запись поделена — её куски подряд
        return piece_parts(path)
    own = parse_recording_name(stem).get("part"), piece_total(stem)
    found = []
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
        found.append((name, parse_recording_name(stem), piece_total(stem)))
    # поделена по-разному — только своё деление (или эфир без этих делений)
    groups = split_groups([(parsed.get("part"), total) for _, parsed, total in found] + [own])
    mine = groups.pop()
    parts = {}
    for (name, parsed, _), group in zip(found, groups):
        if group != mine:
            continue
        # part00 — начало эфира, скачанное из архива площадки: идёт первым
        part = parsed.get("part")
        order = (1 if part is None else part, parsed.get("piece") or 0)
        candidate = os.path.join(folder, name)
        # часть уже сконвертирована, а .ts ещё лежит — смотреть сконвертированную
        if order not in parts or not candidate.lower().endswith(".ts"):
            parts[order] = candidate
    if not parts:
        return [(1, path)]
    return [(number, parts[order]) for number, order in enumerate(sorted(parts), 1)]


def piece_parts(path):
    """
    Куски поделённого видео по порядку: [(номер, путь)] — для скачанного и
    файлов с именем не по образцу записей. Не кусок — список из него самого.
    """
    folder = os.path.dirname(path)
    stem = os.path.splitext(os.path.basename(path))[0]
    mark = PIECE_MARK.search(stem)
    if not mark or EDIT_MARK.search(stem):
        return [(1, path)]
    base = stem[:mark.start()].lower()
    pieces = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return [(1, path)]
    for name in names:
        other, extension = os.path.splitext(name)
        if extension.lower() not in MEDIA_EXTENSIONS or _UNFINISHED.search(name) \
                or name.lower().endswith(chat.SUFFIXES["video"]):
            continue
        found = PIECE_MARK.search(other)
        # поделено ещё раз на другое число — то другое деление
        if not found or other[:found.start()].lower() != base or found.group(2) != mark.group(2):
            continue
        number = int(found.group(1))
        candidate = os.path.join(folder, name)
        # кусок уже сконвертирован, а .ts ещё лежит — смотреть сконвертированный
        if number not in pieces or not candidate.lower().endswith(".ts"):
            pieces[number] = candidate
    if not pieces:
        return [(1, path)]
    return [(index, pieces[number]) for index, number in enumerate(sorted(pieces), 1)]


def parse_download_name(stem):
    """'xqc - 2026-10-24 - заголовок [v123] 720' -> заголовок, дата, id."""
    match = _DOWNLOAD_NAME.match(stem)
    if not match:
        return {}
    moment = datetime.datetime.strptime(match["date"], "%Y-%m-%d")
    return {"title": match["title"] or "", "timestamp": moment.timestamp(),
            "video_id": match["id"] or ""}


def walk_media(base):
    """
    (платформа, канал, путь) для каждого готового видео в base/<платформа>/<канал>
    и в его подпапке whole — там целые записи, оставленные после «Разделить».
    """
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
            for folder in (channel_dir, os.path.join(channel_dir, util.WHOLE_DIR)):
                try:
                    names = os.listdir(folder)
                except OSError:
                    continue
                for name in names:
                    # видео чата — не отдельная запись, а часть своего видео
                    if name.lower().endswith(MEDIA_EXTENSIONS) and not _UNFINISHED.search(name) \
                            and not name.lower().endswith(chat.SUFFIXES["video"]):
                        yield platform, label, os.path.join(folder, name)


def walk_chats(base):
    """
    (платформа, канал, путь .chat.html) для чатов, скачанных без видео («только
    чат»): рядом нет видео с тем же именем.
    """
    html, json_suffix = chat.SUFFIXES["html"], chat.SUFFIXES["json"]
    try:
        platforms = sorted(os.listdir(base))
    except OSError:
        return
    for platform in platforms:
        try:
            labels = sorted(os.listdir(os.path.join(base, platform)))
        except OSError:
            continue
        for label in labels:
            folder = os.path.join(base, platform, label)
            try:
                names = set(os.listdir(folder))
            except OSError:
                continue
            for name in sorted(names):
                if not name.endswith(html):
                    continue
                stem = name[:-len(html)]
                if stem + json_suffix in names and not any(stem + extension in names for extension in MEDIA_EXTENSIONS):
                    yield platform, label, os.path.join(folder, name)


def chat_only_json(path):
    """.chat.json чата без видео по его .chat.html; не такой — None."""
    html = chat.SUFFIXES["html"]
    return path[:-len(html)] + chat.SUFFIXES["json"] if str(path).endswith(html) else None


def read_chat_facts(json_path):
    """Сводка чата для карточки (Library.chat_facts) прямо из .chat.json."""
    try:
        with open(json_path, encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return {}
    video = data.get("video") or {}
    try:
        start = float(video.get("start") or 0)
    except (TypeError, ValueError):
        start = 0.0
    times, names = [], set()
    for comment in data.get("comments") or ():
        try:
            times.append(float(comment["content_offset_seconds"]) - start)
        except (KeyError, TypeError, ValueError):
            continue
        commenter = comment.get("commenter") or {}
        names.add(str(commenter.get("name") or commenter.get("display_name") or "").casefold())
    try:
        length = float(video.get("length") or 0) or float(video.get("end") or 0) - start
    except (TypeError, ValueError):
        length = 0.0
    length = max(length, max(times) if times else 0.0)
    bars = [0] * CHAT_FACTS_BARS
    if times and length > 0:
        for moment in times:
            bars[min(CHAT_FACTS_BARS - 1, max(0, int(moment / length * CHAT_FACTS_BARS)))] += 1
        top = max(bars)
        bars = [round(count / top * 100) for count in bars]
    return {"length": round(length, 3), "messages": len(times), "chatters": len(names - {""}), "bars": bars}


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
        # чаты, скачанные без видео: своей карточкой, со сводкой и поиском
        for platform, label, path in walk_chats(self.folders()["download"]):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            item = self._describe("download", platform, label, path, stat, jobs)
            stem = path[:-len(chat.SUFFIXES["html"])]
            files = chat.existing(stem + ".mp4")
            facts = self.chat_facts(chat_only_json(path))
            # не кадр и длительность эфира, а сам чат: сообщений, зрителей, график
            item.update(video_kind="chat", chat=files, thumbnail="", chat_facts=facts,
                        duration=facts.get("length") or None,
                        # размер — всех файлов чата: HTML сам по себе крошечный
                        size=sum(os.path.getsize(file) for file in files.values() if os.path.isfile(file)),
                        title=item["title"] if item["title"] != os.path.basename(stem) + ".chat" else
                        parse_download_name(os.path.basename(stem)).get("title") or os.path.basename(stem))
            items.append(item)
        _separate_splits(items)
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
            # когда файл появился на диске (создан): «сначала недавно добавленные» —
            # только что скачанная старая запись эфира тоже наверху
            "added": getattr(stat, "st_birthtime", None) or stat.st_ctime,
            "part": None,
            "size": stat.st_size,
            "duration": None,
            "thumbnail": "",
            "url": "",
            "chat": chat.existing(path),   # чат рядом: html, json, video
            "broadcast": "",               # ключ эфира: у его частей он общий
            "edit": "",                    # clip или chat — сделано в окне из другой записи
            "gaps": None,                  # пропуски: {"count", "lost"}; None — не проверялась
            "whole": is_whole(path),       # целая запись, оставленная рядом с кусками
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
        # кусок поделённого видео: сведения — как у целого, карточка — общая
        piece = None if edit else PIECE_MARK.search(stem)
        whole_stem = stem[:piece.start()] if piece else source_stem
        if kind == "record":
            item.update(parse_recording_name(source_stem))
            # части одного эфира окно показывает одной карточкой; правка — не часть
            key = None if edit else broadcast_key(stem)
            item["broadcast"] = "|".join((os.path.dirname(path),) + key) if key else ""
        else:
            # целая, оставленная после «Разделить», лежала папкой выше
            folder = os.path.dirname(os.path.dirname(path)) if item["whole"] else os.path.dirname(path)
            whole = os.path.join(folder, whole_stem + os.path.splitext(path)[1])
            job = jobs.get(os.path.normcase(os.path.abspath(whole)))
            if job:
                item.update(title=job.get("title") or "", channel=job.get("channel") or label,
                            thumbnail=job.get("thumbnail") or "", url=job.get("url") or "",
                            timestamp=job.get("timestamp"), video_kind=job.get("kind") or "")
                # длительность из очереди — у целого видео, а не у куска
                if job.get("duration") and not job.get("section") and not piece:
                    item["duration"] = item["duration"] or job["duration"]
            else:
                parsed = parse_download_name(whole_stem)
                parsed.pop("video_id", None)
                item.update(parsed)
        if piece and not item["broadcast"]:
            item["piece"] = int(piece.group(1))
            # поделено ещё раз на другое число — отдельной карточкой
            item["broadcast"] = "|".join((os.path.dirname(path), "pieces", whole_stem.lower(), piece.group(2)))
        if edit == "clip":
            item["video_kind"] = "clip"
        item["title"] = item["title"] or whole_stem
        item["timestamp"] = item["timestamp"] or stat.st_mtime
        return item

    def chat_facts(self, json_path):
        """
        Чат без видео для карточки: {"length", "messages", "chatters", "bars"} —
        bars: столбики графика активности 0…100. Считается раз, дальше из кэша:
        .chat.json бывает в сотню мегабайт.
        """
        try:
            stat = os.stat(json_path)
        except (OSError, TypeError):
            return {}
        key = self._key(json_path, stat)
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached.get("chat_facts") == CHAT_FACTS_VERSION:
            return cached["facts"]
        facts = read_chat_facts(json_path)
        with self._lock:
            self._cache[key] = {"chat_facts": CHAT_FACTS_VERSION, "facts": facts}
            self._save_cache()
        return facts

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
        json_path = chat_only_json(path)
        if json_path is not None:
            # чат без видео: в корзину — все его файлы
            for target in chat.existing(json_path[:-len(chat.SUFFIXES["json"])] + ".mp4").values():
                util.send_to_recycle_bin(target)
                logging.info(tr("отправлено в корзину: {path}", path=target))
            return
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
