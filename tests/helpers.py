# -*- coding: utf-8 -*-
"""Общие заглушки для тестов: опросчики, очередь обработки, сбор событий."""

import os
import sys
import threading
import time

from streamvault import util
from streamvault.live.probes import Status

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_STREAMLINK = [sys.executable, os.path.join(HERE, "fake_streamlink.py")]
FAKE_YTDLP = [sys.executable, os.path.join(HERE, "fake_ytdlp.py")]


class ScriptedProbe:
    """
    Отвечает по списку: (Status, meta) или исключение. Последний ответ
    повторяется бесконечно.
    """

    def __init__(self, *answers, name="helix"):
        self.name = name
        self._answers = list(answers)
        self._lock = threading.Lock()
        self.calls = 0
        self.refreshes = 0
        self.disabled = False

    def check(self, platform, channel):
        with self._lock:
            self.calls += 1
            answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def is_unusable(self):
        return self.disabled

    def refresh_token(self):
        self.refreshes += 1

    def disable(self):
        self.disabled = True


ONLINE = (Status.ONLINE, {"title": "тестовый стрим"})
OFFLINE = (Status.OFFLINE, None)


class FakeProcessing:
    """Очередь обработки, которая только запоминает, что ей отдали."""

    def __init__(self):
        self.submitted = []
        self.watched = []

    def watch(self, recorded_dir, processed_dir):
        self.watched.append((recorded_dir, processed_dir))

    def mark_active(self, path):
        pass

    def unmark_active(self, path):
        pass

    def submit(self, recorded, processed, delay=0, trusted=False, convert=False):
        self.submitted.append((recorded, processed, trusted))


class EventLog:
    """Подписчик шины, собирающий события."""

    def __init__(self, bus):
        self.events = []
        self._lock = threading.Lock()
        bus.subscribe(self._collect)

    def _collect(self, event):
        with self._lock:
            self.events.append(event)

    def of(self, kind):
        with self._lock:
            return [event for event in self.events if event["type"] == kind]

    def states(self, cid):
        return [event["channel"]["state"] for event in self.of("channel")
                if event["channel"]["id"] == cid]


def wait_for(condition, timeout=10.0, step=0.05):
    """Ждёт, пока condition() станет истинным. Возвращает итог последней проверки."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return bool(condition())


class FakeYtdlp:
    """
    Подменяет yt-dlp заглушкой на время теста. Режим можно менять на ходу
    через set_mode: следующий запуск yt-dlp возьмёт новый.
    """

    def __init__(self, mode="ok", info=None, log=None):
        self._values = {"FAKE_YTDLP_MODE": mode, "FAKE_YTDLP_INFO": info,
                        "FAKE_YTDLP_LOG": log}

    def set_mode(self, mode):
        os.environ["FAKE_YTDLP_MODE"] = mode

    def __enter__(self):
        self._saved_command = util.YTDLP
        self._saved_env = {key: os.environ.get(key) for key in self._values}
        util.YTDLP = FAKE_YTDLP
        for key, value in self._values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc):
        util.YTDLP = self._saved_command
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FakeStreamlink:
    """Подменяет streamlink заглушкой на время теста."""

    def __init__(self, mode):
        self._mode = mode

    def __enter__(self):
        self._saved_command = util.STREAMLINK
        self._saved_mode = os.environ.get("FAKE_STREAMLINK_MODE")
        util.STREAMLINK = FAKE_STREAMLINK
        os.environ["FAKE_STREAMLINK_MODE"] = self._mode
        return self

    def __exit__(self, *exc):
        util.STREAMLINK = self._saved_command
        if self._saved_mode is None:
            os.environ.pop("FAKE_STREAMLINK_MODE", None)
        else:
            os.environ["FAKE_STREAMLINK_MODE"] = self._saved_mode
