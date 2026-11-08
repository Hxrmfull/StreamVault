# -*- coding: utf-8 -*-
"""
Чат прошедшего эфира Twitch из публичного архива чатов.

logs.zonian.dev собирает логи чатов Twitch с публичных архивов (rustlog,
justlog) и отдаёт лучший; сам ничего не хранит. Это запасной путь: чат своей
записи, когда записи эфира на Twitch нет — скрыта или удалена — и
TwitchDownloader взять чат не может. Только Twitch: у Kick, YouTube и VK
Video Live таких архивов нет.

Архив — чужой сайт, и ему не верим на слово:
- адрес один, свой (HTTPS с проверкой сертификата), переадресации на другие
  адреса не принимаются, ник канала проверяется до запроса;
- сутки чата читаются построчно и не больше DAY_LIMIT байт; сообщения
  пишутся в файл кусками, в памяти не копятся; прошедшие сутки скачиваются
  один раз на все части записи (кэш на CACHE_TTL);
- каждая строка разбирается своим разборщиком IRC (тот же, что у чата
  прямого эфира), а не по готовым полям сайта; строка чужого канала или
  чужой комнаты, со временем вне записи или с испорченными тегами —
  отбрасывается; текст и ник — без управляющих символов и не длиннее
  положенного; цвет, значки, смайлы и id — только по своим образцам.
Итог — обычные записи чата (как у live/livechat.py) и из них .chat.json;
окно показывает их текстом, а картинки смайлов — по номерам с сайта Twitch.
"""

import datetime
import json
import os
import re
import threading
import time

import requests

from .. import util
from ..i18n import tr
from ..live.livechat import parse_line

BASE = "https://logs.zonian.dev"
SITE = "logs.zonian.dev"
# метка в video.description чата, взятого отсюда: TwitchDownloader её сохраняет, окно чата показывает
SOURCE_MARK = "StreamVault: chat from the public chat archive " + SITE
BEGINNING_MARK = "StreamVault: beginning chat from the public chat archive " + SITE
TIMEOUT = 30
HEADERS = {"User-Agent": "StreamVault (chat archive)"}
DAY_LIMIT = 3 * 1024 ** 3          # байт чата за сутки (~7–8 млн сообщений): больше — не похоже на архив
LIST_LIMIT = 2 * 1024 * 1024      # ответ со списком дней
LINE_LIMIT = 16 * 1024            # одна строка IRC; у Twitch — до ~8 КБ с тегами
TEXT_LIMIT = 1000                 # символов в сообщении (у Twitch — до 500)
NAME_LIMIT = 64
# записи — в файл кусками по столько: реже писать — без выигрыша (Windows и так
# копит запись в своём кэше), а в памяти больше этого не лежит
FLUSH_BYTES = 16 * 1024 * 1024
CACHE_TTL = 3 * 3600              # скачанные сутки живут столько: пока качаются части одной записи
LIST_TTL = 600                    # список дней канала помнится столько секунд

_LOGIN = re.compile(r"^[a-z0-9_]{1,25}$")
_DIGITS = re.compile(r"^\d{1,20}$")
_MESSAGE_ID = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_BADGE = r"[a-z0-9_-]{1,48}/[0-9A-Za-z_.-]{1,32}"
_BADGES = re.compile(r"^%s(,%s){0,30}$" % (_BADGE, _BADGE))
_EMOTE = r"[0-9A-Za-z_]{1,64}:\d{1,5}-\d{1,5}(,\d{1,5}-\d{1,5}){0,200}"
_EMOTES = re.compile(r"^%s(/%s){0,200}$" % (_EMOTE, _EMOTE))
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")

_lock = threading.Lock()
_days_cache = {}                  # канал -> (когда спросили, множество дней)
_day_locks = {}                   # (канал, сутки) -> замок: одни сутки качает одна часть


class ArchiveError(Exception):
    """Чат из архива не взять. Текст — для человека."""


class ArchiveStopped(Exception):
    """Загрузку остановили."""


def valid_channel(login):
    return bool(_LOGIN.match(str(login or "")))


def _clean(text, limit):
    """Текст без управляющих символов (и невидимых, меняющих направление письма), не длиннее limit."""
    return _CONTROL.sub("", str(text))[:limit]


def _get(path, stream=False):
    """Запрос к архиву: только свой адрес, без переадресаций."""
    try:
        response = requests.get(BASE + path, timeout=TIMEOUT, headers=HEADERS, stream=stream,
                                allow_redirects=False)
    except requests.RequestException as error:
        raise ArchiveError(tr("архив чатов не отвечает: {error}", error=error.__class__.__name__))
    if 300 <= response.status_code < 400:
        response.close()
        raise ArchiveError(tr("архив чатов отправил на другой адрес, переход отклонён"))
    return response


def available_days(channel):
    """
    Дни (UTC), за которые в архиве есть чат канала: множество datetime.date.
    Канала в архиве нет — пустое множество. Помнится LIST_TTL секунд.
    """
    if not valid_channel(channel):
        raise ArchiveError(tr("не похоже на ник канала Twitch: {name}", name=channel))
    with _lock:
        known = _days_cache.get(channel)
        if known and time.time() - known[0] < LIST_TTL:
            return set(known[1])
    with _get("/list?channel=" + channel, stream=True) as response:
        if response.status_code == 404:
            days = set()
        elif response.status_code != 200:
            raise ArchiveError(tr("архив чатов ответил ошибкой {code}", code=response.status_code))
        else:
            body = _read_limited(response, LIST_LIMIT)
            try:
                data = json.loads(body.decode("utf-8"))
                days = {datetime.date(int(day["year"]), int(day["month"]), int(day["day"]))
                        for day in data.get("availableLogs") or ()}
            except (ValueError, TypeError, KeyError, AttributeError):
                raise ArchiveError(tr("архив чатов ответил непонятно"))
    with _lock:
        _days_cache[channel] = (time.time(), set(days))
    return days


def _read_limited(response, limit):
    chunks, size = [], 0
    for chunk in response.iter_content(65536):
        size += len(chunk)
        if size > limit:
            raise ArchiveError(tr("ответ архива чатов слишком большой"))
        chunks.append(chunk)
    return b"".join(chunks)


def days_between(start, end):
    """Сутки UTC, которые задевает отрезок времени [start, end] (секунды эпохи)."""
    first = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).date()
    last = datetime.datetime.fromtimestamp(end, datetime.timezone.utc).date()
    days = []
    while first <= last:
        days.append(first)
        first += datetime.timedelta(days=1)
    return days


def fetch(channel, start, end, target, cache_dir=None, progress=None, stopped=lambda: False):
    """
    Сообщения канала с start до end (секунды эпохи) — в target (.chat.jsonl
    того же вида, что пишет чат прямого эфира: start, room, msg, delete,
    delete_user — по времени). Пишется кусками по FLUSH_BYTES: сколько бы
    сообщений ни было, память не растёт. cache_dir — где держать скачанные
    сутки, чтобы части одной записи не качали их заново (CACHE_TTL).
    progress(проценты) — по дням; stopped() — не остановили ли.
    Возвращает (сколько сообщений, id комнаты).
    """
    days = days_between(start, end)
    known = available_days(channel)
    wanted = [day for day in days if day in known]
    if not known:
        raise ArchiveError(tr("архив чатов не ведёт канал {name}", name=channel))
    if not wanted:
        raise ArchiveError(tr("в архиве чатов нет чата канала {name} за {date}",
                              name=channel, date=days[0].isoformat()))
    if cache_dir:
        _clean_cache(cache_dir)
    with _Sink(target, start, channel) as sink:
        for index, day in enumerate(wanted):
            if stopped():
                raise ArchiveStopped()
            if progress:
                progress(round(index * 100 / len(wanted)))
            for raw in _day_lines(channel, day, cache_dir, stopped):
                _take(raw, channel, start, end, sink)
    if progress:
        progress(100)
    return sink.messages, sink.room


class _Sink:
    """Записи чата — в файл кусками по FLUSH_BYTES, а не все сразу из памяти."""

    def __init__(self, target, start, channel):
        self._target = open(target, "w", encoding="utf-8")
        self._buffer, self._size = [], 0
        self.messages, self.room = 0, None
        self.add({"type": "start", "ts": start, "channel": channel, "platform": "twitch"})

    def add(self, record):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        self._buffer.append(line)
        self._size += len(line)
        if record["type"] == "msg":
            self.messages += 1
        if self._size >= FLUSH_BYTES:
            self.flush()

    def flush(self):
        self._target.write("".join(self._buffer))
        self._buffer, self._size = [], 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self.flush()
        finally:
            self._target.close()
        return False


def _day_lines(channel, day, cache_dir, stopped):
    """
    Строки IRC за сутки (bytes). Прошедшие сутки уже не меняются — они
    скачиваются в cache_dir один раз и читаются оттуда всеми частями записи;
    сегодняшние ещё дописываются — их всегда спрашиваем заново.
    """
    today = datetime.datetime.now(datetime.timezone.utc).date()
    if not cache_dir or day >= today:
        yield from _download_lines(channel, day, stopped)
        return
    cached = os.path.join(cache_dir, "%s-%s.irc" % (channel, day.isoformat()))
    with _day_lock(channel, day):
        if not os.path.isfile(cached):
            os.makedirs(cache_dir, exist_ok=True)
            partial = cached + ".part"
            try:
                with open(partial, "wb") as target:
                    for raw in _download_lines(channel, day, stopped):
                        target.write(raw + b"\n")
                os.replace(partial, cached)
            except BaseException:
                util.remove_quietly(partial)
                raise
    with open(cached, "rb") as source:
        for count, raw in enumerate(source):
            if count % 20000 == 0 and stopped():
                raise ArchiveStopped()
            if len(raw) <= LINE_LIMIT + 2:
                yield raw.rstrip(b"\n")


def _day_lock(channel, day):
    with _lock:
        return _day_locks.setdefault((channel, day), threading.Lock())


def _clean_cache(cache_dir):
    """Скачанные сутки старше CACHE_TTL — прочь: они нужны, пока качаются части одной записи."""
    try:
        names = os.listdir(cache_dir)
    except OSError:
        return
    for name in names:
        path = os.path.join(cache_dir, name)
        try:
            if time.time() - os.path.getmtime(path) > CACHE_TTL:
                os.remove(path)
        except OSError:
            pass


def _download_lines(channel, day, stopped):
    """Строки IRC за сутки прямо из архива — не больше DAY_LIMIT байт и LINE_LIMIT на строку."""
    path = "/channel/%s/%d/%d/%d?raw=1" % (channel, day.year, day.month, day.day)
    with _get(path, stream=True) as response:
        if response.status_code == 404:
            return
        if response.status_code != 200:
            raise ArchiveError(tr("архив чатов ответил ошибкой {code}", code=response.status_code))
        if not response.headers.get("Content-Type", "").startswith("text/plain"):
            raise ArchiveError(tr("архив чатов ответил непонятно"))
        size, rest = 0, b""
        for chunk in response.iter_content(262144):
            if stopped():
                raise ArchiveStopped()
            size += len(chunk)
            if size > DAY_LIMIT:
                raise ArchiveError(tr("ответ архива чатов слишком большой"))
            *lines, rest = (rest + chunk).split(b"\n")
            if len(rest) > LINE_LIMIT:
                rest = b""                       # строка без конца — не IRC
            for raw in lines:
                if 0 < len(raw) <= LINE_LIMIT:
                    yield raw
        if 0 < len(rest) <= LINE_LIMIT:
            yield rest


def _take(raw, channel, start, end, sink):
    """Одна строка IRC из архива — в записи чата, если она годится."""
    if not raw or len(raw) > LINE_LIMIT:
        return
    try:
        line = raw.decode("utf-8").rstrip("\r")
    except UnicodeDecodeError:
        return
    tags, command, params, text = parse_line(line)
    # строка чужого канала — не наша, что бы архив ни говорил
    if command not in ("PRIVMSG", "USERNOTICE", "CLEARMSG", "CLEARCHAT") or params[:1] != ["#" + channel]:
        return
    room_id = tags.get("room-id", "")
    if not _DIGITS.match(room_id):
        return
    if sink.room is None:
        sink.room = room_id
        sink.add({"type": "room", "room_id": room_id})
    elif room_id != sink.room:
        return                                   # другая комната под тем же ником — не та
    sent = tags.get("tmi-sent-ts", "")
    if not _DIGITS.match(sent):
        return
    moment = int(sent) / 1000
    if command == "CLEARMSG":
        if start <= moment <= end and _MESSAGE_ID.match(tags.get("target-msg-id", "")):
            sink.add({"type": "delete", "id": tags["target-msg-id"], "ts": moment})
        return
    if command == "CLEARCHAT":
        if start <= moment <= end and _DIGITS.match(tags.get("target-user-id", "")):
            sink.add({"type": "delete_user", "user_id": tags["target-user-id"], "ts": moment})
        return
    if not start <= moment <= end:
        return
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[len("\x01ACTION "):-1]       # /me
    message_id = tags.get("id", "")
    user_id = tags.get("user-id", "")
    if not _MESSAGE_ID.match(message_id) or not _DIGITS.match(user_id):
        return
    notice = command == "USERNOTICE"
    login = tags.get("login", "")
    sink.add({
        "type": "msg", "ts": moment, "id": message_id, "user_id": user_id,
        "login": login if _LOGIN.match(login) else "",
        "name": _clean(tags.get("display-name", ""), NAME_LIMIT),
        "color": tags.get("color", "") if _COLOR.match(tags.get("color", "")) else "",
        "badges": tags.get("badges", "") if _BADGES.match(tags.get("badges", "")) else "",
        # смайлы — по местам в тексте; текст обрезан — места за обрезом отбросит twitch_fragments
        "emotes": tags.get("emotes", "") if _EMOTES.match(tags.get("emotes", "")) else "",
        "bits": tags.get("bits", "") if _DIGITS.match(tags.get("bits", "")) else "",
        "text": _clean(text, TEXT_LIMIT),
        "system": _clean(tags.get("system-msg", ""), TEXT_LIMIT) if notice else "",
    })
