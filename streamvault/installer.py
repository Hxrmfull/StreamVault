# -*- coding: utf-8 -*-
"""
Скачивание с проверкой: общее для установки ffmpeg и обновления yt-dlp.

Файл качается с первого источника из списка. Источник не отвечает, рвёт
связь или отдаёт медленнее MIN_SPEED — загрузка переходит к следующему,
а если там тот же файл (та же контрольная сумма), продолжает с того же
места. Скачанное обязано совпасть с суммой, которую подкласс заранее
сверил в независимых местах; иначе на диске не остаётся ничего.

Запросы — только по https и только к известным адресам; перенаправление
проверяется до подключения.
"""

import collections
import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from urllib.parse import urljoin, urlsplit

import requests
import urllib3

from . import util
from .i18n import tr


class InstallError(Exception):
    """Установка не удалась. Текст — для человека."""


class HttpError(InstallError):
    """Сервер ответил ошибкой: status — её код (404 — такого нет)."""

    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


class Cancelled(Exception):
    pass


class _SourceTrouble(Exception):
    """Источник не отвечает, оборвался или медленный — пора к следующему."""

    def __init__(self, reason, slow=False):
        super().__init__(reason)
        self.reason = reason
        self.slow = slow


# Откуда качать: label — для человека, sha256 — сверенная заранее сумма,
# size — размер, если известен, headers — особые заголовки запроса.
Source = collections.namedtuple("Source", "label url sha256 size headers", defaults=(None, None))


def total_size(response, offset):
    """Полный размер файла из Content-Range ("bytes 100-199/1000") или Content-Length."""
    content_range = response.headers.get("Content-Range", "")
    if "/" in content_range and content_range.rsplit("/", 1)[1].isdigit():
        return int(content_range.rsplit("/", 1)[1])
    length = response.headers.get("Content-Length", "")
    return int(length) + offset if length.isdigit() else None


def agreed_sum(sums, problems):
    """
    Сумма sha256, сверенная в разных местах: хотя бы два совпадения и ни
    одного расхождения. sums — {где: сумма}, problems — что не удалось узнать.
    """
    for value in sums.values():
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise InstallError(tr("контрольная сумма в непонятном виде"))
    if len(set(sums.values())) > 1:
        raise InstallError(tr("контрольные суммы не совпадают ({sources}) — возможна подмена, "
                              "установка остановлена", sources=", ".join(sums)))
    if len(sums) < 2:
        raise InstallError(tr("не удалось сверить контрольную сумму в двух местах: {problems}",
                              problems="; ".join(problems) or ", ".join(sums)))
    return next(iter(sums.values()))


class Installer:
    """
    Скачивание и установка в фоне, с состоянием для окна:
    idle → checking → downloading → installing → done
    (или current — уже последняя версия, failed, cancelled).

    Подкласс задаёт NAME, URLS, ALLOWED_HOSTS и три шага:
      _work_dir()  — куда класть недокачанное (рядом с установленным);
      _plan()      — (версия, [Source…]) или None, если обновлять нечего;
      _install(путь, версия, источник) — проверить и поставить; вернуть путь.
    """

    NAME = ""
    URLS = {}
    ALLOWED_HOSTS = ()
    # страница для скачивания руками, если не вышло ничего
    MANUAL_PAGE = None
    MAX_SIZE = 400 * 1024 * 1024
    MAX_REDIRECTS = 5
    # данные читаются по мере прихода, до 256 КБ за раз
    CHUNK = 1 << 18
    # медленнее этого (байт/с) за SPEED_WINDOW секунд — пробуем другой источник
    MIN_SPEED = 256 * 1024
    SPEED_WINDOW = 20
    # сколько раз переподключаться к источнику после обрыва
    ATTEMPTS_PER_SOURCE = 2
    # как часто сообщать о прогрессе скачивания, секунды
    PROGRESS_INTERVAL = 0.3

    def __init__(self, on_change=None, urls=None, allowed_hosts=None, require_https=True):
        """urls и allowed_hosts подменяются в тестах."""
        self._on_change = on_change
        self._urls = dict(self.URLS, **(urls or {}))
        self._allowed_hosts = tuple(allowed_hosts or self.ALLOWED_HOSTS)
        self._require_https = require_https
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._state = {"state": "idle", "error": "", "version": None, "progress": None,
                       "source": None, "manual_page": self.MANUAL_PAGE}

    # ------------------------------------------------------------ управление

    def snapshot(self):
        with self._lock:
            return dict(self._state)

    def busy(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name=self.NAME + "-install",
                                            daemon=True)
            self._thread.start()

    def cancel(self):
        self._cancel.set()

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _set(self, **changes):
        with self._lock:
            self._state.update(changes)
            snapshot = dict(self._state)
        if self._on_change is not None:
            self._on_change(snapshot)

    # ------------------------------------------------------------ шаги подкласса

    def _work_dir(self):
        raise NotImplementedError

    def _plan(self):
        raise NotImplementedError

    def _install(self, path, version, source):
        raise NotImplementedError

    def _log_done(self, version, path):
        logging.info(tr("{name} {version} установлен: {path}", name=self.NAME, version=version, path=path))

    # ------------------------------------------------------------ установка

    def _run(self):
        partial = None
        try:
            self._set(state="checking", error="", progress=None, source=None)
            plan = self._plan()
            if plan is None:
                self._set(state="current", progress=None)
                return
            version, sources = plan
            self._set(state="downloading", version=version,
                      progress={"downloaded": 0, "total": sources[0].size, "speed": None})
            partial, source = self._download(sources)
            self._set(state="installing", source=source.label)
            path = self._install(partial, version, source)
            self._log_done(version, path)
            self._set(state="done", progress=None)
        except Cancelled:
            self._set(state="cancelled", progress=None)
        except InstallError as error:
            logging.error(tr("{name} не установлен: {error}", name=self.NAME, error=error))
            self._set(state="failed", error=str(error), progress=None)
        except Exception as error:
            logging.exception(tr("установка {name} упала", name=self.NAME))
            self._set(state="failed", error=tr("сбой программы: {error}", error=error),
                      progress=None)
        finally:
            if partial:
                util.remove_quietly(partial)

    def _get(self, url, stream=False, headers=None):
        """
        Запрос только по разрешённым адресам. Перенаправления разбираются здесь,
        а не в requests: адрес проверяется до подключения, и к чужому серверу
        программа не обращается вовсе.
        """
        headers = dict(headers or {}, **{"User-Agent": "StreamVault"})
        for _ in range(self.MAX_REDIRECTS + 1):
            self._check_url(url)
            try:
                response = requests.get(url, timeout=30, stream=stream, allow_redirects=False,
                                        headers=headers)
            except requests.exceptions.RequestException as error:
                raise InstallError(tr("нет связи с {host}: {error}", host=urlsplit(url).hostname,
                                      error=error.__class__.__name__))
            if response.is_redirect:
                url = urljoin(url, response.headers.get("Location", ""))
                response.close()
                continue
            if not response.ok:
                raise HttpError(tr("{host} ответил {status}", host=urlsplit(url).hostname,
                                   status=response.status_code), response.status_code)
            return response
        raise InstallError(tr("слишком много перенаправлений"))

    def _get_json(self, url):
        try:
            return self._get(url).json()
        except ValueError:
            raise InstallError(tr("{host} прислал непонятный ответ", host=urlsplit(url).hostname))

    def _check_url(self, url):
        parts = urlsplit(url)
        if (self._require_https and parts.scheme != "https") or parts.hostname not in self._allowed_hosts:
            raise InstallError(tr("перенаправление на чужой адрес ({url}) — туда программа "
                                  "не обращается", url=url))

    def _download(self, sources):
        """
        Скачивает во временный файл, сверяя сумму на лету. Возвращает
        (путь, источник). На последнем источнике скорость уже не проверяется.
        """
        directory = self._work_dir()
        os.makedirs(directory, exist_ok=True)
        handle, partial = tempfile.mkstemp(prefix=self.NAME + "-", suffix=".part", dir=directory)
        state = {"downloaded": 0, "digest": hashlib.sha256(), "size": None, "sha256": None}
        problems = []
        try:
            with os.fdopen(handle, "wb") as target:
                for index, source in enumerate(sources):
                    if source.sha256 != state["sha256"]:
                        # у этого источника другой файл — продолжать нечего
                        target.seek(0)
                        target.truncate()
                        state.update(downloaded=0, digest=hashlib.sha256(), size=source.size,
                                     sha256=source.sha256)
                    last = index == len(sources) - 1
                    for _ in range(self.ATTEMPTS_PER_SOURCE):
                        try:
                            self._fetch(source, target, state, check_speed=not last)
                        except _SourceTrouble as trouble:
                            problems.append("%s: %s" % (source.label, trouble.reason))
                            logging.warning(tr("{name}: {source} — {problem}, пробую дальше",
                                               name=self.NAME, source=source.label,
                                               problem=trouble.reason))
                            if trouble.slow:
                                break   # медленный не ускорится от переподключения
                            continue
                        self._verify(state)
                        return partial, source
            # одинаковые причины повторных попыток — один раз
            raise InstallError(tr("{name} не скачался ни с одного источника: {problems}",
                                  name=self.NAME, problems="; ".join(dict.fromkeys(problems))))
        except BaseException:
            # недокачанное или подменённое на диске не остаётся
            util.remove_quietly(partial)
            raise

    def _verify(self, state):
        if state["size"] is not None and state["downloaded"] != state["size"]:
            raise InstallError(tr("файл скачался не целиком: {got} из {size} байт",
                                  got=state["downloaded"], size=state["size"]))
        if state["digest"].hexdigest() != state["sha256"]:
            raise InstallError(tr("скачанный файл не совпал с контрольной суммой — "
                                  "возможна подмена, ничего не установлено"))

    def _fetch(self, source, target, state, check_speed):
        """
        Качает с одного источника, продолжая с уже скачанного. Источник
        не отвечает, оборвался или медленный — _SourceTrouble.
        """
        offset = state["downloaded"]
        headers = dict(source.headers or {})
        if offset:
            headers["Range"] = "bytes=%d-" % offset
        try:
            response = self._get(source.url, stream=True, headers=headers)
        except InstallError as error:
            raise _SourceTrouble(str(error))
        with response:
            if offset and response.status_code != 206:
                # сервер не умеет продолжать — этот файл с начала
                target.seek(0)
                target.truncate()
                state["downloaded"], state["digest"] = 0, hashlib.sha256()
            if state["size"] is None:
                state["size"] = total_size(response, state["downloaded"])
                if state["size"] is not None and state["size"] > self.MAX_SIZE:
                    raise InstallError(tr("{name}: подозрительный размер файла — {size} байт",
                                          name=self.NAME, size=state["size"]))
            self._set(source=source.label)
            window_start, window_bytes = time.monotonic(), 0
            reported, reported_bytes = time.monotonic(), state["downloaded"]
            try:
                for chunk in self._chunks(response):
                    if self._cancel.is_set():
                        raise Cancelled()
                    state["downloaded"] += len(chunk)
                    if state["downloaded"] > (state["size"] or self.MAX_SIZE):
                        raise InstallError(tr("файл больше заявленного — установка остановлена"))
                    state["digest"].update(chunk)
                    target.write(chunk)
                    window_bytes += len(chunk)
                    now = time.monotonic()
                    if now - reported >= self.PROGRESS_INTERVAL:
                        speed = (state["downloaded"] - reported_bytes) / (now - reported)
                        reported, reported_bytes = now, state["downloaded"]
                        self._set(progress={"downloaded": state["downloaded"],
                                            "total": state["size"], "speed": speed})
                    if now - window_start >= self.SPEED_WINDOW:
                        rate = window_bytes / (now - window_start)
                        if check_speed and rate < self.MIN_SPEED:
                            raise _SourceTrouble(tr("медленно: {speed} КБ/с", speed=int(rate / 1024)),
                                                 slow=True)
                        window_start, window_bytes = now, 0
            except (requests.exceptions.RequestException, urllib3.exceptions.HTTPError) as error:
                raise _SourceTrouble(tr("связь оборвалась ({error})", error=error.__class__.__name__))
        target.flush()
        if state["size"] is not None and state["downloaded"] < state["size"]:
            raise _SourceTrouble(tr("связь оборвалась на {got} из {size} байт",
                                    got=state["downloaded"], size=state["size"]))

    def _chunks(self, response):
        """
        Данные по мере прихода, до CHUNK байт за раз. iter_content ждал бы
        полного куска, и на медленной связи прогресс и проверка скорости
        замирали бы на десятки секунд.
        """
        while True:
            chunk = response.raw.read1(self.CHUNK, decode_content=True)
            if not chunk:
                return
            yield chunk
