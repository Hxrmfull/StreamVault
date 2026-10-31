# -*- coding: utf-8 -*-
"""Шина событий: через неё окно программы узнаёт о том, что происходит в движке."""

import logging
import threading
import time

from .i18n import tr


class EventBus:
    """
    Рассылка событий движка подписчикам. Окно программы узнаёт об изменениях
    сразу, а не перечитывает состояние по таймеру.

    Событие — словарь с полями "type" и "time" плюс данные события.
    Подписчики вызываются в том потоке, где случилось событие, поэтому
    должны быть быстрыми. Их ошибки уходят в лог и остальным не мешают.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers = []

    def subscribe(self, callback):
        """Подписывает callback(event). Возвращает функцию отписки."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe():
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
        return unsubscribe

    def emit(self, kind, **data):
        event = {"type": kind, "time": time.time()}
        event.update(data)
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                logging.exception(tr("подписчик на события {kind} упал", kind=kind))
