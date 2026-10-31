# -*- coding: utf-8 -*-
"""
VK Video Live. Открытые запросы api.live.vkvideo.ru — те же, что делает
сайт у зрителя без входа: сведения о канале, идущий эфир, списки записей
и клипов (archive/vkrecords.py: yt-dlp их не знает).
"""

import re

from ..i18n import tr
from ..platforms import is_url
from .common import Memo, get_json, https, search_result

NAME = "VK Video Live"

BLOG = "https://api.live.vkvideo.ru/v1/blog/{name}"
STREAM = "https://api.live.vkvideo.ru/v1/blog/{name}/public_video_stream"
# поиск каналов по части имени — тот же запрос, что делает строка поиска сайта
SEARCH = "https://api.live.vkvideo.ru/v8/search/channel"
# Имя канала VK Video Live: латиница, цифры, точка, дефис и подчёркивание.
CHANNEL_NAME = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
# Кадр эфира: опрос каналов частый, а кадр сайт меняет раз в минуты.
PREVIEW_SECONDS = 60

# Ссылки — для окна и ошибок; сами списки берёт list_channel.
LISTINGS = {
    "vods": "https://live.vkvideo.ru/{channel}/records",
    "clips": "https://live.vkvideo.ru/{channel}/clips",
}

CHAT_KINDS = ("vod", "clip")

# Запись с начала эфира: VK выкладывает запись эфира в записи канала,
# когда он кончился, — начало берётся из неё после конца эфира.
BEGINNING = "ended"

UNSUPPORTED = {}

_streams = Memo(PREVIEW_SECONDS)


def _name(channel):
    name = str(channel or "").strip().lstrip("@")
    return name if CHANNEL_NAME.match(name) else ""


def search(query, limit, tools=None):
    """
    По части имени канала или ника. Кто в эфире, сколько зрителей и что идёт —
    в том же ответе: идущие эфиры и их категории лежат в refs.
    """
    text = str(query or "").strip().lstrip("@")
    if not text:
        return []
    data = get_json("GET", SEARCH, params={"searchQuery": text, "limit": limit})
    data = data if isinstance(data, dict) else {}
    refs = data.get("refs") or {}
    streams, categories = refs.get("streams") or {}, refs.get("categories") or {}
    found = []
    for item in ((data.get("data") or {}).get("channels") or [])[:limit]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        live = item.get("channelStatus") == "online"
        stream = (streams.get(item.get("streamId")) or {}) if live else {}
        category = categories.get(stream.get("categoryId")) or {}
        found.append(search_result("vkvideolive", str(item["url"]), item.get("nick"), https(item.get("avatarUrl")),
                                   (item.get("counters") or {}).get("subscribers"), live=live,
                                   viewers=(stream.get("counters") or {}).get("viewers") if live else None,
                                   game=category.get("title") or ""))
    return found


def profile(channel, tools=None):
    name = _name(channel)
    if not name:
        return None
    data = get_json("GET", BLOG.format(name=name))
    if not isinstance(data, dict) or not data.get("blogUrl"):
        return None
    owner = data.get("owner") or {}
    avatar = owner.get("avatarUrl") if owner.get("hasAvatar", True) else ""
    return {"name": str(owner.get("displayName") or owner.get("nick") or data["blogUrl"]),
            "avatar": https(avatar)}


def live_preview(channel, meta):
    if not channel or is_url(channel):
        return ""
    name = _name(channel)
    if not name:
        return ""
    data = _streams.get(name.lower(), lambda: get_json("GET", STREAM.format(name=name)))
    if not isinstance(data, dict) or not data.get("isOnline"):
        return ""
    return https(data.get("previewUrl"))


def list_channel(channel, kind, limit, start, period, inspect=None):
    """Списки VK — своими запросами, в виде ответа inspect у yt-dlp."""
    from ..archive import vkrecords
    from ..archive.ytdlp import YtdlpError
    try:
        return vkrecords.list_records(channel, limit=limit, start=start, kind=kind)
    except vkrecords.VkRecordsError as error:
        raise YtdlpError(str(error))


def make_chat(channel, ytdlp_environment=None):
    from ..live.chatreaders import VkVideoChat
    return VkVideoChat(channel)


def stream_started(channel, meta=None, tools=None):
    """Когда начался идущий эфир (секунды) или None."""
    name = _name(channel)
    if not name:
        return None
    data = get_json("GET", STREAM.format(name=name)) or {}
    started = data.get("startTime") if data.get("isOnline") else None
    return float(started) if isinstance(started, (int, float)) and not isinstance(started, bool) else None


def archive_for(channel, started, meta=None, tools=None):
    """Запись эфира, начавшегося в started: (ссылка, "") или (None, почему её нет)."""
    from ..archive import vkrecords
    name = _name(channel)
    if not name:
        return None, tr("канал задан ссылкой")
    try:
        records = vkrecords.list_records(name, limit=10).get("entries") or []
    except vkrecords.VkRecordsError as error:
        return None, str(error)
    for record in records:
        if record.get("timestamp") and started and abs(record["timestamp"] - started) < 300:
            return record["url"], ""
    return None, tr("VK ещё не выложил запись этого эфира")
