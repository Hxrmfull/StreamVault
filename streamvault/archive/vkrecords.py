# -*- coding: utf-8 -*-
"""
Записи и клипы канала VK Video Live — списком, как вкладки сайта.

yt-dlp умеет скачать запись VK по ссылке (клип — с расширением программы,
ytdlp_plugins), но списков канала не знает. Сайт берёт их открытыми
запросами — страницами по offset; отсюда они в том же виде, что
describe_entry у yt-dlp, чтобы окно показывало их как у других площадок.
"""

import requests

from ..i18n import tr

RECORDS = "https://api.live.vkvideo.ru/v1/blog/{blog}/public_video_stream/record/"
CLIPS = "https://api.live.vkvideo.ru/v1/channel/{blog}/clip/"
RECORD_PAGE = "https://live.vkvideo.ru/{blog}/record/{id}"
CLIP_PAGE = "https://live.vkvideo.ru/{blog}/clip/{id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault",
           "Accept": "application/json"}
TIMEOUT = 20


class VkRecordsError(Exception):
    """Список не получен. Текст — для человека."""


def describe(record, blog, kind="vod"):
    owner = ((record.get("blog") or {}).get("owner") or {})
    return {
        "id": str(record.get("id") or ""),
        "url": (CLIP_PAGE if kind == "clip" else RECORD_PAGE).format(blog=blog, id=record.get("id")),
        "title": record.get("title") or "",
        "platform": "vkvideolive",
        "kind": kind,
        "channel": owner.get("displayName") or owner.get("nick") or blog,
        "duration": record.get("duration"),
        "thumbnail": record.get("previewUrl") or "",
        "timestamp": record.get("startTime") or record.get("videoCreatedAt") or record.get("createdAt"),
        "view_count": (record.get("count") or {}).get("views"),
        "is_live": False,
        "live_status": "was_live",
    }


def list_records(blog, limit=30, start=1, session=None, kind="vods"):
    """
    Страница записей (kind vods) или клипов (clips) канала: limit штук начиная
    со start (с единицы), в виде ответа inspect.
    """
    blog = blog.strip().lstrip("@")
    clips = kind == "clips"
    params = {"limit": limit, "offset": max(0, start - 1)}
    if clips:
        params["order"] = "created"          # новые сверху, как у записей
    try:
        response = (session or requests).get((CLIPS if clips else RECORDS).format(blog=requests.utils.quote(blog)),
                                             params=params, headers=HEADERS, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise VkRecordsError(tr("VK Video Live не ответил: {error}", error=error.__class__.__name__))
    if response.status_code == 404:
        raise VkRecordsError(tr("канал {name} на VK Video Live не найден", name=blog))
    if response.status_code >= 400:
        raise VkRecordsError(tr("VK Video Live не отдал записи: HTTP {code}", code=response.status_code))
    try:
        records = (response.json().get("data") or {}).get("clips" if clips else "records") or []
    except ValueError:
        raise VkRecordsError(tr("VK Video Live ответил непонятно"))
    return {
        "type": "playlist",
        "title": "",
        "url": "https://live.vkvideo.ru/%s/%s" % (blog, "clips" if clips else "records"),
        "platform": "vkvideolive",
        # ещё не готовая запись (идёт эфир или обработка) и удалённый клип не скачаются
        "entries": [describe(record, blog, "clip" if clips else "vod") for record in records
                    if isinstance(record, dict) and record.get("id")
                    and record.get("processState", "done") == "done" and record.get("state", "active") == "active"
                    and not record.get("deletedAt")],
    }
