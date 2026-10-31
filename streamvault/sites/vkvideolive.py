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

# Поиск по части имени. Открытого запроса, которым сайт ищет без входа,
# пока не нашлось: страница поиска собирается у зрителя, а в облаке, где
# писалась программа, сайт недоступен. Поэтому проверяется точное имя.
SEARCH_NOTE = "нет канала с точно таким именем"

_streams = Memo(PREVIEW_SECONDS)


def _name(channel):
    name = str(channel or "").strip().lstrip("@")
    return name if CHANNEL_NAME.match(name) else ""


def search(query, limit, tools=None):
    name = _name(query)
    if not name:
        return []
    data = get_json("GET", BLOG.format(name=name))
    if not isinstance(data, dict) or not data.get("blogUrl"):
        return []
    owner = data.get("owner") or {}
    return [search_result("vkvideolive", str(data["blogUrl"]),
                          owner.get("displayName") or owner.get("nick"),
                          owner.get("avatarUrl") if owner.get("hasAvatar", True) else "",
                          (data.get("count") or {}).get("subscribers"))][:limit]


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
