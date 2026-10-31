# -*- coding: utf-8 -*-
"""Пересылка событий движков в окно программы."""

import logging
import threading
import time


class EventPump(threading.Thread):
    """
    Копит события шины и отдаёт их окну пачками.

    Прогресс загрузок приходит десятками в секунду, а каждый вызов JavaScript
    небесплатен: за четверть секунды из нескольких событий об одном канале
    или одной загрузке остаётся последнее. Раз в пару секунд к пачке добавляется
    размер идущих записей — файл растёт, не порождая событий.
    """

    # Сколько событий держать, если окно не забирает их (свёрнуто, ещё грузится).
    LIMIT = 2000

    def __init__(self, bus, live, sink, interval=0.25, tick=2.0):
        super().__init__(name="ui-events", daemon=True)
        self._live = live
        self._sink = sink
        self._interval = interval
        self._tick = tick
        self._lock = threading.Lock()
        self._events = []
        self._index = {}
        self._stop = threading.Event()
        self._unsubscribe = bus.subscribe(self._collect)

    @staticmethod
    def _key(event):
        if event["type"] == "channel":
            return ("channel", event["channel"]["id"])
        if event["type"] in ("download", "edit"):
            return (event["type"], event["job"]["id"])
        return None

    def _collect(self, event):
        key = self._key(event)
        with self._lock:
            if key is not None and key in self._index:
                self._events[self._index[key]] = event
                return
            if len(self._events) >= self.LIMIT:
                # окно давно не забирало события; при открытии оно всё равно
                # перечитает состояние целиком
                self._events, self._index = [], {}
            if key is not None:
                self._index[key] = len(self._events)
            self._events.append(event)

    def drain(self):
        with self._lock:
            events, self._events, self._index = self._events, [], {}
        return events

    def _recording_sizes(self):
        """Сколько записано у идущих записей: байты и сообщения чата эфира."""
        sizes, chats = {}, {}
        for channel in self._live.snapshot()["channels"]:
            recording = channel.get("recording")
            if recording:
                sizes[channel["id"]] = recording.get("bytes", 0)
                if recording.get("chat_messages") is not None:
                    chats[channel["id"]] = recording["chat_messages"]
        return sizes, chats

    def run(self):
        next_tick = 0.0
        while not self._stop.wait(self._interval):
            batch = self.drain()
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + self._tick
                sizes, chats = self._recording_sizes()
                if sizes:
                    batch.append({"type": "recording", "time": time.time(), "bytes": sizes,
                                  "chats": chats})
            if not batch:
                continue
            try:
                self._sink(batch)
            except Exception as error:
                # окно закрывается или ещё не готово: при открытии оно
                # перечитает состояние, потерянные события не страшны
                logging.debug("события в окно не ушли: %s", error)

    def stop(self):
        self._stop.set()
        self._unsubscribe()
