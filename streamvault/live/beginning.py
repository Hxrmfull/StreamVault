# -*- coding: utf-8 -*-
"""
Запись с начала эфира, если запись началась позже.

Поток эфира, который пишет streamlink, держит лишь последние 15–25 секунд:
раньше там ничего нет. Начало эфира есть в его записи на самой площадке —
так перематывают и их сайты. Twitch и Kick выкладывают запись, пока эфир
идёт; YouTube и VK — когда он кончился. Поэтому запись идёт как обычно,
а кусок от начала эфира до начала записи скачивает очередь загрузок из
записи площадки и кладёт рядом первой частью — «… - part00», с чатом этого
куска. Нет записи (у канала выключен архив, у трансляции — перемотка) —
об этом пишется в журнал и на странице канала, а запись идёт как шла.

YouTube и VK выкладывают запись через минуты или часы после конца эфира —
программу за это время могут закрыть или обновить. Поэтому то, чего ждём,
лежит и на диске (store, beginning.json) и после запуска ждётся дальше.
"""

import json
import logging
import os
import re
import threading
import time

from .. import sites
from ..archive.ytdlp import H264
from ..i18n import tr
from ..platforms import channel_id

# Запись началась позже эфира меньше чем на столько — добирать нечего.
MIN_GAP = 90
# Кусок берётся чуть длиннее: видео эфира у зрителей запаздывает на секунды,
# и лучше пара секунд повтора, чем дыра между началом и записью.
OVERLAP = 15
# Запись идущего эфира бывает видна на площадке не сразу: паузы между попытками.
LIVE_RETRIES = (30, 60, 120, 300)
# Опубликованную после эфира запись ждать, проверяя раз в столько секунд, и до какого срока.
ENDED_CHECK = 300
ENDED_PATIENCE = 6 * 3600


def download_quality(quality):
    """
    Качество записи эфира ("720p60", "best", "audio_only") — в качество загрузки:
    с приставкой h264 — те же кодеки, что у записи, иначе не склеить.
    """
    first = str(quality or "best").split(",")[0].strip().lower()
    if first.startswith("audio"):
        return "audio"
    match = re.match(r"(\d{3,4})p", first)
    return H264 + (match.group(1) if match else "best")


def clock(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


class Beginning:
    """
    Начало эфиров, которые начали записывать позже. downloads — очередь
    загрузок: через неё скачивается кусок (с прогрессом, повторами и чатом),
    она же даёт yt-dlp модулям площадок.
    """

    def __init__(self, downloads, stop_event=None, store=None):
        """store — файл, где лежит то, чего ждём (переживает перезапуск); None — только в памяти."""
        self._downloads = downloads
        self._stop = stop_event or threading.Event()
        self._lock = threading.Lock()
        self._store = store
        self._pending = {}      # канал -> что добрать после конца эфира (эфир ещё идёт)
        self._waiting = {}      # канал -> эфир кончился, ждём его запись на площадке
        self._resume()

    def stop(self):
        self._stop.set()

    def joined(self, head, main):
        """Начало склеено с записью: его загрузка теперь — про склеенную запись."""
        self._downloads.mark_joined(head, main)

    def busy(self, path):
        """Начало эфира path (файл «… - part00») ещё качается — с чатом или без."""
        stem = os.path.splitext(path)[0]
        final = ("done", "failed", "cancelled")
        return any(job.get("fixed_target") == stem and job.get("state") not in final
                   for job in self._downloads.snapshot()["jobs"])

    def session_started(self, platform, channel, began, target, title, quality, chat_mode,
                        meta=None, report=None):
        """
        Запись эфира началась (первая часть) в began. target — путь части
        «начало эфира» без расширения. report(состояние) — для страницы канала.
        """
        report = report or (lambda status: None)
        when = sites.beginning_when(platform)
        tag = "%s:%s" % (platform, channel)
        if when is None:
            report({"state": "unavailable", "reason": tr("у этой площадки начала эфира не взять")})
            return
        started = sites.stream_started(platform, channel, meta, self._downloads)
        if started is None:
            logging.info(tr("{tag}: когда начался эфир, не узнать — запись идёт с текущего места", tag=tag))
            report({"state": "unavailable", "reason": tr("не узнать, когда начался эфир")})
            return
        gap = began - started
        if gap < MIN_GAP:
            return
        request = {"platform": platform, "channel": channel, "tag": tag, "started": started,
                   "length": gap + OVERLAP, "target": target, "title": title, "meta": dict(meta or {}),
                   "quality": download_quality(quality), "chat_mode": chat_mode, "report": report}
        if when == "live":
            report({"state": "searching", "length": gap})
            threading.Thread(target=self._from_live, args=(request,), name="beginning-" + tag,
                             daemon=True).start()
        else:
            logging.info(tr("{tag}: начало эфира ({length}) скачается, когда площадка выложит его запись",
                            tag=tag, length=clock(gap)))
            report({"state": "waiting", "length": gap})
            with self._lock:
                self._pending[channel_id(platform, channel)] = request
                self._save()

    def session_ended(self, platform, channel):
        """Запись эфира кончилась: YouTube и VK выкладывают его запись после конца."""
        with self._lock:
            request = self._pending.pop(channel_id(platform, channel), None)
            if request is None:
                return
            request["ended"] = time.time()
            self._waiting[channel_id(platform, channel)] = request
            self._save()
        self._wait_published(request)

    def _wait_published(self, request):
        threading.Thread(target=self._from_published, args=(request,),
                         name="beginning-" + request["tag"], daemon=True).start()

    # ------------------------------------------------------------ на диске

    def _save(self):
        """Под self._lock: чего ждём — в store, без обратных вызовов окна."""
        if not self._store:
            return
        keep = [{key: value for key, value in request.items() if key != "report"}
                for request in list(self._pending.values()) + list(self._waiting.values())]
        try:
            temporary = self._store + ".tmp"
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump(keep, target, ensure_ascii=False, default=str)
            os.replace(temporary, self._store)
        except OSError as error:
            logging.warning(tr("не сохранить, чего ждёт запись с начала эфира: {error}", error=error))

    def _resume(self):
        """
        После запуска — ждать дальше то, чего ждали. Эфир, запись которого
        оборвал выход из программы, мог и кончиться: его запись тоже ищется
        как опубликованная — у YouTube и VK она и появляется только после конца.
        """
        if not self._store:
            return
        try:
            with open(self._store, encoding="utf-8") as source:
                saved = json.load(source)
        except (OSError, ValueError):
            return
        for request in saved if isinstance(saved, list) else []:
            if not isinstance(request, dict) or not request.get("tag") or not request.get("target"):
                continue
            request["report"] = lambda status: None
            request.setdefault("ended", time.time())
            self._waiting[channel_id(request["platform"], request["channel"])] = request
            logging.info(tr("{tag}: жду запись эфира на площадке, чтобы добрать его начало ({length})",
                            tag=request["tag"], length=clock(request["length"] - OVERLAP)))
        with self._lock:
            self._save()
        for request in list(self._waiting.values()):
            self._wait_published(request)

    def _done_waiting(self, request):
        with self._lock:
            key = channel_id(request["platform"], request["channel"])
            if self._waiting.get(key) is request:
                del self._waiting[key]
                self._save()

    # ------------------------------------------------------------ поиск записи

    def _archive(self, request):
        """
        Запись эфира на площадке: (ссылка, "") или (None, почему нет). Сбой
        (нет связи, yt-dlp не ответил) — тоже «пока нет»: поток ожидания от
        него не падает, а пробует снова в свой срок.
        """
        try:
            return sites.archive_for(request["platform"], request["channel"], request["started"],
                                     request["meta"], self._downloads)
        except Exception as error:
            logging.warning(tr("{tag}: запись эфира на площадке не проверить ({error}) — попробую позже",
                               tag=request["tag"], error=error))
            return None, str(error)

    def _from_live(self, request):
        reason = ""
        for delay in (0,) + LIVE_RETRIES:
            if self._stop.wait(delay):
                return
            url, reason = self._archive(request)
            if url:
                self._queue(request, url)
                return
        self._give_up(request, reason)

    def _from_published(self, request):
        # срок — от конца эфира, а не от запуска программы: перезапуск его не продлевает
        deadline = float(request.get("ended") or time.time()) + ENDED_PATIENCE
        reason = ""
        while not self._stop.is_set() and time.time() < deadline:
            url, reason = self._archive(request)
            if url:
                self._done_waiting(request)
                self._queue(request, url)
                return
            if self._stop.wait(ENDED_CHECK):
                return
        if not self._stop.is_set():
            self._done_waiting(request)
            self._give_up(request, reason)

    def _give_up(self, request, reason):
        logging.warning(tr("{tag}: начало эфира не взять — {reason}", tag=request["tag"],
                           reason=reason or tr("записи эфира на площадке нет")))
        request["report"]({"state": "unavailable", "reason": reason, "length": request["length"] - OVERLAP})

    def _queue(self, request, url):
        kind = "video" if request["platform"] == "youtube" else "vod"
        meta = {"title": request["title"], "channel": request["channel"], "platform": request["platform"],
                "kind": kind, "timestamp": request["started"], "live_status": "was_live"}
        try:
            job_id = self._downloads.add(url, quality=request["quality"], section=(0, request["length"]),
                                         meta=meta, chat_mode=request["chat_mode"], target=request["target"])
        except ValueError as error:
            self._give_up(request, str(error))
            return
        logging.info(tr("{tag}: начало эфира ({length}) скачивается — «Загрузки»", tag=request["tag"],
                        length=clock(request["length"] - OVERLAP)))
        request["report"]({"state": "queued", "job": job_id, "length": request["length"] - OVERLAP})
