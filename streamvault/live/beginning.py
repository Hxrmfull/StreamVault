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
"""

import logging
import re
import threading
import time

from .. import sites
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
    """Качество записи эфира ("720p60", "best", "audio_only") — в качество загрузки."""
    first = str(quality or "best").split(",")[0].strip().lower()
    if first.startswith("audio"):
        return "audio"
    match = re.match(r"(\d{3,4})p", first)
    return match.group(1) if match else "best"


def clock(seconds):
    seconds = int(seconds)
    return "%d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


class Beginning:
    """
    Начало эфиров, которые начали записывать позже. downloads — очередь
    загрузок: через неё скачивается кусок (с прогрессом, повторами и чатом),
    она же даёт yt-dlp модулям площадок.
    """

    def __init__(self, downloads, stop_event=None):
        self._downloads = downloads
        self._stop = stop_event or threading.Event()
        self._lock = threading.Lock()
        self._pending = {}      # канал -> что добрать после конца эфира

    def stop(self):
        self._stop.set()

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

    def session_ended(self, platform, channel):
        """Запись эфира кончилась: YouTube и VK выкладывают его запись после конца."""
        with self._lock:
            request = self._pending.pop(channel_id(platform, channel), None)
        if request is not None:
            threading.Thread(target=self._from_published, args=(request,),
                             name="beginning-" + request["tag"], daemon=True).start()

    # ------------------------------------------------------------ поиск записи

    def _from_live(self, request):
        reason = ""
        for delay in (0,) + LIVE_RETRIES:
            if self._stop.wait(delay):
                return
            url, reason = sites.archive_for(request["platform"], request["channel"], request["started"],
                                            request["meta"], self._downloads)
            if url:
                self._queue(request, url)
                return
        self._give_up(request, reason)

    def _from_published(self, request):
        deadline = time.monotonic() + ENDED_PATIENCE
        reason = ""
        while not self._stop.is_set() and time.monotonic() < deadline:
            url, reason = sites.archive_for(request["platform"], request["channel"], request["started"],
                                            request["meta"], self._downloads)
            if url:
                self._queue(request, url)
                return
            if self._stop.wait(ENDED_CHECK):
                return
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
