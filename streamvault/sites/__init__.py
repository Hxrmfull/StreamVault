# -*- coding: utf-8 -*-
"""
Площадки: по модулю на каждую и общий реестр.

Каждый модуль отвечает на одни и те же вопросы — возможности (CAPABILITIES).
Чего площадка не умеет, записано в её UNSUPPORTED с причиной: так дыра
видна в коде, в окне и в README, а тест tests/test_sites.py не даёт
появиться молчаливой.

Модули площадок лёгкие: читатели чата и списки записей подтягиваются
внутри функций, чтобы не было циклов sites ↔ live ↔ archive.
"""

import concurrent.futures
import logging

from ..i18n import tr
from ..platforms import PLATFORMS, clean_profile, is_url
from . import kick, twitch, vkvideolive, youtube
from .common import Memo, SiteError

SITES = {"twitch": twitch, "kick": kick, "youtube": youtube, "vkvideolive": vkvideolive}

# Возможность -> как понять, что модуль её реализует.
CAPABILITIES = {
    "search": "поиск каналов по имени",
    "profile": "имя и аватарка канала",
    "live_preview": "кадр идущего эфира",
    "live_chat": "чат эфира",
    "live_restart": "запись с начала эфира",
    "listings": "вкладки страницы канала",
    "clips": "клипы канала",
    "recording_chat": "чат записей и клипов",
}

# Профили каналов меняются редко: помнятся несколько часов, «канала нет» — тоже.
PROFILE_SECONDS = 6 * 3600
# Сбой (нет связи) — не повод спрашивать снова при каждой отрисовке окна.
PROFILE_FAILURE_SECONDS = 300
PROFILE_WORKERS = 4


def site(platform):
    module = SITES.get(platform)
    if module is None:
        raise ValueError(tr("неизвестная платформа: {name}", name=platform))
    return module


def implements(module, capability):
    """Реализует ли модуль площадки возможность — по тому, что в нём есть."""
    if capability == "search":
        return callable(getattr(module, "search", None))
    if capability == "profile":
        return callable(getattr(module, "profile", None))
    if capability == "live_preview":
        return callable(getattr(module, "live_preview", None))
    if capability == "live_chat":
        return callable(getattr(module, "make_chat", None))
    if capability == "live_restart":
        return callable(getattr(module, "archive_for", None)) and \
            getattr(module, "BEGINNING", None) in ("live", "ended")
    if capability == "listings":
        return bool(getattr(module, "LISTINGS", None))
    if capability == "clips":
        return "clips" in (getattr(module, "LISTINGS", None) or {})
    if capability == "recording_chat":
        return bool(getattr(module, "CHAT_KINDS", None))
    raise ValueError(capability)


def unsupported(platform):
    """Чего площадка не умеет: {возможность: причина по-русски}."""
    return dict(getattr(site(platform), "UNSUPPORTED", {}))


def capabilities(platform):
    """{возможность: True или причина, почему её нет}."""
    module = site(platform)
    missing = unsupported(platform)
    return {name: True if implements(module, name) else missing.get(name, "")
            for name in CAPABILITIES}


# ------------------------------------------------------------ поиск и профили

def search(platform, query, limit, tools=None):
    """Выдача поиска; сбой площадки — SiteError."""
    return site(platform).search(query, limit, tools)


def search_note(platform):
    """Подсказка, когда поиск площадки ничего не нашёл (например, ищет только точное имя)."""
    return getattr(site(platform), "SEARCH_NOTE", "")


class Profiles:
    """
    Имена и аватарки каналов всех площадок для окна. Запросы идут
    параллельно, ответы помнятся PROFILE_SECONDS, «канала нет» — тоже.
    tools — чем звать yt-dlp (DownloadManager): профиль YouTube берёт он.
    """

    def __init__(self, tools=None, seconds=PROFILE_SECONDS):
        self._tools = tools
        self._found = Memo(seconds)
        self._failed = Memo(PROFILE_FAILURE_SECONDS)

    def profiles(self, pairs):
        """pairs — [(платформа, канал)] -> {(платформа, канал): {"name", "avatar"}}."""
        pairs = [(platform, channel) for platform, channel in dict.fromkeys(pairs)
                 if platform in SITES and channel]
        missing = [pair for pair in pairs
                   if not self._found.known(pair) and not self._failed.known(pair)]
        if missing:
            workers = min(PROFILE_WORKERS, len(missing))
            with concurrent.futures.ThreadPoolExecutor(workers) as pool:
                list(pool.map(self._fetch, missing))
        return {pair: dict(self._found.peek(pair)) for pair in pairs if self._found.peek(pair)}

    def _fetch(self, pair):
        platform, channel = pair
        try:
            self._found.get(pair, lambda: clean_profile(site(platform).profile(channel, self._tools)))
        except Exception as error:
            # нет связи или yt-dlp не ответил — окно обойдётся буквой
            logging.debug("профиль %s:%s не получен: %r", platform, channel, error)
            self._failed.get(pair, lambda: True)


# ------------------------------------------------------------ эфир

def live_preview(platform, channel, meta=None):
    """Кадр идущего эфира или пусто. Сеть не ответила — тоже пусто: рамка подождёт."""
    module = SITES.get(platform)
    if module is None:
        return ""
    try:
        return module.live_preview(channel, meta or {}) or ""
    except Exception as error:
        logging.debug("кадр эфира %s:%s не получен: %r", platform, channel, error)
        return ""


def make_chat(platform, channel, ytdlp_environment=None):
    """
    Чат эфира канала или None, если его читать не умеем. Канал ссылкой
    (например, трансляция YouTube) понимает только площадка с CHAT_FROM_URL.
    """
    module = SITES.get(platform)
    if module is None or not implements(module, "live_chat"):
        return None
    if is_url(channel) and not getattr(module, "CHAT_FROM_URL", False):
        return None
    return module.make_chat(channel, ytdlp_environment=ytdlp_environment)


def beginning_when(platform):
    """
    Когда площадка отдаёт начало эфира: live — сразу, из записи идущего эфира;
    ended — после конца эфира, из опубликованной записи; None — никак.
    """
    module = SITES.get(platform)
    return getattr(module, "BEGINNING", None) if module is not None and implements(module, "live_restart") else None


def stream_started(platform, channel, meta=None, tools=None):
    """Когда начался идущий эфир (секунды) или None; площадка не ответила — None."""
    try:
        return site(platform).stream_started(channel, meta, tools)
    except SiteError as error:
        logging.debug("начало эфира %s:%s не узнать: %s", platform, channel, error)
        return None


def archive_for(platform, channel, started, meta=None, tools=None):
    """Где взять начало эфира: (ссылка, "") или (None, почему нет)."""
    try:
        url, reason = site(platform).archive_for(channel, started, meta, tools)
    except SiteError as error:
        return None, str(error)
    return url, reason


def live_restart(platform):
    """Можно ли записать эфир с начала, если запись началась позже."""
    module = SITES.get(platform)
    return module is not None and implements(module, "live_restart")


# ------------------------------------------------------------ записи

def listings(platform):
    """Вкладки страницы канала: {вид: шаблон ссылки}."""
    module = SITES.get(platform)
    return dict(getattr(module, "LISTINGS", None) or {}) if module else {}


def own_lister(platform):
    """Свой способ получить список (если yt-dlp его не знает) или None."""
    return getattr(SITES.get(platform), "list_channel", None)


def enrich_listing(platform, channel, kind, result):
    """
    Дополнить список от yt-dlp тем, чего он не отдаёт (превью записей Kick).
    Площадка не ответила — список остаётся, какой есть.
    """
    enrich = getattr(SITES.get(platform), "enrich_listing", None)
    if enrich is None:
        return
    try:
        enrich(channel, kind, result)
    except SiteError as error:
        logging.debug("список %s:%s не дополнен: %s", platform, channel, error)


def recording_chat(platform, kind, live_status=""):
    """Бывает ли чат у загрузки этого вида."""
    module = SITES.get(platform)
    if module is None:
        return False
    return (kind in getattr(module, "CHAT_KINDS", ())
            and (live_status or "") not in getattr(module, "CHAT_SKIP_STATUS", ()))


def for_window():
    """
    Возможности площадок для окна: оно ничего не решает само. Причины
    переводятся сразу — окно показывает их как есть.
    """
    result = {}
    for platform in PLATFORMS:
        module = SITES[platform]
        result[platform] = {
            "name": module.NAME,
            "listings": list(listings(platform)),
            # вкладки со сроком («популярные за неделю»): клипы Twitch
            "periodic": [kind for kind, url in listings(platform).items() if "{period}" in url],
            "chat_kinds": list(getattr(module, "CHAT_KINDS", ())),
            "chat_skip_status": list(getattr(module, "CHAT_SKIP_STATUS", ())),
            "search_note": tr(search_note(platform)) if search_note(platform) else "",
            "unsupported": {name: tr(reason) for name, reason in unsupported(platform).items()},
        }
    return result


__all__ = ["SITES", "CAPABILITIES", "SiteError", "Profiles", "capabilities", "for_window",
           "listings", "live_preview", "make_chat", "own_lister", "recording_chat", "search",
           "site", "unsupported"]
