# -*- coding: utf-8 -*-
"""
Twitch. Без своих ключей — GQL сайта twitch.tv с его открытым Client-ID
(тем же, с которым ходят streamlink и yt-dlp): поиск и аватарки.
С ключами Helix (live/probes.py) опрашивает каналы и тоже даёт аватарки.
"""

import logging
import threading

import requests

from ..i18n import tr
from ..platforms import is_url
from .common import get_json, search_result, unix_time

NAME = "Twitch"

GQL_URL = "https://gql.twitch.tv/gql"
GQL_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
GQL_PROFILES = ("query($logins: [String!]) "
                "{ users(logins: $logins) { login displayName profileImageURL(width: 150) } }")
GQL_SEARCH = ("query($query: String!) { searchFor(userQuery: $query, platform: \"web\") "
              "{ channels { items { login displayName profileImageURL(width: 70) "
              "followers { totalCount } stream { viewersCount game { displayName } } } } } }")

# Размер превью стрима, который Twitch подставляет в шаблон ссылки.
THUMBNAIL_SIZE = (640, 360)
# Кадр идущего эфира по имени канала — то же, что на главной twitch.tv.
PREVIEW = "https://static-cdn.jtvnw.net/previews-ttv/live_user_{login}-{width}x{height}.jpg"

# Вкладки страницы канала: ссылки для yt-dlp. В клипах {period} — за какой
# срок брать самые просматриваемые: 24hr, 7d, 30d, all.
LISTINGS = {
    "vods": "https://www.twitch.tv/{channel}/videos?filter=archives&sort=time",
    "highlights": "https://www.twitch.tv/{channel}/videos?filter=highlights&sort=time",
    "uploads": "https://www.twitch.tv/{channel}/videos?filter=uploads&sort=time",
    "clips": "https://www.twitch.tv/{channel}/clips?filter=clips&range={period}",
}

# Чат бывает у записей эфиров (и хайлайтов — они из записей) и клипов.
CHAT_KINDS = ("vod", "clip")

# Запись с начала эфира: Twitch пишет запись эфира с первой секунды, пока
# он идёт (так перематывает и сайт), — начало берётся из неё сразу. Нет её
# только у канала, где выключен архив прошедших эфиров.
BEGINNING = "live"
GQL_STREAM = ("query($login: String!) { user(login: $login) "
              "{ stream { createdAt archiveVideo { id } } } }")
VIDEO = "https://www.twitch.tv/videos/{id}"

UNSUPPORTED = {}


def search(query, limit, tools=None):
    data = get_json("POST", GQL_URL, json={"query": GQL_SEARCH, "variables": {"query": query}},
                    headers={"Client-ID": GQL_CLIENT_ID})
    items = (((((data or {}).get("data") or {}).get("searchFor") or {}).get("channels") or {})
             .get("items") or [])
    found = []
    for item in items[:limit]:
        if not isinstance(item, dict) or not item.get("login"):
            continue
        stream = item.get("stream") or {}
        found.append(search_result("twitch", str(item["login"]).lower(), item.get("displayName"),
                                   item.get("profileImageURL"),
                                   (item.get("followers") or {}).get("totalCount"),
                                   live=bool(stream), viewers=stream.get("viewersCount"),
                                   game=(stream.get("game") or {}).get("displayName")))
    return found


class PublicProfiles:
    """
    Имена и аватарки каналов Twitch без своих ключей — тем же способом,
    каким их получает сайт twitch.tv. Ответ кэшируется на всё время работы
    программы, в том числе «такого канала нет».
    """

    BATCH = 100

    def __init__(self):
        self._lock = threading.Lock()
        self._profiles = {}     # логин -> {"name", "avatar"} или None, если канала нет

    def profiles(self, logins):
        logins = [login.lower() for login in logins]
        with self._lock:
            missing = [login for login in dict.fromkeys(logins) if login not in self._profiles]
        for start in range(0, len(missing), self.BATCH):
            chunk = missing[start:start + self.BATCH]
            try:
                response = requests.post(GQL_URL, json={"query": GQL_PROFILES,
                                                        "variables": {"logins": chunk}},
                                         headers={"Client-ID": GQL_CLIENT_ID}, timeout=15)
                response.raise_for_status()
                users = (response.json().get("data") or {}).get("users") or []
            except (requests.exceptions.RequestException, ValueError, AttributeError) as error:
                logging.debug("аватарки Twitch без ключей не получены: %s", error)
                break
            found = {}
            for user in users:
                if isinstance(user, dict) and user.get("login"):
                    login = str(user["login"]).lower()
                    found[login] = {"name": user.get("displayName") or login,
                                    "avatar": user.get("profileImageURL") or ""}
            with self._lock:
                for login in chunk:
                    self._profiles[login] = found.get(login)
        with self._lock:
            return {login: dict(self._profiles[login]) for login in logins
                    if self._profiles.get(login)}


_public = PublicProfiles()


def profile(channel, tools=None):
    """
    Имя и аватарка канала. Окно берёт их пачкой через LiveService.twitch_profiles
    (с ключами — Helix); это — тот же запрос сайта для одного канала.
    """
    return _public.profiles([channel]).get(channel.lower())


def live_preview(channel, meta):
    if not channel or is_url(channel):
        return ""
    return PREVIEW.format(login=channel.lower(), width=THUMBNAIL_SIZE[0], height=THUMBNAIL_SIZE[1])


def make_chat(channel, ytdlp_environment=None):
    from ..live.livechat import TwitchChat
    return TwitchChat(channel)


def _stream(channel):
    data = get_json("POST", GQL_URL, json={"query": GQL_STREAM, "variables": {"login": channel.lower()}},
                    headers={"Client-ID": GQL_CLIENT_ID})
    return ((((data or {}).get("data") or {}).get("user") or {}).get("stream")) or {}


def stream_started(channel, meta=None, tools=None):
    """Когда начался идущий эфир (секунды) или None."""
    if not channel or is_url(channel):
        return None
    return unix_time(_stream(channel).get("createdAt"))


def archive_for(channel, started, meta=None, tools=None):
    """Запись идущего эфира: (ссылка, "") или (None, почему её нет)."""
    if not channel or is_url(channel):
        return None, tr("канал задан ссылкой")
    video = _stream(channel).get("archiveVideo") or {}
    if not video.get("id"):
        return None, tr("у канала выключен архив прошедших эфиров")
    return VIDEO.format(id=video["id"]), ""
