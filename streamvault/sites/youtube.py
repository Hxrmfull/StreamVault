# -*- coding: utf-8 -*-
"""
YouTube. Всё — через yt-dlp: страница канала уводит зрителя без входа на
согласие с cookie (consent.youtube.com), а соглашаться за пользователя
программа не должна. yt-dlp получает те же сведения открыто.
"""

import re

from ..i18n import tr
from ..platforms import is_url
from .common import SiteError, https, search_result

NAME = "YouTube"

LISTINGS = {
    "vods": "https://www.youtube.com/@{channel}/streams",
    "videos": "https://www.youtube.com/@{channel}/videos",
    "shorts": "https://www.youtube.com/@{channel}/shorts",
}

# Чат бывает у прошедших эфиров; yt-dlp называет их video, а обычное видео
# отличает live_status == not_live. Неизвестно, был ли эфир, — пробуем.
CHAT_KINDS = ("video",)
CHAT_SKIP_STATUS = ("not_live",)
# Чат эфира по ссылке на трансляцию (watch?v=) читает yt-dlp.
CHAT_FROM_URL = True

# Запись с начала эфира: перемотку трансляции yt-dlp кусками не скачивает,
# а после конца эфира YouTube публикует его как видео по той же ссылке —
# начало берётся оттуда. Нет перемотки у трансляции — нет и записи начала.
BEGINNING = "ended"
WATCH = "https://www.youtube.com/watch?v={id}"

UNSUPPORTED = {
    "clips": "у YouTube нет списка клипов канала — вместо них вкладка Shorts",
}

# Сколько ждать yt-dlp со сведениями о канале: обычно около 2 с.
PROFILE_TIMEOUT = 30
# Сведения о только что кончившемся эфире yt-dlp собирает дольше: 30 с не хватало.
ARCHIVE_TIMEOUT = 120


def youtube_results(entries, limit=5):
    """Каналы из выдачи yt-dlp: @имя, если оно есть, — иначе ссылка на трансляцию канала."""
    found = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        handle = str(entry.get("uploader_id") or "")
        if handle.startswith("@"):
            channel = handle[1:]
        elif entry.get("channel_url"):
            channel = entry["channel_url"].rstrip("/") + "/live"
        else:
            continue
        thumbnails = [thumb.get("url") for thumb in entry.get("thumbnails") or [] if thumb.get("url")]
        avatar = https(thumbnails[-1]) if thumbnails else ""
        found.append(search_result("youtube", channel, entry.get("channel") or entry.get("title"),
                                   avatar, entry.get("channel_follower_count")))
        if len(found) >= limit:
            break
    return found


def search(query, limit, tools=None):
    if tools is None:
        raise SiteError(tr("поиск по {platform} недоступен", platform=NAME))
    return youtube_results(tools.youtube_channels(query, limit), limit)


def channel_page(channel):
    """Страница канала для yt-dlp: @имя или ссылка канала без хвоста /live."""
    if is_url(channel):
        return re.sub(r"/live/?$", "", channel.split("?", 1)[0].rstrip("/"))
    return "https://www.youtube.com/@%s" % channel.lstrip("@")


def profile(channel, tools=None):
    """
    Имя и аватарка: yt-dlp -J --flat-playlist -I 0 — сведения о канале без
    списка видео. Аватарка — миниатюра avatar_uncropped.
    """
    if tools is None:
        raise SiteError(tr("поиск по {platform} недоступен", platform=NAME))
    if is_url(channel) and not re.search(r"youtube\.com/(@|channel/|c/|user/)", channel):
        return None      # ссылка на видео — не канал
    data = tools.ytdlp_json(["-J", "--flat-playlist", "--no-warnings", "--color", "never",
                             "-I", "0", "--", channel_page(channel)], PROFILE_TIMEOUT)
    if not isinstance(data, dict):
        return None
    avatar = ""
    for thumbnail in data.get("thumbnails") or []:
        if isinstance(thumbnail, dict) and thumbnail.get("id") == "avatar_uncropped":
            avatar = https(thumbnail.get("url"))
            break
    name = str(data.get("channel") or data.get("uploader") or "")
    return {"name": name, "avatar": avatar} if name or avatar else None


def live_preview(channel, meta):
    """Кадр трансляции — по её номеру, который сообщает streamlink."""
    video_id = str((meta or {}).get("id") or "")
    if re.fullmatch(r"[\w-]{11}", video_id):
        return "https://i.ytimg.com/vi/%s/hqdefault_live.jpg" % video_id
    return ""


def make_chat(channel, ytdlp_environment=None):
    from ..live.chatreaders import YouTubeChat
    return YouTubeChat(channel, environment=ytdlp_environment)


def _video_id(meta):
    video_id = str((meta or {}).get("id") or "")
    return video_id if re.fullmatch(r"[\w-]{11}", video_id) else ""


def stream_started(channel, meta=None, tools=None):
    """Когда начался идущий эфир (секунды) или None: спрашивает yt-dlp."""
    video_id = _video_id(meta)
    if tools is None or not video_id:
        return None
    data = tools.ytdlp_json(["-J", "--no-warnings", "--color", "never", "--", WATCH.format(id=video_id)],
                            PROFILE_TIMEOUT)
    started = (data or {}).get("release_timestamp") if isinstance(data, dict) else None
    return float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else None


def archive_for(channel, started, meta=None, tools=None):
    """Опубликованная запись эфира: (ссылка, "") или (None, почему её ещё нет)."""
    video_id = _video_id(meta)
    if tools is None or not video_id:
        return None, tr("не известен номер трансляции")
    url = WATCH.format(id=video_id)
    data = tools.ytdlp_json(["-J", "--no-warnings", "--color", "never", "--", url], ARCHIVE_TIMEOUT)
    status = (data or {}).get("live_status") if isinstance(data, dict) else None
    if status == "was_live":
        return url, ""
    if status in ("is_live", "post_live", "is_upcoming"):
        return None, tr("YouTube ещё готовит запись эфира")
    return None, tr("YouTube не сохранил запись этого эфира")
