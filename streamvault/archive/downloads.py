# -*- coding: utf-8 -*-
"""
Очередь загрузок готовых записей и клипов.

Каждая загрузка — отдельный процесс yt-dlp. Одновременно идёт не больше
max_downloads, остальные ждут. Очередь хранится в downloads.json: прерванные
загрузки продолжаются со следующего запуска с того места, где остановились.
"""

import copy
import datetime
import glob
import importlib.util
import json
import logging
import os
import re
import subprocess
import threading
import time
import traceback
import uuid

from .. import sites, util
from ..ffmpeg import FFmpegLocator, own_dir_for
from ..twitchdownloader import TwitchDownloaderLocator
from ..twitchdownloader import own_dir_for as twitchdownloader_dir
from ..i18n import tr
from ..platforms import channel_label
from ..util import MAX_PATH_LEN, sanitize_filename
from . import chat, dashsection, hlssection, vodchat, ytdlp, ytdlp_update

# Состояния загрузки.
QUEUED = "queued"            # ждёт свободного места
DOWNLOADING = "downloading"
PROCESSING = "processing"    # yt-dlp склеивает или перепаковывает
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

UNFINISHED = (QUEUED, DOWNLOADING, PROCESSING, PAUSED, FAILED)

# Качество «только чат»: видео не качается, только чат записи эфира или клипа.
CHAT_ONLY = "chat"
# Чат своей записи из архива чатов (archive/chatlogs.py) — когда записи эфира на Twitch нет.
CHAT_ARCHIVE = "chat-archive"

# Поиск записи эфира к своей записи (чат к ней): столько записей с вкладки
# канала, у стольких из них спросить время и длину, если в списке их нет, и
# насколько время записи может расходиться с её началом и концом, секунды
VOD_SEARCH_PAGE = 24
VOD_SEARCH_INSPECT = 6
VOD_SLACK = 300


def _shift_chat(path, begin, length):
    """
    Чат отрезка записи эфира — со временем от начала отрезка. TwitchDownloader
    оставляет время от начала записи эфира (и пишет начало отрезка в video.start),
    свой загрузчик чата уже считает от начала отрезка: сдвигается только первое.
    """
    with open(path, encoding="utf-8") as source:
        data = json.load(source)
    comments = data.get("comments") or []
    video = data.get("video") or {}
    offsets = [float(comment.get("content_offset_seconds", 0)) for comment in comments]
    absolute = (float(video.get("start") or 0) >= begin - 1) or (offsets and min(offsets) >= begin - 5)
    if absolute:
        kept = []
        for comment in comments:
            moment = float(comment.get("content_offset_seconds", 0)) - begin
            if -1 <= moment <= length + 1:
                comment = dict(comment, content_offset_seconds=round(max(0.0, moment), 3))
                kept.append(comment)
        data["comments"] = kept
    data["video"] = dict(video, start=0, end=round(length), length=round(length))
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False)
    os.replace(temporary, path)


def _plain_title(title):
    """Название для сравнения: без регистра, лишних пробелов и знаков, которых нет в имени файла."""
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|]', "", str(title or ""))).strip().casefold()
ACTIVE = (DOWNLOADING, PROCESSING)

# Как часто слать в шину событие о прогрессе одной загрузки, секунды.
PROGRESS_EVENT_INTERVAL = 0.5
# Сколько раз спрашивать страницу списка канала, пока она приходит пустой.
EMPTY_PAGE_ATTEMPTS = 3
# «Скачать всё»: список канала страницами по столько, не больше стольких страниц.
ALL_PAGE_SIZE = 100
ALL_PAGES = 30

def _file_size(path):
    try:
        return os.path.getsize(path) if path else None
    except OSError:
        return None


# Начало эфира кладётся рядом с записью под этим хвостом (live/joining.py).
PART00 = " - part00"
# Отрезок начала эфира длиннее стыка на столько (live/beginning.py: OVERLAP): по нему
# — стык у склеенных до того, как он стал запоминаться
BEGINNING_OVERLAP = 15
# Начало эфира не скачалось — через сколько секунд пробовать снова: сбои там
# обычно временные (YouTube оборвал соединение, площадка ответила 5xx)
BEGINNING_RETRIES = (60, 180, 300)


def _follow_join(job):
    """
    Начало эфира склеили с записью и удалили, пока загрузка этого не знала
    (склейка в 0.3.2 и раньше): загрузка — про склеенную запись.
    """
    file = job.get("file") or ""
    stem = os.path.splitext(file)[0]
    if job.get("state") != DONE or job.get("joined") or not stem.endswith(PART00) or os.path.exists(file):
        return
    main = next((stem[:-len(PART00)] + extension for extension in util.RECORDING_EXTENSIONS
                 if os.path.isfile(stem[:-len(PART00)] + extension)), None)
    if main:
        job.update(file=main, joined=True, chat_files=chat.existing(main))
        logging.info(tr("начало эфира склеено с записью — загрузка теперь о ней: {name}", name=os.path.basename(main)))


# Платформы, где имя канала не различает регистр: папка xqc, а не xQc.
_LOWERCASE_LABELS = ("twitch", "kick")


def _clock(seconds):
    """3723 -> '01h02m03s' для имени файла отрезка."""
    seconds = int(seconds)
    return "%02dh%02dm%02ds" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def _section_problem(section, duration):
    """Почему отрезок не скачать; пустая строка — скачать можно."""
    if not section or not duration:
        return ""
    if section[0] >= duration:
        seconds = int(duration)
        return tr("отрезок начинается после конца записи: она длится {length}",
                  length="%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60))
    return ""


def build_download_base(directory, label, title, moment, video_id, suffix=""):
    """
    Путь будущего файла без расширения: "канал - дата - заголовок [id]".
    Заголовок обрезается под лимит длины пути с запасом на расширение
    и служебные хвосты yt-dlp вроде ".f137.mp4.part".
    """
    stamp = moment.strftime("%Y-%m-%d") if moment else ""
    prefix = sanitize_filename(" - ".join(part for part in (label, stamp) if part)) or "video"
    tail = sanitize_filename(("[%s]" % video_id if video_id else "") + suffix)
    title = sanitize_filename(title or "")
    reserve = len(".f000.mp4.part-Frag0000.part")
    budget = (MAX_PATH_LEN - len(directory) - len(os.sep) - len(prefix) - len(" - ")
              - len(" ") - len(tail) - reserve)
    title = title[:budget].rstrip(". ") if budget > 0 else ""
    name = " - ".join(part for part in (prefix, title) if part)
    if tail:
        name = "%s %s" % (name, tail)
    return os.path.join(directory, name)


# Отрезок с начала записи качается своими запросами, а не ffmpeg — у всех площадок:
# ffmpeg качает HLS по куску и у записи идущего эфира Twitch и Kick шёл в десятки раз
# медленнее CDN (archive/hlssection.py); VK — ffmpeg ещё и не доверяет сертификату
# CDN; YouTube — рвёт долгие соединения ffmpeg (archive/dashsection.py)
OWN_SECTION_PLATFORMS = ("twitch", "kick", "vkvideolive", "youtube")


def beginning_reason(error):
    """
    Почему не скачалось начало эфира — коротко, для строки в «Загрузках»
    (подробности — в журнале), и стоит ли пробовать снова: доступ только
    подписчикам или закрытую запись повторы не изменят.
    """
    text = str(error or "").lower()
    lasting = (
        (("subscriber-only", "subscribers only", "only available to subscribers", "sub-only"),
         tr("запись эфира доступна только подписчикам канала")),
        (("members-only", "members only", "join this channel"), tr("запись эфира доступна только спонсорам канала")),
        (("private video", "video is private", "is private"), tr("запись эфира закрыта")),
        (("available in your country", "geo restrict", "geo-restrict", "geoblocked"),
         tr("запись эфира недоступна в твоей стране")),
        (("confirm your age", "age-restricted", "age restricted", "login required", "requires authentication",
          "sign in to"), tr("запись эфира доступна только после входа на площадку")),
    )
    for words, reason in lasting:
        if any(word in text for word in words):
            return reason, False
    passing = (
        (("timed out", "timeout", "connection", "wsae", "reset", "4294957242", "http error 5", "temporarily"),
         tr("площадка не ответила или оборвалась связь")),
        (("404", "not found", "does not exist", "unavailable"), tr("записи эфира на площадке пока нет")),
        (("ещё не дошла",), tr("запись эфира на площадке ещё догоняет эфир")),
    )
    for words, reason in passing:
        if any(word in text for word in words):
            return reason, True
    return tr("не скачалось — подробности в журнале"), True


class DownloadManager:
    """
    Загрузки по ссылкам: очередь, прогресс, пауза, отмена, продолжение.

    В шину уходят события "download" с полным снимком загрузки и
    "download_removed", когда загрузку убрали из списка.
    """

    def __init__(self, settings, bus=None, state_path=None, resume=True, ffmpeg=None, ytdlp_copy=None,
                 twitchdownloader=None):
        """
        resume — продолжать ли незаконченное сразу после запуска. Консольная
        команда передаёт False: она качает только то, что ей указали.
        ffmpeg — FFmpegLocator, общий с записью эфиров; ytdlp_copy — YtdlpLocator:
        какой yt-dlp запускать; twitchdownloader — TwitchDownloaderLocator для чата.
        """
        self._settings = settings
        self._bus = bus
        self._ffmpeg = ffmpeg or FFmpegLocator(settings, own_dir_for(settings))
        self._ytdlp = ytdlp_copy or ytdlp_update.YtdlpLocator(ytdlp_update.own_dir_for(settings))
        self._twitchdownloader = twitchdownloader or TwitchDownloaderLocator(
            twitchdownloader_dir(settings))
        self._state_path = state_path or os.path.join(
            os.path.dirname(os.path.abspath(settings.path)), "downloads.json")
        self._lock = threading.RLock()
        self._wakeup = threading.Condition(self._lock)
        self._jobs = []
        self._running = {}       # id -> {"process": Popen, "stop": None|pause|cancel|shutdown}
        self._last_event = {}    # id -> когда слали прогресс
        self._stopping = False
        self._scheduler = None
        self._load(resume)

    # ------------------------------------------------------------ запуск и остановка

    def start(self):
        with self._lock:
            if self._scheduler is not None:
                return
            self._scheduler = threading.Thread(target=self._schedule, name="downloads",
                                               daemon=True)
            self._scheduler.start()

    def shutdown(self, timeout=30):
        """
        Останавливает идущие загрузки, не удаляя скачанное: со следующего
        запуска они продолжатся с того же места.
        """
        with self._lock:
            self._stopping = True
            running = list(self._running.items())
            for job_id, runtime in running:
                runtime["stop"] = "shutdown"
            self._wakeup.notify_all()
        for job_id, runtime in running:
            if runtime.get("process") is not None:
                util.kill_process_tree(runtime["process"])
        deadline = time.monotonic() + timeout
        for job_id, runtime in running:
            thread = runtime.get("thread")
            if thread is not None:
                util.join_interruptibly(thread, max(0, deadline - time.monotonic()))
        if self._scheduler is not None:
            util.join_interruptibly(self._scheduler, 5)
        self._save()

    def preflight(self):
        """Чего не хватает для загрузок. Пустой список — всё на месте."""
        problems = []
        # yt-dlp запускается тем же Python, что и программа, или обновлённый из архива
        if importlib.util.find_spec("yt_dlp") is None and self._ytdlp.active_copy() is None:
            problems.append(tr("yt-dlp не установлен: python -m pip install yt-dlp"))
        directory = self.downloads_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            problems.append(tr("папка для загрузок недоступна: {path} ({error})",
                               path=directory, error=error.strerror or error))
        return problems

    def downloads_dir(self):
        return (self._settings["downloads_path"]
                or os.path.join(self._settings["root_path"], "downloads"))

    # ------------------------------------------------------------ что по ссылке

    def inspect(self, url, limit=None, start=1):
        """Сведения о ссылке. Блокирует до ответа yt-dlp — звать из фонового потока."""
        auth_args, cookies = self._auth_args()
        try:
            return ytdlp.inspect(url, limit=limit, start=start, extra_args=auth_args,
                                 env=self._ytdlp.environment())
        finally:
            if cookies:
                util.remove_quietly(cookies)

    def youtube_channels(self, query, limit=5):
        """Каналы YouTube по названию — тем yt-dlp, которым качаем."""
        return ytdlp.youtube_channel_search(query, limit, env=self._ytdlp.environment())

    def ytdlp_json(self, args, timeout=ytdlp.INSPECT_TIMEOUT):
        """Ответ yt-dlp как JSON — тем yt-dlp, которым качаем (сведения о канале YouTube)."""
        return ytdlp.run_json(args, timeout, env=self._ytdlp.environment())

    def list_channel(self, platform, channel, kind="vods", limit=30, start=1, period="all"):
        """
        Записи, хайлайты или клипы канала: страница по limit штук.
        Twitch время от времени отвечает пустой страницей там, где записи есть
        (примерно раз из шести), — пустую страницу переспрашиваем.
        """
        url = ytdlp.listing_url(platform, channel, kind, period)
        # списки, которых yt-dlp не знает (VK Video Live), площадка берёт сама
        lister = sites.own_lister(platform)
        if lister is not None:
            return lister(channel, kind, limit, start, period, inspect=self.inspect)
        for attempt in range(EMPTY_PAGE_ATTEMPTS):
            try:
                result = self.inspect(url, limit=limit, start=start)
            except ytdlp.YtdlpError as error:
                # у канала YouTube просто нет такой вкладки (трансляций, Shorts) — список пуст
                if not re.search(r"does not have an? \w+ tab", str(error)):
                    raise
                return {"type": "playlist", "title": "", "url": url, "platform": platform, "entries": []}
            if result.get("entries"):
                break
        for entry in result.get("entries", []):
            # в списке записей Twitch канал не указан — он тот, чей список
            entry["channel"] = entry["channel"] or channel
            # и папка — того канала, чья страница: как у записей его эфиров
            entry["folder"] = channel
        sites.enrich_listing(platform, channel, kind, result)
        return result

    def find_vod(self, platform, channel, moment, title=""):
        """
        Запись эфира на площадке, которая шла в момент moment (секунды): сведения
        о ней ({url, timestamp, duration, …}) или None. Берётся первая страница
        вкладки записей; где у списка нет времени или длины, видео спрашиваются
        по одному (первые несколько). Не нашлось по времени — по названию.
        """
        entries = self.list_channel(platform, channel, "vods", limit=VOD_SEARCH_PAGE).get("entries") or []
        asked = 0
        for entry in entries:
            if (not entry.get("timestamp") or not entry.get("duration")) and asked < VOD_SEARCH_INSPECT:
                asked += 1
                try:
                    full = self.inspect(entry["url"])
                except ytdlp.YtdlpError:
                    continue
                for key in ("timestamp", "duration", "title", "id", "kind", "live_status"):
                    entry[key] = entry.get(key) or full.get(key)
        covering = [entry for entry in entries if entry.get("timestamp") and entry.get("duration")
                    and entry["timestamp"] - VOD_SLACK <= moment <= entry["timestamp"] + entry["duration"] + VOD_SLACK]
        if covering:
            return min(covering, key=lambda entry: abs(moment - entry["timestamp"]))
        # время не сошлось (у YouTube в списке бывает время выкладки) — то же название эфира
        wanted = _plain_title(title)
        named = [entry for entry in entries if wanted and _plain_title(entry.get("title")) == wanted]
        return min(named, key=lambda entry: abs(moment - (entry.get("timestamp") or moment))) if named else None

    def chat_for_recording(self, platform, channel, parts, moment, from_start=False, chat_mode=None):
        """
        Чат своей записи эфира, если при записи его не было: parts —
        [(путь части, её длина)] по порядку, moment — когда началась запись
        (время из имени файла), from_start — первая часть начинается с начала
        эфира (склеена с ним). Следующие части идут за первой подряд. Для каждой
        части без чата — загрузка «только чат» отрезка записи эфира на площадке;
        чат ложится рядом с частью, со временем от её начала. Возвращает
        (сколько поставлено, ссылка на запись эфира).
        """
        if not parts:
            return 0, None
        from ..library import parse_recording_name
        title = parse_recording_name(os.path.splitext(os.path.basename(parts[0][0]))[0]).get("title") or ""
        vod = self.find_vod(platform, channel, moment, title)
        if vod is None:
            raise ValueError(tr("запись этого эфира на площадке не нашлась — без неё чат не взять"))
        added = 0
        begin = 0.0 if from_start else max(0.0, moment - (vod.get("timestamp") or moment))
        for path, length in parts:
            start, begin = begin, begin + (length or 0)
            if chat.existing(path).get("json") or not length:
                continue
            begin_here = start
            meta = {key: vod.get(key) for key in ("id", "title", "channel", "folder", "platform", "kind",
                                                  "duration", "thumbnail", "timestamp", "live_status")}
            meta["platform"] = meta["platform"] or platform
            self.add(vod["url"], quality=CHAT_ONLY, section=(begin_here, begin_here + length), meta=meta,
                     chat_mode=chat_mode or chat.TEXT, target=os.path.splitext(path)[0], chat_shift=begin_here)
            added += 1
        return added, vod["url"]

    def add_archive_chat(self, part, channel, start, end, title="", seam=None, chat_mode=None):
        """
        Чат части своей записи из архива чатов: с start до end (секунды эпохи).
        seam — часть склеена с началом эфира и чат прямого эфира у неё есть:
        чат из архива встаёт только до стыка. Та же часть второй раз не ставится,
        пока её загрузка не закончена. Возвращает id загрузки.
        """
        from . import chatlogs
        stem = os.path.splitext(part)[0]
        with self._lock:
            for job in self._jobs:
                if job["quality"] == CHAT_ARCHIVE and job.get("fixed_target") == stem and job["state"] in UNFINISHED:
                    if job["state"] in (PAUSED, FAILED):
                        self._requeue(job)
                    existing = job["id"]
                    break
            else:
                existing = None
                snapshot = self._new_job(chatlogs.BASE + "/channel/" + channel, CHAT_ARCHIVE, None,
                                         {"title": title, "channel": channel, "platform": "twitch", "kind": "vod",
                                          "timestamp": start},
                                         chat_mode if chat_mode in (chat.TEXT, chat.RENDER) else chat.TEXT, stem)
                job = self._job(snapshot["id"])
                job["archive"] = {"channel": channel, "start": float(start), "end": float(end),
                                  "seam": float(seam) if seam else None, "video": part}
                job["video_id"] = "archive"      # сведения у yt-dlp не спрашиваются: их нет
                self._save()
                snapshot = copy.deepcopy(job)
        if existing is not None:
            self._emit(existing)
            return existing
        logging.info(tr("загрузка добавлена: {name}", name=tr("чат из архива чатов: {name}",
                                                                name=os.path.basename(part))))
        if self._bus is not None:
            self._bus.emit("download", job=snapshot)
        return snapshot["id"]

    def known_videos(self):
        """
        Что с площадок уже есть: id видео и ссылки из очереди (кроме отменённых)
        и id из имён файлов в папке загрузок — «[id]» в конце имени.
        """
        from ..library import EDIT_MARK, parse_download_name, walk_media
        ids, urls = set(), set()
        with self._lock:
            for job in self._jobs:
                if job["state"] == CANCELLED:
                    continue
                urls.add(job["url"])
                if job["video_id"] and not job["section"]:
                    ids.add((job["platform"], str(job["video_id"])))
        for platform, _label, path in walk_media(self.downloads_dir()):
            stem = os.path.splitext(os.path.basename(path))[0]
            video_id = parse_download_name(stem).get("video_id")
            # отрезок видео («[id] (1:00-2:00)») и клип, вырезанный в окне, — ещё не всё видео
            if video_id and "(" not in stem[stem.rfind("]"):] and not EDIT_MARK.search(stem):
                ids.add((platform, video_id))
        return ids, urls

    def download_all(self, platform, channel, kind="vods", period="all", quality=None, chat_mode=None):
        """
        Всё с вкладки канала: список целиком, страница за страницей, и каждое
        видео, которого ещё нет, — в очередь. Сверка по id видео с загрузками
        и файлами в папке загрузок: повторно докачивается только новое.
        Возвращает {"added", "known", "total"}.
        """
        entries = []
        start = 1
        for _ in range(ALL_PAGES):
            page = self.list_channel(platform, channel, kind, limit=ALL_PAGE_SIZE, start=start, period=period)
            batch = page.get("entries") or []
            entries.extend(batch)
            if len(batch) < ALL_PAGE_SIZE:
                break
            start += ALL_PAGE_SIZE
        chat_only = quality == CHAT_ONLY
        if chat_only:
            # «только чат»: уже есть — только такие же загрузки чата, скачанное видео не в счёт
            with self._lock:
                ids, urls = set(), {job["url"] for job in self._jobs if job["quality"] == CHAT_ONLY}
        else:
            ids, urls = self.known_videos()
        added = known = 0
        for entry in entries:
            if not entry.get("url"):
                continue
            video_id = str(entry.get("id") or "")
            if entry["url"] in urls or (video_id and (entry.get("platform") or platform, video_id) in ids):
                known += 1
                continue
            meta = {key: entry.get(key) for key in ("id", "title", "channel", "folder", "platform", "kind",
                                                    "duration", "thumbnail", "timestamp", "live_status")}
            if chat_only and not chat.supported(meta["platform"] or platform, meta["kind"] or "",
                                                meta["live_status"] or ""):
                continue                   # у этого видео чата нет — и качать нечего
            self.add(entry["url"], quality=quality, meta=meta, chat_mode=chat_mode)
            urls.add(entry["url"])
            added += 1
        logging.info(tr("{channel}: всё с вкладки «{kind}» — в очереди {added}, уже было {known}",
                        channel=channel, kind=kind, added=added, known=known))
        return {"added": added, "known": known, "total": len(entries)}

    # ------------------------------------------------------------ управление

    def add(self, url, quality=None, section=None, meta=None, force=False, chat_mode=None, target=None,
            chat_shift=None):
        """
        Ставит ссылку в очередь. Возвращает id загрузки.

        quality — best, worst, audio, высота или id формата; по умолчанию из
        настроек. section — (начало, конец) в секундах. meta — уже известные
        сведения (describe_entry): с ними не нужно заново спрашивать yt-dlp.
        chat_mode — чат: none, text или render (chat.MODES); по умолчанию
        из настроек; у загрузок без чата (chat.supported) не скачивается.
        target — свой путь файла без расширения (начало эфира кладётся рядом
        с записью эфира), иначе — папка загрузок.
        Та же ссылка с тем же качеством второй раз не добавляется: незаконченная
        продолжается, скачанная возвращается как есть, пока жив её файл, — разве
        что к ней теперь попросили чат.
        """
        url = url.strip()
        quality = (quality or self._settings["download_quality"] or "best").strip()
        chat_mode = chat_mode if chat_mode in chat.MODES else self._settings["chat_mode"]
        if quality == CHAT_ONLY and chat_mode == chat.NONE:
            chat_mode = chat.TEXT          # «только чат» без чата — пустая загрузка
        if quality == CHAT_ONLY and meta and meta.get("platform") \
                and not chat.supported(meta["platform"], meta.get("kind") or "", meta.get("live_status") or ""):
            raise ValueError(tr("у этого видео чата не бывает"))
        section = [float(section[0]), float(section[1])] if section else None
        if section and meta:
            problem = _section_problem(section, meta.get("duration"))
            if problem:
                raise ValueError(problem)
        existing = None
        with self._lock:
            for job in self._jobs:
                if (job["url"], job["quality"], job["section"]) != (url, quality, section):
                    continue
                if job["state"] in UNFINISHED:
                    if job["state"] in (PAUSED, FAILED):
                        self._requeue(job)
                    existing = job["id"]
                    break
                if job["state"] == DONE and not force and job["file"] \
                        and os.path.exists(job["file"]):
                    if chat_mode != chat.NONE and not job.get("chat_files") \
                            and self._chat_supported(job):
                        # видео уже есть — докачать к нему чат
                        job["chat"] = chat_mode
                        self._chat_only(job)
                    existing = job["id"]
                    break
            if existing is None:
                snapshot = self._new_job(url, quality, section, meta, chat_mode, target, chat_shift)
        if existing is not None:
            self._emit(existing)
            return existing
        logging.info(tr("загрузка добавлена: {name}", name=snapshot["title"] or url))
        if self._bus is not None:
            self._bus.emit("download", job=snapshot)
        return snapshot["id"]

    def _new_job(self, url, quality, section, meta, chat_mode, target=None, chat_shift=None):
        """Новая загрузка в очереди. Возвращает её снимок для окна."""
        with self._lock:
            job = {
                # «только чат» к своей записи: на столько сдвинуть время чата
                # (отрезок записи эфира на площадке начинается позже её начала)
                "chat_shift": chat_shift or 0,
                "id": uuid.uuid4().hex[:12],
                "url": url,
                "quality": quality,
                "section": section,
                "state": QUEUED,
                "title": "", "channel": "", "platform": "", "kind": "",
                "folder": "",       # идентификатор канала для папки (ytdlp.channel_folder)
                "video_id": "", "duration": None, "thumbnail": "", "timestamp": None,
                "live_status": "",
                "target": "",       # путь без расширения, выбирается при первом запуске
                # заданный путь (начало эфира — рядом с записью): переживает отмену
                "fixed_target": target or "",
                "file": "",         # готовый файл
                "error": "",
                "progress": None,
                "chat": chat_mode,
                "chat_files": {},    # готовые файлы чата: json, html, video
                "chat_error": "",
                "chat_only": False,  # видео уже скачано, осталось сделать чат
                "chat_progress": None,
                "created": time.time(), "started": None, "finished": None,
            }
            if meta:
                self._apply_meta(job, meta)
            self._jobs.append(job)
            self._save()
            # снимок до пробуждения очереди: иначе она успевает начать загрузку,
            # и окно ни разу не увидит её в очереди
            snapshot = copy.deepcopy(job)
            self._wakeup.notify_all()
        return snapshot

    def pause(self, job_id):
        """Останавливает загрузку, сохраняя скачанное."""
        self._stop_job(job_id, "pause")

    def cancel(self, job_id):
        """Останавливает загрузку и удаляет недокачанное."""
        self._stop_job(job_id, "cancel")

    def fetch_chat(self, job_id, mode=None):
        """Чат к уже скачанной записи или клипу: видео заново не качается."""
        mode = mode if mode in chat.MODES and mode != chat.NONE else None
        with self._lock:
            job = self._job(job_id)
            if job["state"] != DONE or not job["file"] or not os.path.exists(job["file"]):
                raise ValueError(tr("чат делается к уже скачанному видео"))
            if not self._chat_supported(job):
                raise ValueError(tr("у этого видео чата не бывает"))
            if mode is None and job.get("chat", chat.NONE) == chat.NONE:
                mode = self._settings["chat_mode"] if self._settings["chat_mode"] != chat.NONE else chat.TEXT
            if mode:
                job["chat"] = mode
            self._chat_only(job)
        self._emit(job_id)

    def _chat_only(self, job):
        job.update(chat_only=True, chat_error="", chat_progress=None)
        self._requeue(job)

    def resume(self, job_id):
        """Возвращает остановленную, упавшую или отменённую загрузку в очередь."""
        with self._lock:
            job = self._job(job_id)
            if job["state"] not in (PAUSED, FAILED, CANCELLED):
                return
            self._requeue(job)
        self._emit(job_id)

    def remove(self, job_id):
        """
        Убирает загрузку из списка. Скачанный файл остаётся на диске,
        недокачанное удаляется.
        """
        with self._lock:
            job = self._job(job_id)
            runtime = self._running.get(job_id)
            if runtime is not None:
                # идущую загрузку уберёт из списка её же поток, когда погасит yt-dlp
                runtime["remove"] = True
            else:
                if job["state"] != DONE:
                    self._remove_partials(job)
                self._drop(job_id)
        if runtime is not None:
            self._stop_job(job_id, "cancel")

    def _drop(self, job_id):
        with self._lock:
            self._jobs = [other for other in self._jobs if other["id"] != job_id]
            self._save()
        if self._bus is not None:
            self._bus.emit("download_removed", id=job_id)

    def clear_finished(self):
        """Убирает из списка скачанное и отменённое."""
        with self._lock:
            gone = [job["id"] for job in self._jobs if job["state"] in (DONE, CANCELLED)]
            self._jobs = [job for job in self._jobs if job["id"] not in gone]
            self._save()
        if self._bus is not None:
            for job_id in gone:
                self._bus.emit("download_removed", id=job_id)

    # ------------------------------------------------------------ состояние

    def snapshot(self):
        with self._lock:
            return {"jobs": [self._view(job) for job in self._jobs],
                    "active": len(self._running),
                    "downloads_dir": self.downloads_dir()}

    def job(self, job_id):
        with self._lock:
            return self._view(self._job(job_id))

    @staticmethod
    def _view(job):
        """
        Копия загрузки для показа. Отрезок yt-dlp отдаёт качать ffmpeg, а тот
        прогресса не сообщает — тогда показываем хотя бы, сколько уже на диске.
        """
        job = copy.deepcopy(job)
        progress = job["progress"] or {}
        if job["state"] == DOWNLOADING and job["target"] and not progress.get("downloaded"):
            size = 0
            for path in glob.glob(glob.escape(job["target"]) + ".*"):
                try:
                    size += os.path.getsize(path)
                except OSError:
                    pass
            if size:
                job["progress"] = dict(progress, downloaded=size)
        if job.get("joined") and "beginning_chat" not in job:
            # склеено раньше, чем это запоминалось: чат начала был, если он скачался без ошибки
            job["beginning_chat"] = job.get("chat", chat.NONE) != chat.NONE and not job.get("chat_error")
        return job

    # ------------------------------------------------------------ внутреннее

    def _job(self, job_id):
        for job in self._jobs:
            if job["id"] == job_id:
                return job
        raise KeyError(tr("загрузки {id} нет в списке", id=job_id))

    def _emit(self, job_id, progress_only=False):
        if self._bus is None:
            return
        now = time.monotonic()
        with self._lock:
            if progress_only and now - self._last_event.get(job_id, 0) < PROGRESS_EVENT_INTERVAL:
                return
            self._last_event[job_id] = now
            try:
                snapshot = copy.deepcopy(self._job(job_id))
            except KeyError:
                return
        self._bus.emit("download", job=snapshot)

    def mark_joined(self, head, main, seam=None, had_chat=None):
        """
        Начало эфира (head) склеено с записью (main) и удалено: загрузка
        теперь показывает и открывает склеенную запись. seam — стык: по нему
        встанет чат начала, если его докачают потом; had_chat — был ли он.
        """
        key = os.path.normcase(os.path.abspath(head))
        with self._lock:
            found = [job["id"] for job in self._jobs
                     if job.get("file") and os.path.normcase(os.path.abspath(job["file"])) == key]
            for job_id in found:
                job = self._job(job_id)
                if had_chat is None:
                    had_chat = bool((job.get("chat_files") or {}).get("json"))
                job.update(file=main, joined=True, chat_files=chat.existing(main), beginning_chat=bool(had_chat))
                if seam is not None:
                    job["joined_seam"] = float(seam)
            if found:
                self._save()
        for job_id in found:
            self._emit(job_id)

    def _update(self, job_id, **changes):
        with self._lock:
            job = self._job(job_id)
            job.update(changes)
            if "state" in changes:
                self._save()
        self._emit(job_id)

    def _requeue(self, job):
        # target остаётся: yt-dlp продолжит недокачанное с того же места.
        # «Повторить» руками — сразу и с новым запасом автоповторов
        job.update(state=QUEUED, error="", reason="", progress=None, finished=None, retry_at=None, auto_retries=0)
        self._save()
        self._wakeup.notify_all()

    def _stop_job(self, job_id, reason):
        with self._lock:
            job = self._job(job_id)
            runtime = self._running.get(job_id)
            if runtime is not None:
                runtime["stop"] = reason
                process = runtime.get("process")
            else:
                process = None
                if job.get("chat_only") and job["state"] in (QUEUED, PAUSED):
                    # ждал только чат: видео уже скачано и остаётся готовым
                    if reason == "cancel":
                        job.update(state=DONE, chat_only=False, chat_error=tr("чат отменён"))
                    else:
                        job["state"] = PAUSED
                    self._save()
                elif job["state"] in (QUEUED, PAUSED, FAILED):
                    if reason == "cancel":
                        self._remove_partials(job)
                        job.update(state=CANCELLED, target="", progress=None)
                    else:
                        job["state"] = PAUSED
                    self._save()
        if process is not None:
            util.kill_process_tree(process)
        self._emit(job_id)

    @staticmethod
    def _apply_meta(job, meta):
        job["title"] = meta.get("title") or job["title"]
        job["channel"] = meta.get("channel") or job["channel"]
        job["folder"] = meta.get("folder") or job.get("folder") or ""
        job["platform"] = meta.get("platform") or job["platform"]
        job["kind"] = meta.get("kind") or job["kind"]
        job["video_id"] = meta.get("id") or job["video_id"]
        job["duration"] = meta.get("duration") or job["duration"]
        job["thumbnail"] = meta.get("thumbnail") or job["thumbnail"]
        job["timestamp"] = meta.get("timestamp") or job["timestamp"]
        job["live_status"] = meta.get("live_status") or job.get("live_status") or ""

    @staticmethod
    def _chat_supported(job):
        return chat.supported(job["platform"], job["kind"], job.get("live_status") or "")

    def _load(self, resume):
        try:
            with open(self._state_path, encoding="utf-8") as source:
                jobs = json.load(source)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as error:
            logging.error(tr("{path} не читается ({error}), начинаю с пустой очереди",
                             path=self._state_path, error=error))
            return
        for job in jobs if isinstance(jobs, list) else []:
            if not isinstance(job, dict) or "id" not in job or "url" not in job:
                continue
            # программу закрыли посреди загрузки — продолжим с того же места
            if job.get("state") in (QUEUED, DOWNLOADING, PROCESSING):
                job["state"] = QUEUED if resume else PAUSED
            job["progress"] = job["chat_progress"] = None
            # размер готового файла — для строки «Готово · 3,9 МБ»; у загрузок прошлых версий его нет
            if job.get("state") == DONE and not job.get("size"):
                job["size"] = _file_size(job.get("file"))
            # загрузки из прошлых версий — без чата
            job.setdefault("chat", chat.NONE)
            job.setdefault("chat_files", {})
            job.setdefault("chat_error", "")
            job.setdefault("chat_only", False)
            _follow_join(job)
            self._jobs.append(job)

    def _save(self):
        with self._lock:
            data = [{key: value for key, value in job.items()
                     if key not in ("progress", "chat_progress")}
                    for job in self._jobs]
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self._state_path)), exist_ok=True)
                temporary = self._state_path + ".tmp"
                with open(temporary, "w", encoding="utf-8") as target:
                    json.dump(data, target, ensure_ascii=False, indent=1)
                os.replace(temporary, self._state_path)
            except OSError as error:
                logging.error(tr("очередь загрузок не сохранилась: {error}", error=error))

    def _schedule(self):
        """Запускает ждущие загрузки, пока есть свободные места."""
        with self._lock:
            while not self._stopping:
                limit = max(1, self._settings["max_downloads"])
                now = time.time()
                waiting = [job for job in self._jobs if job["state"] == QUEUED
                           and job["id"] not in self._running and (job.get("retry_at") or 0) <= now]
                if waiting and len(self._running) < limit:
                    job = waiting[0]
                    runtime = {"process": None, "stop": None}
                    thread = threading.Thread(target=self._work, args=(job["id"], runtime),
                                              name="download-" + job["id"], daemon=True)
                    runtime["thread"] = thread
                    self._running[job["id"]] = runtime
                    job.update(state=DOWNLOADING, started=time.time(), error="")
                    thread.start()
                    continue
                self._wakeup.wait(1)

    def _work(self, job_id, runtime):
        try:
            self._download(job_id, runtime)
        except Exception as error:
            logging.error(tr("загрузка {id}: сбой", id=job_id) + "\n"
                          + traceback.format_exc().rstrip())
            self._finish(job_id, runtime, FAILED, error=tr("сбой программы: {error}", error=error))
        finally:
            with self._lock:
                self._running.pop(job_id, None)
                self._wakeup.notify_all()

    def _download(self, job_id, runtime):
        job = self.job(job_id)
        self._emit(job_id)

        if job["quality"] == CHAT_ARCHIVE:
            return self._chat_from_archive(job_id, runtime, job)

        if job.get("chat_only"):
            if job["file"] and os.path.exists(job["file"]):
                return self._finish_with_chat(job_id, runtime, job["file"])
            # видео за это время удалили — скачается заново, а с ним и чат
            self._update(job_id, chat_only=False, file="")

        if not job["video_id"]:
            # сведения нужны для имени файла; заодно видно, что ссылка жива
            try:
                info = self.inspect(job["url"])
            except ytdlp.YtdlpError as error:
                return self._finish(job_id, runtime, FAILED, error=str(error))
            if info["type"] == "playlist":
                return self._finish(job_id, runtime, FAILED,
                                    error=tr("по ссылке список из {count} видео — добавь "
                                             "нужные по отдельности",
                                             count=len(info["entries"])))
            with self._lock:
                self._apply_meta(self._job(job_id), info)
            job = self.job(job_id)
            self._emit(job_id)

        if runtime["stop"]:
            return self._finish(job_id, runtime, None)

        # yt-dlp на такой отрезок не ругается, а молча делает пустышку
        problem = _section_problem(job["section"], job["duration"])
        if problem:
            return self._finish(job_id, runtime, FAILED, error=problem)

        if not job["target"]:
            target = job.get("fixed_target") or self._choose_target(job)
            self._update(job_id, target=target)
            job["target"] = target
        os.makedirs(os.path.dirname(job["target"]), exist_ok=True)

        if job["quality"] == CHAT_ONLY:
            return self._chat_without_video(job_id, runtime, job)

        if job["platform"] in OWN_SECTION_PLATFORMS and job["section"] and not job["section"][0]:
            done = self._own_section(job_id, runtime, job)
            if done:
                return None

        auth_args, cookies = self._auth_args()
        command = ytdlp.download_command(
            job["url"], job["target"], job["quality"], job["section"],
            self._settings["download_fragments"], self._ffmpeg.directory(),
            auth_args + list(self._settings["ytdlp_extra_args"]))
        logging.info(tr("скачиваю: {name}", name=job["title"] or job["url"]))
        try:
            returncode, produced, error, tail = self._run_ytdlp(job_id, runtime, command)
        finally:
            if cookies:
                util.remove_quietly(cookies)

        if runtime["stop"]:
            return self._finish(job_id, runtime, None)
        if returncode == 0 and produced and os.path.exists(produced):
            return self._finish_with_chat(job_id, runtime, produced)
        if not error:
            error = (tail[-1] if tail else "") or tr("yt-dlp завершился с кодом {code}",
                                                    code=returncode)
        return self._finish(job_id, runtime, FAILED, error=error)

    def _own_section(self, job_id, runtime, job):
        """
        Отрезок с начала записи — своими запросами: HLS одним потоком (VK)
        или видео и звук mp4 с оглавлением (YouTube). Возвращает True, если
        загрузка этим и кончилась; False — формат не тот, качать как обычно.
        """
        try:
            info = self.ytdlp_json(["-J", "--no-warnings", "--color", "never",
                                    *ytdlp.format_args(job["quality"]), "--", job["url"]])
        except ytdlp.YtdlpError as error:
            self._finish(job_id, runtime, FAILED, error=str(error))
            return True
        chosen = (info or {}).get("requested_formats") or [info or {}]
        protocols = [str(item.get("protocol") or "") for item in chosen]
        hls = len(chosen) == 1 and protocols[0].startswith("m3u8")
        dash = all(protocol == "https" for protocol in protocols) \
            and all(item.get("ext") in ("mp4", "m4a") for item in chosen)
        ffmpeg = self._ffmpeg.path()
        if not all(item.get("url") for item in chosen) or not (hls or (dash and ffmpeg)):
            return False
        logging.info(tr("скачиваю: {name}", name=job["title"] or job["url"]))

        # скорость — сглаженная за последние секунды, как у yt-dlp; сколько всего — по доле
        meter = {"time": time.monotonic(), "bytes": 0, "speed": None}

        def progress(fraction, written):
            now = time.monotonic()
            if now - meter["time"] >= 1:
                rate = (written - meter["bytes"]) / (now - meter["time"])
                meter["speed"] = rate if meter["speed"] is None else meter["speed"] * 0.7 + rate * 0.3
                meter.update(time=now, bytes=written)
            speed, total = meter["speed"], (written / fraction if fraction else None)
            with self._lock:
                self._job(job_id)["progress"] = {
                    "percent": fraction * 100.0, "downloaded": written, "total": total, "speed": speed,
                    "eta": (total - written) / speed if speed and total else None}
            self._emit(job_id, progress_only=True)

        stopped = lambda: bool(runtime["stop"])     # noqa: E731
        try:
            if hls:
                produced = hlssection.download(chosen[0]["url"], chosen[0].get("http_headers"), job["section"][1],
                                               job["target"], progress, stopped=stopped,
                                               workers=self._settings["download_fragments"])
                if ffmpeg:
                    produced = hlssection.from_zero(ffmpeg, produced)
            else:
                produced = dashsection.download([(item["url"], item.get("http_headers")) for item in chosen],
                                                job["section"][1], job["target"], ffmpeg, progress, stopped=stopped)
        except hlssection.PlaylistError as error:
            # плейлист не взять своими запросами — до кусков не дошло: тот же отрезок через ffmpeg
            logging.info(tr("{name}: {error} — качаю через ffmpeg", name=job["title"] or job["url"], error=error))
            return False
        except dashsection.NoIndex as error:
            # оглавления нет — до скачивания дело не дошло: тот же отрезок обычным путём
            logging.info(tr("{name}: {error} — качаю через ffmpeg", name=job["title"] or job["url"], error=error))
            return False
        except (hlssection.HlsSectionError, dashsection.DashSectionError) as error:
            self._finish(job_id, runtime, None if runtime["stop"] else FAILED, error=str(error))
            return True
        self._finish_with_chat(job_id, runtime, produced)
        return True

    def _chat_without_video(self, job_id, runtime, job):
        """
        «Только чат»: чат записи эфира или клипа без видео — .chat.json и .chat.html
        (и видео чата, если выбрано) в папке загрузок. Файл загрузки — HTML чата.
        """
        if not self._chat_supported(job):
            return self._finish(job_id, runtime, FAILED, error=tr("у этого видео чата не бывает"))
        if job["chat"] == chat.NONE:
            job["chat"] = chat.TEXT
            self._update(job_id, chat=chat.TEXT)
        # видео нет: имена файлов чата — от того, каким было бы видео
        named = job["target"] + ".mp4"
        # чат сдвигается — HTML и видео делаются после сдвига, а не дважды
        result, interrupted = self._fetch_chat(job_id, runtime, job, named, json_only=bool(job.get("chat_shift")))
        if runtime.get("remove"):
            self._drop(job_id)
            return None
        if interrupted:
            # пауза или выход — чат скачается заново со следующего раза; отмена — отменена
            return self._finish(job_id, runtime, None, chat_progress=None)
        files = result.get("chat_files") or {}
        if not files.get("json"):
            return self._finish(job_id, runtime, FAILED, chat_progress=None,
                                error=result.get("chat_error") or tr("чат не скачался"))
        notes = [result.get("chat_error") or ""]
        if job.get("chat_shift"):
            # чат к своей записи: время — от начала записи, а не от начала эфира на площадке
            try:
                _shift_chat(files["json"], job["chat_shift"], (job["section"] or [0, 0])[1] - job["chat_shift"])
            except (OSError, ValueError) as error:
                return self._finish(job_id, runtime, FAILED, chat_progress=None,
                                    error=tr("чат не сдвинулся: {error}", error=error))
            notes.append(self._remake_chat_html(job_id, runtime, job, named))
            files = chat.existing(named)
        return self._finish(job_id, runtime, DONE, file=files.get("html") or files["json"], chat_files=files,
                            chat_error="; ".join(filter(None, notes)), chat_progress=None)

    def _remake_chat_html(self, job_id, runtime, job, named, embedded=True):
        """
        HTML (и видео) чата заново, по поправленному .chat.json — с прогрессом
        в строке загрузки. Возвращает примечание или "".
        """
        paths = chat.chat_paths(named)
        for key in ("html", "video"):
            util.remove_quietly(paths[key])
        cli = self._twitchdownloader.command()
        if not cli:
            return ""
        mode = job["chat"] if job["chat"] != chat.NONE else chat.TEXT
        ffmpeg = self._ffmpeg.path()
        if mode == chat.RENDER and not ffmpeg:
            mode = chat.TEXT
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(self._settings.path)), "cache", "twitchdownloader")
        size = (self._settings["chat_width"], self._settings["chat_height"], self._settings["chat_font_size"])
        progress, started, stopped = self._chat_watchers(job_id, runtime)
        try:
            chat.run(chat.finishing(cli, named, mode, ffmpeg, size, temp_dir, embedded=embedded),
                     progress, started, stopped)
        except chat.ChatStopped:
            return tr("HTML чата не сделан: {error}", error=tr("чат отменён"))
        except chat.ChatError as error:
            return tr("HTML чата не сделан: {error}", error=error)
        return ""

    def _beginning_chat_later(self, job_id, runtime, job):
        """
        Чат начала эфира, которое уже склеено с записью (file — сама запись):
        качается отдельно, рядом под именем начала, и встаёт в чат записи до
        стыка; чат прямого эфира в записи не трогается. Без этого чат начала
        лёг бы поверх чата записи.
        """
        from ..live.joining import join_chat
        main = job["file"]
        stem, _ = os.path.splitext(main)
        named = stem + PART00 + ".mp4"           # начала уже нет — имена файлов чата от него
        # HTML и видео — только общего чата: у чата начала они ушли бы в удаление
        result, interrupted = self._fetch_chat(job_id, runtime, job, named, json_only=True)
        head_chat, main_chat = chat.chat_paths(named), chat.chat_paths(main)
        changes = {"file": main, "chat_only": False, "chat_progress": None, "chat_files": chat.existing(main)}
        try:
            if interrupted:
                if runtime["stop"] in ("pause", "shutdown"):
                    return self._finish(job_id, runtime, None, **dict(changes, chat_only=True))
                return self._finish(job_id, runtime, DONE, **dict(changes, chat_error=tr("чат отменён")))
            if not os.path.isfile(head_chat["json"]):
                return self._finish(job_id, runtime, DONE, **dict(changes, chat_error=result.get("chat_error")
                                                                  or tr("чат не скачался")))
            # стык — с склейки; у склеенных раньше его нет: отрезок начала длиннее стыка на запас
            seam = job.get("joined_seam") or max(0.0, (job["section"] or [0, 0])[1] - BEGINNING_OVERLAP)
            length = None
            try:
                with open(main_chat["json"], encoding="utf-8") as source:
                    length = (json.load(source).get("video") or {}).get("length")
            except (OSError, ValueError):
                pass
            length = length or (seam + (self._probe_length(main) or 0))
            joined = main_chat["json"] + ".joined"
            join_chat(head_chat["json"], main_chat["json"], seam, length, joined, shift=0)
            os.replace(joined, main_chat["json"])
            # HTML и видео чата — заново, по общему чату; смайлы чата прямого эфира
            # ещё не встроены — TwitchDownloader докачает недостающие
            notes = [result.get("chat_error") or "",
                     self._remake_chat_html(job_id, runtime, job, main, embedded=False)]
            logging.info(tr("чат начала эфира вклеен в чат записи: {name}", name=os.path.basename(main)))
            return self._finish(job_id, runtime, DONE, **dict(changes, chat_files=chat.existing(main),
                                                              beginning_chat=True,
                                                              chat_error="; ".join(filter(None, notes))))
        finally:
            for path in head_chat.values():
                util.remove_quietly(path)

    def _chat_from_archive(self, job_id, runtime, job):
        """
        Чат части своей записи из архива чатов (add_archive_chat): сообщения
        за время части — в .chat.json рядом с ней, затем смайлы и HTML, как
        у чата прямого эфира. У склеенной с началом части — только до стыка,
        в её чат; чат прямого эфира в ней не трогается.
        """
        from . import chatlogs
        from ..live.joining import join_chat
        from ..live.livechat import convert
        archive = job.get("archive") or {}
        video = archive.get("video") or ""
        seam = archive.get("seam")
        if not video or not os.path.exists(video):
            return self._finish(job_id, runtime, FAILED, error=tr("записи, к которой чат, больше нет"))
        main_chat = chat.chat_paths(video)
        if seam is None and os.path.isfile(main_chat["json"]):
            return self._finish(job_id, runtime, FAILED, error=tr("чат у этой части уже есть"))
        self._update(job_id, state=PROCESSING, file=video, chat_error="",
                     chat_progress={"stage": "archive", "percent": 0})
        progress, _, stopped = self._chat_watchers(job_id, runtime)
        logging.info(tr("чат из архива чатов: {name}", name=os.path.basename(video)))
        # своё имя, чтобы не задеть чат части: у склеенной он уже есть
        stem = os.path.splitext(video)[0] + (PART00 if seam is not None else "")
        jsonl, target = stem + ".archive.chat.jsonl", stem + ".archive.chat.json"
        # скачанные сутки — общие для частей одной записи: второй раз не качаются
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(self._settings.path)), "cache", "chatlogs")
        try:
            chatlogs.fetch(archive["channel"], archive["start"], archive["end"], jsonl, cache_dir=cache_dir,
                           progress=lambda percent: progress("archive", percent), stopped=stopped)
        except chatlogs.ArchiveStopped:
            util.remove_quietly(jsonl)
            return self._finish(job_id, runtime, None, chat_progress=None)
        except (chatlogs.ArchiveError, OSError) as error:
            util.remove_quietly(jsonl)
            return self._finish(job_id, runtime, FAILED, chat_progress=None, error=str(error))
        if runtime["stop"]:
            util.remove_quietly(jsonl)
            return self._finish(job_id, runtime, None, chat_progress=None)
        length = archive["end"] - archive["start"]
        try:
            count = convert(jsonl, target, title=job["title"], channel=archive["channel"], length=length)
            if not count:
                return self._finish(job_id, runtime, FAILED, chat_progress=None,
                                    error=tr("в архиве чатов за время этой записи сообщений нет"))
            if seam is not None:
                # до стыка — из архива, после — чат прямого эфира, как был
                total = None
                try:
                    with open(main_chat["json"], encoding="utf-8") as source:
                        total = (json.load(source).get("video") or {}).get("length")
                except (OSError, ValueError):
                    pass
                joined = main_chat["json"] + ".joined"
                join_chat(target, main_chat["json"], seam, total or seam + (self._probe_length(video) or 0), joined,
                          shift=0)
                os.replace(joined, main_chat["json"])
            else:
                os.replace(target, main_chat["json"])
        except (OSError, ValueError) as error:
            return self._finish(job_id, runtime, FAILED, chat_progress=None,
                                error=tr("чат из архива не записан: {error}", error=error))
        finally:
            for path in (jsonl, target):
                util.remove_quietly(path)
        # смайлы 7TV, BTTV, FFZ и значки — внутрь .chat.json; потом HTML (и видео чата)
        notes = [self._embed_chat(job_id, runtime, main_chat["json"]),
                 self._remake_chat_html(job_id, runtime, job, video)]
        if seam is not None:
            # у загрузки начала эфира кнопка «Скачать чат начала» больше не нужна
            with self._lock:
                for other in self._jobs:
                    if other.get("joined") and other.get("file") \
                            and os.path.normcase(other["file"]) == os.path.normcase(video):
                        other["beginning_chat"] = True
                        other["chat_files"] = chat.existing(video)
                self._save()
        logging.info(tr("чат из архива чатов готов: {name} — сообщений: {count}",
                        name=os.path.basename(video), count=count))
        return self._finish(job_id, runtime, DONE, file=video, chat_files=chat.existing(video), chat_progress=None,
                            chat_error="; ".join(filter(None, notes)))

    def _embed_chat(self, job_id, runtime, json_path):
        """Смайлы и значки внутрь .chat.json (TwitchDownloader). Возвращает примечание или ""."""
        cli = self._twitchdownloader.command()
        if not cli:
            return tr("смайлы 7TV, BTTV и FFZ не встроены: нужен TwitchDownloader")
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(self._settings.path)), "cache", "twitchdownloader")
        step, temporary = chat.embedding(cli, json_path, temp_dir)
        progress, started, stopped = self._chat_watchers(job_id, runtime)
        try:
            chat.run([("embedding", step[1])], progress, started, stopped)
            os.replace(temporary, json_path)
            return ""
        except chat.ChatStopped:
            util.remove_quietly(temporary)
            return tr("смайлы не встроены: {error}", error=tr("чат отменён"))
        except (chat.ChatError, OSError) as error:
            util.remove_quietly(temporary)
            return tr("смайлы не встроены: {error}", error=error)

    def _probe_length(self, path):
        from ..library import probe_duration
        try:
            return probe_duration(self._ffmpeg.path(), path)
        except Exception:
            return None

    def _finish_with_chat(self, job_id, runtime, produced):
        """Видео скачано; если просили — ещё чат. Чат не удался — видео всё равно готово."""
        job = self.job(job_id)
        if job.get("joined"):
            return self._beginning_chat_later(job_id, runtime, job)
        changes = {"file": produced, "chat_only": False, "chat_progress": None}
        if job["chat"] != chat.NONE and self._chat_supported(job):
            result, interrupted = self._fetch_chat(job_id, runtime, job, produced)
            changes.update(result)
            if runtime.get("remove"):
                # убрать из списка попросили во время чата: скачанное остаётся на диске
                self._drop(job_id)
                return
            if interrupted and runtime["stop"] in ("pause", "shutdown"):
                # чат продолжится после паузы или со следующего запуска, видео — уже есть
                return self._finish(job_id, runtime, None, **dict(changes, chat_only=True))
            if interrupted:
                changes["chat_error"] = tr("чат отменён")
        return self._finish(job_id, runtime, DONE, **changes)

    def _chat_watchers(self, job_id, runtime):
        """(прогресс, запущен процесс, остановлено ли) — для chat.run в строке загрузки."""
        def progress(stage, percent):
            with self._lock:
                self._job(job_id)["chat_progress"] = {"stage": stage, "percent": percent}
            self._emit(job_id, progress_only=percent not in (None, 100))

        def started(process):
            with self._lock:
                runtime["process"] = process
                stop = runtime["stop"]
            if stop:
                util.kill_process_tree(process)

        return progress, started, lambda: bool(runtime["stop"])

    def _fetch_chat(self, job_id, runtime, job, video, json_only=False):
        """
        Чат через TwitchDownloader (у YouTube и Kick сам чат скачивает vodchat,
        а HTML и видео чата — тоже он). json_only — только .chat.json: HTML и
        видео сделает вызывающий (чат, который ещё вклеится в другой).
        Возвращает (изменения загрузки, остановлен ли).
        """
        cli = self._twitchdownloader.command()
        twitch = job["platform"] == "twitch"
        if not cli and twitch:
            return {"chat_error": tr("для чата нужен TwitchDownloader — его можно скачать "
                                     "в настройках, раздел «Инструменты»")}, False
        mode, notes = job["chat"], []
        ffmpeg = self._ffmpeg.path()
        if mode == chat.RENDER and not ffmpeg:
            mode = chat.TEXT
            notes.append(tr("видео чата не сделано: нет ffmpeg"))
        size = (self._settings["chat_width"], self._settings["chat_height"],
                self._settings["chat_font_size"])
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(self._settings.path)),
                                "cache", "twitchdownloader")
        if twitch:
            steps = chat.commands(cli, job["url"], video, mode, job["section"], ffmpeg, size, temp_dir)
        else:
            # смайлы и значки уже встроены — TwitchDownloader их у Twitch не ищет
            steps = chat.finishing(cli, video, mode, ffmpeg, size, temp_dir, embedded=True) if cli else []
        if json_only:
            steps = steps[:1] if twitch else []
        self._update(job_id, state=PROCESSING, chat_progress={"stage": "download", "percent": None})
        logging.info(tr("скачивание чата: {name}", name=job["title"] or job["url"]))
        progress, started, stopped = self._chat_watchers(job_id, runtime)

        try:
            if not twitch:
                count = vodchat.download(
                    job["platform"], job["url"], chat.chat_paths(video)["json"], title=job["title"],
                    channel=job["channel"], section=job["section"], duration=job["duration"],
                    progress=lambda percent: progress("download", percent), on_process=started,
                    stopped=stopped, env=self._ytdlp.environment())
                if not count:
                    return {"chat_files": chat.existing(video),
                            "chat_error": tr("в чате этого видео нет сообщений")}, False
                if not cli:
                    notes.append(tr("чат сохранён в .chat.json; HTML и видео чата сделает "
                                    "TwitchDownloader — его можно скачать в настройках"))
            chat.run(steps, progress, started, stopped)
        except chat.ChatStopped:
            return {"chat_files": chat.existing(video)}, True
        except chat.ChatError as error:
            logging.warning(tr("чат не скачался: {name} — {error}",
                               name=job["title"] or job["url"], error=error))
            return {"chat_files": chat.existing(video), "chat_error": str(error)}, False
        files = chat.existing(video)
        logging.info(tr("чат скачан: {path}", path=files.get("html") or video))
        return {"chat_files": files, "chat_error": "; ".join(notes)}, False

    def _auth_args(self):
        """Вход в Twitch для yt-dlp: аргументы и файл кук, который потом удалить."""
        try:
            token = ytdlp.normalize_auth_token(self._settings["twitch_auth_token"])
        except ValueError as error:
            logging.warning(tr("токен Twitch не годится: {error}", error=error))
            return [], None
        if not token:
            return [], None
        path = ytdlp.write_cookie_file(token)
        return ["--cookies", path], path

    def _run_ytdlp(self, job_id, runtime, command):
        """
        Запускает загрузку и разбирает её вывод, пока yt-dlp не выйдет или его
        не погасят. Возвращает (код выхода, готовый файл, ошибка, последние строки).
        """
        # без PYTHONUNBUFFERED прогресс приходил бы пачками, а не по строке
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL,
                                   env=self._ytdlp.environment(
                                       dict(util.child_environment(), PYTHONUNBUFFERED="1")),
                                   creationflags=util.subprocess_flags())
        with self._lock:
            runtime["process"] = process
            stop_requested = runtime["stop"]
        if stop_requested:
            util.kill_process_tree(process)

        produced, error, tail = "", "", []
        # отрезок качает ffmpeg: yt-dlp о ходе молчит, зато ffmpeg пишет, докуда дошёл
        # (-progress) — доля по длине отрезка, скорость и сколько осталось считаем сами
        meter = {"time": time.monotonic(), "bytes": 0, "speed": None, "began": time.monotonic(),
                 "position": 0.0, "size": 0}
        section = self.job(job_id)["section"]
        length = (section[1] - section[0]) if section else None
        with process.stdout:
            for raw in process.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                kind, data = ytdlp.parse_progress(line)
                if kind == "progress":
                    progress = ytdlp.progress_of(data)
                    if progress["speed"] is None and progress["downloaded"]:
                        now = time.monotonic()
                        if now - meter["time"] >= 1 and progress["downloaded"] >= meter["bytes"]:
                            rate = (progress["downloaded"] - meter["bytes"]) / (now - meter["time"])
                            meter["speed"] = rate if meter["speed"] is None else meter["speed"] * 0.7 + rate * 0.3
                            meter.update(time=now, bytes=progress["downloaded"])
                        progress["speed"] = meter["speed"]
                    with self._lock:
                        self._job(job_id)["progress"] = progress
                    self._emit(job_id, progress_only=True)
                elif kind == "ffmpeg" and length:
                    meter["position"] = data.get("time", meter["position"])
                    meter["size"] = data.get("size", meter["size"])
                    now = time.monotonic()
                    if now - meter["time"] >= 1 and meter["size"] >= meter["bytes"]:
                        rate = (meter["size"] - meter["bytes"]) / (now - meter["time"])
                        meter["speed"] = rate if meter["speed"] is None else meter["speed"] * 0.7 + rate * 0.3
                        meter.update(time=now, bytes=meter["size"])
                    share = min(0.999, max(0.0, meter["position"] / length))
                    spent = now - meter["began"]
                    with self._lock:
                        self._job(job_id)["progress"] = {
                            "percent": share * 100.0, "downloaded": meter["size"] or None,
                            "total": meter["size"] / share if share > 0.02 and meter["size"] else None,
                            "speed": meter["speed"],
                            "eta": spent * (1 - share) / share if share > 0.02 else None}
                    self._emit(job_id, progress_only=True)
                elif kind == "postprocess":
                    if data.get("status") == "started" and ytdlp.is_heavy_postprocessor(data):
                        self._update(job_id, state=PROCESSING)
                elif kind == "file":
                    produced = data
                elif kind == "error":
                    error = data
                elif line:
                    tail = (tail + [line])[-5:]
                    logging.debug("yt-dlp: %s", line)
        process.wait()
        return process.returncode, produced, error, tail

    def _finish(self, job_id, runtime, state, **changes):
        """Итог загрузки. state=None — её остановили, итог зависит от причины."""
        reason = runtime["stop"]
        if runtime.get("remove"):
            with self._lock:
                self._remove_partials(self._job(job_id))
            self._drop(job_id)
            return
        with self._lock:
            job = self._job(job_id)
            if state is None:
                if reason == "cancel":
                    self._remove_partials(job)
                    state, changes = CANCELLED, dict(changes, target="", progress=None)
                elif reason == "shutdown":
                    state = QUEUED     # продолжится со следующего запуска
                else:
                    state = PAUSED
            retry = None
            # чат из архива — не начало эфира: его отказ по делу, повтор ничего не даст
            if state == FAILED and job.get("fixed_target") and job["quality"] != CHAT_ARCHIVE \
                    and not runtime.get("remove"):
                # начало эфира — само ещё раз, а не ждать, пока нажмут «Повторить»;
                # в строке — короткая причина, текст ошибки — в журнале
                reason, passing = beginning_reason(changes.get("error") or job.get("error"))
                changes = dict(changes, reason=reason)
                retries = job.get("auto_retries") or 0
                if passing and retries < len(BEGINNING_RETRIES):
                    retry = BEGINNING_RETRIES[retries]
                    state = QUEUED
                    changes = dict(changes, auto_retries=retries + 1, retry_at=time.time() + retry,
                                   progress=None)
            if state in (DONE, FAILED, CANCELLED):
                changes.setdefault("finished", time.time())
            if state == DONE:
                changes["progress"] = dict(job["progress"] or {}, percent=100.0)
                changes["size"] = _file_size(changes.get("file") or job.get("file"))
            job.update(state=state, **changes)
            self._save()
        if state == DONE:
            logging.info(tr("скачано: {path}", path=job["file"]))
        elif state == FAILED:
            logging.error(tr("не скачалось: {name} — {error}",
                             name=job["title"] or job["url"], error=job["error"]))
        elif retry is not None:
            logging.warning(tr("не скачалось: {name} — {error}; попробую снова через {seconds} с "
                               "(повтор {number} из {total})", name=job["title"] or job["url"],
                               error=job["error"], seconds=retry, number=job["auto_retries"],
                               total=len(BEGINNING_RETRIES)))
        self._emit(job_id)

    def _choose_target(self, job):
        """Свободный путь: не занят ни готовым файлом, ни другой загрузкой."""
        # платформа становится именем папки, а приходить может и из окна программы
        platform = re.sub(r"[^a-z0-9_-]", "", (job["platform"] or "").lower()) or "other"
        label = channel_label(job.get("folder") or job["channel"] or "unknown")
        if platform in _LOWERCASE_LABELS:
            label = label.lower()
        directory = os.path.join(self.downloads_dir(), platform, label)
        moment = (datetime.datetime.fromtimestamp(job["timestamp"])
                  if job["timestamp"] else None)
        suffix = ""
        # у «только чата» в имени и так будет .chat.json — без лишнего « chat»
        if job["quality"] not in ("best", CHAT_ONLY):
            suffix += " " + job["quality"]
        if job["section"]:
            suffix += " (%s-%s)" % (_clock(job["section"][0]), _clock(job["section"][1]))
        base = build_download_base(directory, label, job["title"], moment,
                                   job["video_id"], suffix)

        with self._lock:
            taken = {other["target"] for other in self._jobs
                     if other["id"] != job["id"] and other["target"]}
        candidate, index = base, 1
        while candidate in taken or self._finished_files(candidate):
            index += 1
            candidate = "%s (%d)" % (base, index)
        return candidate

    @staticmethod
    def _finished_files(base):
        """Готовые файлы с таким началом имени (не недокачанные хвосты yt-dlp)."""
        return [path for path in glob.glob(glob.escape(base) + ".*")
                if not path.endswith((".part", ".ytdl")) and ".part-Frag" not in path]

    @staticmethod
    def _remove_partials(job):
        """
        Недокачанное отменённой загрузки: .part, .ytdl, куски и промежуточные
        файлы. Готовое видео и его чат не трогаются, даже если отменили чат.
        """
        if not job.get("target"):
            return
        keep = set()
        if job.get("file"):
            keep = {os.path.normcase(os.path.abspath(path))
                    for path in [job["file"], *chat.chat_paths(job["file"]).values()]}
        for path in glob.glob(glob.escape(job["target"]) + ".*"):
            if os.path.normcase(os.path.abspath(path)) not in keep:
                util.remove_quietly(path)
