# -*- coding: utf-8 -*-
"""
Kick. Открытые запросы сайта kick.com: поиск, сведения о канале (имя,
аватарка и кадр идущего эфира — одним ответом), списки записей и клипов
понимает yt-dlp. Превью записей yt-dlp не отдаёт — их подставляет
enrich_listing из списка записей сайта.
"""

import re

from ..i18n import tr
from ..platforms import is_url
from .common import Memo, get_json, https, search_result, unix_time

NAME = "Kick"

SEARCH = "https://kick.com/api/search"
CHANNEL = "https://kick.com/api/v2/channels/{slug}"
# Идущий эфир отдельно: в сведениях о канале кадр бывает пустым (thumbnail:
# null), а здесь он есть.
LIVESTREAM = "https://kick.com/api/v2/channels/{slug}/livestream"
# Последние 30 записей канала с превью (дальше страниц нет: Kick и хранит
# записи около месяца). id здесь — тот же vod_id, что в ссылках yt-dlp.
VIDEOS = "https://web.kick.com/api/v1/channels/{id}/videos"
WEB_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault",
               "Accept": "application/json", "x-app-platform": "web"}
# Превью записей меняются редко, а список канала листают часто.
VIDEOS_SECONDS = 300
# Имя канала Kick: латиница, цифры, дефис и подчёркивание.
SLUG = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Сведения о канале нужны и кадру эфира, и профилю; опрос каналов частый,
# а кадр Kick обновляет раз в несколько минут — минуты хватает.
INFO_SECONDS = 60

LISTINGS = {
    "vods": "https://kick.com/{channel}/videos",
    "clips": "https://kick.com/{channel}/clips",
}

CHAT_KINDS = ("vod", "clip")

# Запись с начала эфира: Kick выкладывает запись эфира, пока он идёт
# (в списке записей канала она с is_live), — начало берётся из неё сразу.
BEGINNING = "live"
VIDEO_PAGE = "https://kick.com/{slug}/videos/{id}"

UNSUPPORTED = {}

_info = Memo(INFO_SECONDS)
_live = Memo(INFO_SECONDS)
_videos = Memo(VIDEOS_SECONDS)


def search(query, limit, tools=None):
    data = get_json("GET", SEARCH, params={"searched_word": query})
    found = []
    for item in ((data or {}).get("channels") or [])[:limit]:
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        user = item.get("user") or {}
        live = item.get("isLive", item.get("is_live"))
        found.append(search_result("kick", str(item["slug"]).lower(),
                                   user.get("username") or item["slug"],
                                   user.get("profile_pic") or user.get("profilepic"),
                                   item.get("followers_count", item.get("followersCount")),
                                   live=bool(live) if live is not None else None))
    return found


def channel_info(slug):
    """Ответ kick.com о канале или None, если такого нет. Помнится минуту."""
    slug = slug.strip().lstrip("@").lower()
    if not SLUG.match(slug):
        return None
    return _info.get(slug, lambda: _dict_or_none(get_json("GET", CHANNEL.format(slug=slug))))


def _dict_or_none(data):
    return data if isinstance(data, dict) and data.get("slug") else None


def profile(channel, tools=None):
    data = channel_info(channel)
    if data is None:
        return None
    user = data.get("user") or {}
    return {"name": str(user.get("username") or data["slug"]),
            "avatar": https(user.get("profile_pic") or user.get("profilepic"))}


def _thumbnail(value):
    if isinstance(value, dict):
        return https(value.get("url") or value.get("src"))
    return https(value)


def live_preview(channel, meta):
    """
    Кадр есть, только пока идёт эфир: у канала офлайн livestream пуст.
    Пустой кадр в сведениях о канале — ещё раз у самого эфира.
    """
    if not channel or is_url(channel):
        return ""
    data = channel_info(channel) or {}
    livestream = data.get("livestream") or {}
    preview = _thumbnail(livestream.get("thumbnail"))
    if preview or not livestream:
        return preview
    slug = data["slug"].lower()
    live = _live.get(slug, lambda: get_json("GET", LIVESTREAM.format(slug=slug))) or {}
    return _thumbnail(((live.get("data") or {}) if isinstance(live, dict) else {}).get("thumbnail"))


def enrich_listing(channel, kind, result):
    """У записей из списка yt-dlp нет превью — берутся из списка записей сайта."""
    entries = [entry for entry in result.get("entries") or () if not entry.get("thumbnail")]
    if kind != "vods" or not entries or is_url(channel):
        return
    info = channel_info(channel) or {}
    if not info.get("id"):
        return
    data = _videos.get(info["id"], lambda: get_json("GET", VIDEOS.format(id=info["id"]), headers=WEB_HEADERS))
    thumbnails = {str(video.get("id")): _thumbnail(video.get("thumbnail"))
                  for video in ((data or {}).get("data") or ()) if isinstance(video, dict)}
    for entry in entries:
        entry["thumbnail"] = thumbnails.get(str(entry.get("id"))) or ""


def make_chat(channel, ytdlp_environment=None):
    from ..live.chatreaders import KickChat
    return KickChat(channel)


def stream_started(channel, meta=None, tools=None):
    """Когда начался идущий эфир (секунды) или None."""
    if not channel or is_url(channel):
        return None
    livestream = (channel_info(channel) or {}).get("livestream") or {}
    return unix_time(livestream.get("start_time") or livestream.get("created_at"))


def archive_for(channel, started, meta=None, tools=None):
    """Запись идущего эфира: (ссылка, "") или (None, почему её нет)."""
    info = channel_info(channel) or {}
    if not info.get("id"):
        return None, tr("канал не найден")
    data = get_json("GET", VIDEOS.format(id=info["id"]), headers=WEB_HEADERS)
    for video in (data or {}).get("data") or ():
        if not isinstance(video, dict) or not video.get("id"):
            continue
        moment = unix_time(video.get("start_time"))
        if video.get("is_live") or (moment and started and abs(moment - started) < 300):
            return VIDEO_PAGE.format(slug=info["slug"].lower(), id=video["id"]), ""
    return None, tr("Kick ещё не выложил запись этого эфира")
