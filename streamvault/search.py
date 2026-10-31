# -*- coding: utf-8 -*-
"""
Поиск каналов по имени на всех площадках сразу. Строка сверху и окно
«Добавить канал» показывают выдачу каждой площадки — писать kick:имя
не нужно. Как ищет каждая площадка — в её модуле (streamvault/sites):
без своих ключей и так же, как это делают сами сайты.
"""

import logging
import threading
import time

from . import sites
from .i18n import tr
from .sites.common import SiteError as SearchError

LIMIT = 5
# Сколько помнить ответ: пока печатают и стирают, те же запросы не повторяются.
CACHE_SECONDS = 120


class ChannelSearch:
    """
    Поиск по площадкам с короткой памятью ответов. tools — чем звать yt-dlp
    (DownloadManager: youtube_channels и ytdlp_json): YouTube ищет он.
    """

    def __init__(self, tools=None):
        self._tools = tools
        self._lock = threading.Lock()
        self._cache = {}

    def platforms(self):
        return [platform for platform in sites.SITES
                if sites.capabilities(platform)["search"] is True]

    def search(self, platform, query, limit=LIMIT):
        """Каналы площадки по имени: список search_result(); не ответила — SearchError."""
        query = str(query).strip()
        if platform not in self.platforms():
            raise ValueError(tr("поиск по {platform} недоступен", platform=platform))
        if len(query) < 2:
            return []
        key = (platform, query.lower(), limit)
        with self._lock:
            cached = self._cache.get(key)
            if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
                return [dict(item) for item in cached[1]]
        try:
            found = sites.search(platform, query, limit, self._tools)
        except SearchError:
            raise
        except Exception as error:
            # чужой ответ неожиданного вида не должен ронять окно
            logging.debug("поиск %s «%s»: %r", platform, query, error)
            raise SearchError(tr("платформа ответила непонятно"))
        with self._lock:
            self._cache[key] = (time.monotonic(), found)
            if len(self._cache) > 200:
                self._cache.pop(next(iter(self._cache)))
        return [dict(item) for item in found]
