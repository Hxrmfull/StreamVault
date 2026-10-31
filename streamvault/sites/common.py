# -*- coding: utf-8 -*-
"""
Общее для модулей площадок: открытые запросы так же, как их делает сайт
у зрителя без входа, и короткая память ответов.
"""

import threading
import time

import requests

from ..i18n import tr
from ..platforms import channel_id

TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault",
           "Accept": "application/json"}


class SiteError(Exception):
    """Площадка не ответила или ответила непонятно: текст — для человека."""


def get_json(method, url, **kwargs):
    """Ответ площадки как JSON; 404 — None (такого нет), прочие сбои — SiteError."""
    kwargs.setdefault("timeout", TIMEOUT)
    kwargs.setdefault("headers", HEADERS)
    try:
        response = requests.request(method, url, **kwargs)
    except requests.exceptions.RequestException as error:
        raise SiteError(tr("нет связи ({error})", error=error.__class__.__name__))
    if response.status_code == 404:
        return None
    if not response.ok:
        raise SiteError(tr("платформа ответила {status}", status=response.status_code))
    try:
        return response.json()
    except ValueError:
        raise SiteError(tr("платформа ответила непонятно"))


def search_result(platform, channel, name, avatar="", followers=None, live=None, viewers=None,
                  game=""):
    """Канал в выдаче поиска — одинаково у всех площадок."""
    return {"id": channel_id(platform, channel), "platform": platform, "channel": channel,
            "name": name or channel, "avatar": avatar or "", "followers": followers,
            "live": live, "viewers": viewers, "game": game or ""}


def https(url):
    """Адрес картинки: «//host/…» — к https, всё не https — пусто."""
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url if url.lower().startswith("https://") else ""


class Memo:
    """
    Ответы по ключу на seconds секунд, в том числе «такого нет» (None).
    Сбой (исключение) не запоминается: спросим снова в следующий раз.
    """

    LIMIT = 500

    def __init__(self, seconds, clock=time.monotonic):
        self._seconds = seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._values = {}

    def get(self, key, fetch):
        with self._lock:
            known = self._values.get(key)
            if known and self._clock() - known[0] < self._seconds:
                return known[1]
        value = fetch()
        with self._lock:
            self._values[key] = (self._clock(), value)
            while len(self._values) > self.LIMIT:
                self._values.pop(next(iter(self._values)))
        return value

    def known(self, key):
        """Есть ли свежий ответ (может быть и None)."""
        with self._lock:
            known = self._values.get(key)
            return bool(known) and self._clock() - known[0] < self._seconds

    def peek(self, key):
        with self._lock:
            known = self._values.get(key)
            return known[1] if known and self._clock() - known[0] < self._seconds else None


def unix_time(text):
    """'2026-10-31T06:16:30Z', '2026-10-31 08:22:30' (UTC) -> секунды; непонятное — None."""
    import datetime
    try:
        moment = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()
