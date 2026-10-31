# -*- coding: utf-8 -*-
"""
Опрос каналов: в эфире ли канал прямо сейчас.

Способов два:
  * twitch  — Helix API по client_id/client_secret: дёшево, можно раз в 15 с;
  * прочие  — сам streamlink. Он одинаково отвечает для всех своих плагинов
    и сам разбирается с защитой вроде JS-проверки у Kick, так что писать
    по парсеру на платформу не нужно.
"""

import enum
import json
import logging
import re
import subprocess
import tempfile
import threading
import time

import requests

from .. import sites, util
from ..platforms import PLATFORMS, stream_url
from ..i18n import tr
# GQL сайта Twitch и аватарки без ключей живут в модуле площадки
from ..sites.twitch import GQL_CLIENT_ID, GQL_URL, THUMBNAIL_SIZE, PublicProfiles  # noqa: F401

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
USERS_URL = "https://api.twitch.tv/helix/users"
# Потолки для паузы между повторами.
NETWORK_RETRY_CAP = 60
API_RETRY_CAP = 300


class Status(enum.Enum):
    ONLINE = 0
    OFFLINE = 1
    NOT_FOUND = 2
    UNAUTHORIZED = 3
    NETWORK_ERROR = 4
    API_ERROR = 5


def live_thumbnail(platform, channel, meta):
    """
    Кадр идущего эфира, когда его не дал Helix, — от модуля площадки:
    Twitch держит его по имени канала, YouTube — по номеру трансляции,
    Kick и VK Video Live — в открытых сведениях о канале.
    """
    return sites.live_preview(platform, channel, meta)


def describe_stream(meta, platform=None, channel=None):
    """
    Сведения об идущем стриме для окна программы. Helix и streamlink называют
    одно и то же по-разному: game_name и category, user_name и author.
    platform и channel — чтобы найти кадр эфира, если в meta его нет.
    """
    meta = meta if isinstance(meta, dict) else {}
    thumbnail = meta.get("thumbnail_url") or ""
    if "{width}" in thumbnail:
        thumbnail = (thumbnail.replace("{width}", str(THUMBNAIL_SIZE[0]))
                              .replace("{height}", str(THUMBNAIL_SIZE[1])))
    if not thumbnail and platform:
        thumbnail = live_thumbnail(platform, channel, meta)
    return {
        "title": meta.get("title") or "",
        "game": meta.get("game_name") or meta.get("category") or "",
        "author": meta.get("user_name") or meta.get("author") or "",
        "viewers": meta.get("viewer_count"),
        "thumbnail": thumbnail,
        "started_at": meta.get("started_at") or "",
    }


class AccessToken:
    """App access token Twitch: общий на все потоки, обновляется до истечения срока."""

    REFRESH_MARGIN = 300

    def __init__(self, client_id, client_secret, stop_event):
        self._client_id = client_id
        self._client_secret = client_secret
        self._stop = stop_event
        self._lock = threading.Lock()
        self._value = None
        self._expires_at = 0.0
        # ключи отвергнуты навсегда: повторять бессмысленно, но и ронять
        # запись Kick или YouTube из-за чужой платформы незачем
        self.fatal = False

    def get(self, force_refresh=False):
        with self._lock:
            if self.fatal:
                return None
            if force_refresh:
                self._value = None
            if self._value is None or time.time() >= self._expires_at:
                self._fetch()
            return self._value

    def _fetch(self):
        delay = 5
        while not self._stop.is_set():
            try:
                # секрет — в теле запроса, а не в адресе: адрес целиком попадает
                # в отладочные логи urllib3, прокси и журналы серверов
                response = requests.post(
                    TOKEN_URL,
                    data={"client_id": self._client_id,
                          "client_secret": self._client_secret,
                          "grant_type": "client_credentials"},
                    timeout=15)
            except requests.exceptions.RequestException as error:
                logging.warning(tr("токен Twitch: сеть недоступна ({error}), повтор через "
                                   "{delay} с", error=error.__class__.__name__, delay=delay))
                self._stop.wait(delay)
                delay = min(delay * 2, API_RETRY_CAP)
                continue

            if response.status_code in (400, 401, 403):
                logging.critical(tr("токен Twitch: ключи приложения отклонены ({status}). "
                                    "Проверь Client ID и Client Secret в настройках",
                                    status=response.status_code))
                self.fatal = True
                return

            if not response.ok:
                logging.warning(tr("токен Twitch: ответ {status}, повтор через {delay} с",
                                   status=response.status_code, delay=delay))
                self._stop.wait(delay)
                delay = min(delay * 2, API_RETRY_CAP)
                continue

            try:
                payload = response.json()
                value = payload["access_token"]
                lifetime = int(payload.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError):
                logging.warning(tr("токен Twitch: непонятный ответ, повтор через {delay} с",
                                   delay=delay))
                self._stop.wait(delay)
                delay = min(delay * 2, API_RETRY_CAP)
                continue

            self._value = value
            self._expires_at = time.time() + max(60, lifetime - self.REFRESH_MARGIN)
            logging.info(tr("токен Twitch получен, действителен ~{days} сут",
                            days=round(lifetime / 86400, 1)))
            return


class TwitchProbe:
    """Статус канала через Helix API. Дёшево, поэтому годится для частого опроса."""

    name = "helix"

    def __init__(self, client_id, token):
        self._client_id = client_id
        self._token = token
        self._lock = threading.Lock()
        self._exists = {}   # логин -> есть ли такой канал вообще
        self._profiles = {}  # логин -> имя и аватарка для окна программы

    def is_unusable(self):
        """Ключи отвергнуты: этим опросчиком пользоваться больше нельзя."""
        return self._token.fatal

    def refresh_token(self):
        self._token.get(force_refresh=True)

    def disable(self):
        """Twitch раз за разом не принимает токен — дальше без Helix."""
        self._token.fatal = True

    def _headers(self):
        token = self._token.get()
        if token is None:
            return None
        return {"Client-ID": self._client_id, "Authorization": "Bearer " + token}

    def _channel_exists(self, login, headers):
        """
        Helix отвечает пустым списком и на "канал офлайн", и на "такого канала нет".
        Без этой проверки опечатка в нике выглядела бы как вечный офлайн.
        Ответ кэшируется: на канал — один запрос за всю работу программы.
        """
        with self._lock:
            if login in self._exists:
                return self._exists[login]
        try:
            response = requests.get(USERS_URL, params={"login": login},
                                    headers=headers, timeout=15)
            response.raise_for_status()
            exists = bool(response.json().get("data"))
        except (requests.exceptions.RequestException, ValueError):
            return True   # не знаем — не мешаем, спросим в другой раз
        with self._lock:
            self._exists[login] = exists
        return exists

    def profiles(self, logins):
        """
        Имена и аватарки каналов для окна программы. Helix отдаёт до 100
        каналов за запрос; ответ кэшируется на всё время работы программы.
        """
        logins = [login.lower() for login in logins]
        with self._lock:
            missing = [login for login in logins if login not in self._profiles]
        headers = self._headers() if missing else None
        for start in range(0, len(missing) if headers else 0, 100):
            chunk = missing[start:start + 100]
            try:
                response = requests.get(USERS_URL, params=[("login", login) for login in chunk],
                                        headers=headers, timeout=15)
                response.raise_for_status()
                users = response.json().get("data") or []
            except (requests.exceptions.RequestException, ValueError) as error:
                logging.debug("аватарки Twitch не получены: %s", error)
                break
            with self._lock:
                for user in users:
                    login = str(user.get("login", "")).lower()
                    self._profiles[login] = {
                        "name": user.get("display_name") or login,
                        "avatar": user.get("profile_image_url") or "",
                    }
                    self._exists[login] = True
        with self._lock:
            return {login: dict(self._profiles[login])
                    for login in logins if login in self._profiles}

    def check(self, platform, channel):
        headers = self._headers()
        if headers is None:
            return Status.NETWORK_ERROR, None

        try:
            response = requests.get(STREAMS_URL, params={"user_login": channel},
                                    headers=headers, timeout=15)
        except requests.exceptions.RequestException as error:
            logging.debug("%s: запрос не прошёл (%s)", channel, error)
            return Status.NETWORK_ERROR, None

        if response.status_code == 401:
            return Status.UNAUTHORIZED, None
        if response.status_code == 404:
            return Status.NOT_FOUND, None
        if response.status_code == 400:
            # так Helix отвечает на логин с недопустимыми символами
            return Status.NOT_FOUND, {"reason": tr("недопустимое имя канала Twitch "
                                                   "(только латиница, цифры и _)")}
        if response.status_code == 429:
            logging.warning(tr("{channel}: превышен лимит запросов Twitch", channel=channel))
            return Status.API_ERROR, None
        if not response.ok:
            logging.error(tr("{channel}: Twitch ответил {status}", channel=channel,
                             status=response.status_code))
            return Status.API_ERROR, None

        try:
            payload = response.json()
        except ValueError:
            logging.error(tr("{channel}: непонятный ответ Twitch", channel=channel))
            return Status.API_ERROR, None

        entries = payload.get("data") or []
        if entries:
            return Status.ONLINE, entries[0]
        if not self._channel_exists(channel, headers):
            return Status.NOT_FOUND, {"reason": tr("такого канала на Twitch нет")}
        return Status.OFFLINE, None


class StreamlinkProbe:
    """
    Статус канала через сам streamlink: если канал в эфире, он печатает
    список доступных качеств, если нет — ошибку, часто с причиной от плагина.

    Работает одинаково для всех платформ, включая те, где своими силами
    до API не достучаться: Kick, например, прячется за JS-проверкой.
    streamlink решает это внутри своих плагинов.
    """

    name = "streamlink"

    # Собственная жалоба плагина. Именно она отличает "канала нет в эфире"
    # от "платформа не отдаёт поток": вход, гео-блок, возрастное ограничение.
    _PLUGIN_ERROR = re.compile(r"^\[plugins\.[^\]]+\]\[error\]\s*(.+)$", re.M)

    _OFFLINE_MARKERS = ("no playable streams", "currently offline", "is offline",
                        "not live", "is not live")
    _NOT_FOUND_MARKERS = ("no plugin can handle", "unable to find channel",
                          "channel not found", "user not found", "404 client error")

    def __init__(self, timeout=180):
        self._timeout = timeout

    def is_unusable(self):
        return False

    def refresh_token(self):
        pass   # токена нет, обновлять нечего

    def disable(self):
        pass

    def _run(self, args):
        """
        Вывод пишется во временные файлы, а не в каналы. subprocess.run(timeout=)
        с каналами ждёт, пока их закроют все потомки, — а браузер, поднятый
        streamlink для Kick, держит их открытыми, и лимит переставал работать.
        Ожидание завершения самого процесса от этого не зависит.
        """
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen([*util.STREAMLINK, "--retry-open", "1", *args],
                                       stdout=out, stderr=err,
                                       stdin=subprocess.DEVNULL,
                                       env=util.child_environment(),
                                       creationflags=util.subprocess_flags())
            try:
                process.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                util.kill_process_tree(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                raise
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(
                process.args, process.returncode,
                out.read().decode("utf-8", "replace"),
                err.read().decode("utf-8", "replace"))

    def check(self, platform, channel):
        url = stream_url(platform, channel)
        # Опрашиваем без --json: в этом режиме streamlink глушит вывод плагинов,
        # и причина отказа не доходит вообще никак, даже с --loglevel error.
        try:
            result = self._run(["--loglevel", "info", url])
        except subprocess.TimeoutExpired:
            logging.warning(tr("{channel}: проверка не уложилась в {seconds} с",
                               channel=channel, seconds=self._timeout))
            return Status.NETWORK_ERROR, None
        except OSError as error:
            # streamlink пропал из PATH уже после старта
            logging.error(tr("{channel}: не удалось запустить streamlink ({error})",
                             channel=channel, error=error))
            return Status.NETWORK_ERROR, None

        output = "%s\n%s" % (result.stdout or "", result.stderr or "")

        if "Available streams:" in output:
            return Status.ONLINE, self._metadata(url)

        complaint = self._PLUGIN_ERROR.search(output)
        if complaint:
            return Status.API_ERROR, {"reason": complaint.group(1).strip()}

        lowered = output.lower()
        if any(marker in lowered for marker in self._NOT_FOUND_MARKERS):
            return Status.NOT_FOUND, None
        if any(marker in lowered for marker in self._OFFLINE_MARKERS):
            return Status.OFFLINE, None

        first_error = next((line for line in output.splitlines()
                            if line.lower().startswith("error:")), "")
        if first_error:
            return Status.API_ERROR, {"reason": first_error[len("error:"):].strip()}
        return Status.OFFLINE, None

    def _metadata(self, url):
        """Заголовок и категория стрима. Спрашиваются только когда канал в эфире."""
        try:
            result = self._run(["--json", url])
            return json.loads(result.stdout or "{}").get("metadata") or {}
        except (subprocess.TimeoutExpired, OSError, ValueError, AttributeError):
            return {}


def build_probes(settings, stop_event):
    """
    Опросчики по платформам. Twitch получает Helix, если заданы ключи,
    остальные — общий streamlink. Возвращает (по платформам, streamlink).
    """
    streamlink_probe = StreamlinkProbe(timeout=settings["probe_timeout"])
    probes = {}

    client_id = settings["twitch_client_id"]
    client_secret = settings["twitch_client_secret"]
    if client_id and client_secret:
        token = AccessToken(client_id, client_secret, stop_event)
        probes["twitch"] = TwitchProbe(client_id, token)
    else:
        logging.info(tr("ключи Twitch не заданы — каналы Twitch проверяются через streamlink"))

    for platform in PLATFORMS:
        probes.setdefault(platform, streamlink_probe)
    return probes, streamlink_probe
