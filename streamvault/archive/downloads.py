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
from . import chat, vodchat, ytdlp, ytdlp_update

# Состояния загрузки.
QUEUED = "queued"            # ждёт свободного места
DOWNLOADING = "downloading"
PROCESSING = "processing"    # yt-dlp склеивает или перепаковывает
PAUSED = "paused"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

UNFINISHED = (QUEUED, DOWNLOADING, PROCESSING, PAUSED, FAILED)
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
            self.add(entry["url"], quality=quality, meta=meta, chat_mode=chat_mode)
            urls.add(entry["url"])
            added += 1
        logging.info(tr("{channel}: всё с вкладки «{kind}» — в очереди {added}, уже было {known}",
                        channel=channel, kind=kind, added=added, known=known))
        return {"added": added, "known": known, "total": len(entries)}

    # ------------------------------------------------------------ управление

    def add(self, url, quality=None, section=None, meta=None, force=False, chat_mode=None, target=None):
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
                snapshot = self._new_job(url, quality, section, meta, chat_mode, target)
        if existing is not None:
            self._emit(existing)
            return existing
        logging.info(tr("загрузка добавлена: {name}", name=snapshot["title"] or url))
        if self._bus is not None:
            self._bus.emit("download", job=snapshot)
        return snapshot["id"]

    def _new_job(self, url, quality, section, meta, chat_mode, target=None):
        """Новая загрузка в очереди. Возвращает её снимок для окна."""
        with self._lock:
            job = {
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

    def _update(self, job_id, **changes):
        with self._lock:
            job = self._job(job_id)
            job.update(changes)
            if "state" in changes:
                self._save()
        self._emit(job_id)

    def _requeue(self, job):
        # target остаётся: yt-dlp продолжит недокачанное с того же места
        job.update(state=QUEUED, error="", progress=None, finished=None)
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
                waiting = [job for job in self._jobs if job["state"] == QUEUED
                           and job["id"] not in self._running]
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

    def _finish_with_chat(self, job_id, runtime, produced):
        """Видео скачано; если просили — ещё чат. Чат не удался — видео всё равно готово."""
        job = self.job(job_id)
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

    def _fetch_chat(self, job_id, runtime, job, video):
        """
        Чат через TwitchDownloader (у YouTube и Kick сам чат скачивает vodchat,
        а HTML и видео чата — тоже он). Возвращает (изменения загрузки, остановлен ли).
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
        self._update(job_id, state=PROCESSING, chat_progress={"stage": "download", "percent": None})
        logging.info(tr("скачиваю чат: {name}", name=job["title"] or job["url"]))

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

        try:
            if not twitch:
                count = vodchat.download(
                    job["platform"], job["url"], chat.chat_paths(video)["json"], title=job["title"],
                    channel=job["channel"], section=job["section"], duration=job["duration"],
                    progress=lambda percent: progress("download", percent), on_process=started,
                    stopped=lambda: bool(runtime["stop"]), env=self._ytdlp.environment())
                if not count:
                    return {"chat_files": chat.existing(video),
                            "chat_error": tr("в чате этого видео нет сообщений")}, False
                if not cli:
                    notes.append(tr("чат сохранён в .chat.json; HTML и видео чата сделает "
                                    "TwitchDownloader — его можно скачать в настройках"))
            chat.run(steps, progress, started, lambda: bool(runtime["stop"]))
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
        with process.stdout:
            for raw in process.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                kind, data = ytdlp.parse_progress(line)
                if kind == "progress":
                    with self._lock:
                        self._job(job_id)["progress"] = ytdlp.progress_of(data)
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
        if job["quality"] not in ("best",):
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
