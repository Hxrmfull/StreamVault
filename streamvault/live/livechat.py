# -*- coding: utf-8 -*-
"""
Чат прямого эфира во время записи.

Пока пишется эфир, программа читает чат канала анонимно — как зритель
без входа — и дописывает каждое сообщение в <запись>.chat.jsonl рядом
с частью записи, построчно: при сбое записанное не пропадёт. Когда часть
сконвертирована, из этого файла получается .chat.json в формате
TwitchDownloader — том же, что у чата записей, — а из него .chat.html и,
по желанию, видео чата (archive/chat.py).

Здесь — общая основа и чат Twitch (IRC); Kick, VK Video Live и YouTube —
в chatreaders.py. Смайлы и значки Twitch, 7TV, BTTV и FFZ TwitchDownloader
подставляет сам; смайлов и значков остальных площадок он не знает — они
встраиваются в .chat.json картинками, когда чат переводится в его формат.

Время сообщения — когда его отправили в чат; время в записи считается от
начала части. Видео эфира запаздывает за чатом на несколько секунд —
как у всех, кто смотрит трансляцию.
"""

import base64
import concurrent.futures
import datetime
import io
import json
import logging
import os
import queue
import random
import socket
import ssl
import threading
import time
import uuid

import requests

from .. import util
from ..archive import chat as archive_chat
from ..i18n import plural, tr
from ..settings import channel_setting
from ..util import remove_quietly

HOST, PORT = "irc.chat.twitch.tv", 6697
JSONL = ".chat.jsonl"
# Сколько молчания терпеть: Twitch шлёт PING раз в несколько минут.
IDLE_TIMEOUT = 360

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def jsonl_path(recorded):
    return os.path.splitext(recorded)[0] + JSONL


def unescape_tag(value):
    """Значение тега IRCv3: \\s — пробел, \\: — точка с запятой и так далее."""
    result, chars = [], iter(value)
    for char in chars:
        if char == "\\":
            following = next(chars, "")
            result.append(_TAG_ESCAPES.get(following, following))
        else:
            result.append(char)
    return "".join(result)


def parse_line(line):
    """'@tags :prefix COMMAND params :trailing' -> (теги, команда, параметры, текст)."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line[1:].partition(" ")
        for item in raw.split(";"):
            key, _, value = item.partition("=")
            tags[key] = unescape_tag(value)
    if line.startswith(":"):
        _, _, line = line.partition(" ")
    line, _, trailing = line.partition(" :")
    parts = line.split()
    return tags, (parts[0] if parts else ""), parts[1:], trailing


class ChatReader(threading.Thread):
    """
    Чат одного канала на время одного эфира. begin_part()/end_part() —
    куда писать: у каждой части записи свой .chat.jsonl. Наследник читает
    чат своей площадки в _session() — до обрыва или остановки — и отдаёт
    сообщения в _emit(); переподключается основа.
    """

    platform = ""
    RECONNECT_DELAYS = (1, 2, 5, 10, 30, 60)
    # обрывы связи — обычное дело: о них коротко, об остальном — со стеком
    ERRORS = (OSError, ssl.SSLError)

    def __init__(self, channel):
        super().__init__(name="chat-%s-%s" % (self.platform, channel), daemon=True)
        self.channel = channel
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._file = None
        # смайлы и значки, уже описанные в файле этой части
        self._described = set()
        self.room_id = None
        self.messages = 0

    # ------------------------------------------------------------ управление

    def begin_part(self, recorded, started):
        """Сообщения дальше — в .chat.jsonl этой части записи."""
        with self._lock:
            self._close_file()
            self._file = open(jsonl_path(recorded), "a", encoding="utf-8")
            self._described = set()
            self._write_locked({"type": "start", "ts": started, "platform": self.platform,
                                "channel": self.channel, "room_id": self.room_id})

    def end_part(self):
        with self._lock:
            # когда часть кончилась: по нему видно, насколько видео длиннее чата
            self._write_locked({"type": "end", "ts": time.time()})
            self._close_file()

    def stop(self):
        self._stopping.set()
        self.end_part()
        self._interrupt()

    def _interrupt(self):
        """Прервать идущий _session(): закрыть соединение, погасить процесс."""

    # ------------------------------------------------------------ запись

    def _close_file(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None

    def _write_locked(self, record):
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._file.flush()
        except OSError as error:
            logging.warning(tr("чат {channel}: не пишется ({error})", channel=self.channel,
                               error=error))
            self._close_file()

    def _write(self, record):
        with self._lock:
            self._write_locked(record)

    def _emit(self, record, emotes=(), badges=()):
        """
        Сообщение в файл части. emotes и badges — (id, название, адрес картинки)
        смайлов и значков из него: каждый описывается в файле части один раз,
        перед первым сообщением, где встретился.
        """
        with self._lock:
            for kind, items in (("emote", emotes), ("badge", badges)):
                for item_id, name, url in items:
                    if url and (kind, item_id) not in self._described:
                        self._described.add((kind, item_id))
                        self._write_locked({"type": kind, "id": item_id, "name": name, "url": url})
            self._write_locked(record)
        self.messages += 1

    # ------------------------------------------------------------ соединение

    def run(self):
        failures = 0
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                self._session()
            except self.ERRORS as error:
                if self._stopping.is_set():
                    break
                # долгий обрыв не засоряет журнал: пишется первая неудача подряд
                if not failures:
                    logging.info(tr("чат {channel}: связь прервалась ({error}), переподключаюсь",
                                    channel=self.channel, error=error.__class__.__name__))
            except Exception:
                if self._stopping.is_set():
                    break
                if not failures:
                    logging.exception(tr("чат {channel}: сбой, переподключаюсь", channel=self.channel))
            finally:
                self._interrupt()
            # долго проработавшее соединение — повод начать отсчёт пауз заново
            failures = 0 if time.monotonic() - started > 60 else failures + 1
            delay = self.RECONNECT_DELAYS[min(failures, len(self.RECONNECT_DELAYS) - 1)]
            self._stopping.wait(delay)

    def _session(self):
        raise NotImplementedError


class TwitchChat(ChatReader):
    """Чат Twitch: IRC, анонимный зритель justinfan."""

    platform = "twitch"

    def __init__(self, channel, connect=None):
        """connect(host, port) -> сокет; тесты подставляют свой сервер."""
        super().__init__(channel.lower().lstrip("#"))
        self._connect = connect or self._tls_connect
        self._socket = None

    def _interrupt(self):
        # оборвавшееся соединение закрываем сами, иначе каждое
        # переподключение оставляло бы открытый сокет
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _tls_connect(host, port):
        raw = socket.create_connection((host, port), timeout=15)
        return ssl.create_default_context().wrap_socket(raw, server_hostname=host)

    def _session(self):
        sock = self._connect(HOST, PORT)
        self._socket = sock
        sock.settimeout(1.0)
        # анонимный зритель: читать чат можно без входа в аккаунт
        nick = "justinfan%d" % random.randint(10000, 99999)
        for line in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE",
                     "NICK " + nick, "JOIN #" + self.channel):
            sock.sendall((line + "\r\n").encode("utf-8"))
        buffer, last_data = b"", time.monotonic()
        while not self._stopping.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if time.monotonic() - last_data > IDLE_TIMEOUT:
                    raise OSError("idle")
                continue
            if not chunk:
                raise OSError("closed")
            last_data = time.monotonic()
            buffer += chunk
            *lines, buffer = buffer.split(b"\r\n")
            for raw in lines:
                if raw:
                    self._on_line(sock, raw.decode("utf-8", "replace"))

    def _on_line(self, sock, line):
        tags, command, params, text = parse_line(line)
        if command == "PING":
            sock.sendall(("PONG :" + (text or "tmi.twitch.tv") + "\r\n").encode("utf-8"))
        elif command == "RECONNECT":
            raise OSError("reconnect")
        elif command == "ROOMSTATE" and tags.get("room-id"):
            if self.room_id != tags["room-id"]:
                self.room_id = tags["room-id"]
                self._write({"type": "room", "room_id": self.room_id})
        elif command in ("PRIVMSG", "USERNOTICE"):
            self._message(tags, text, command == "USERNOTICE")
        elif command == "CLEARMSG" and tags.get("target-msg-id"):
            self._write({"type": "delete", "id": tags["target-msg-id"]})
        elif command == "CLEARCHAT" and tags.get("target-user-id"):
            # бан или тайм-аут: сообщения зрителя из чата убираются
            self._write({"type": "delete_user", "user_id": tags["target-user-id"]})

    def _message(self, tags, text, notice):
        if text.startswith("\x01ACTION ") and text.endswith("\x01"):
            text = text[len("\x01ACTION "):-1]          # /me
        try:
            sent = int(tags.get("tmi-sent-ts", "")) / 1000
        except ValueError:
            sent = time.time()
        self._emit({"type": "msg", "ts": sent, "id": tags.get("id") or uuid.uuid4().hex,
                    "user_id": tags.get("user-id", ""), "login": tags.get("login", ""),
                    "name": tags.get("display-name", ""), "color": tags.get("color", ""),
                    "badges": tags.get("badges", ""), "emotes": tags.get("emotes", ""),
                    "bits": tags.get("bits", ""), "text": text,
                    "system": tags.get("system-msg", "") if notice else ""})


# ------------------------------------------------------------ в формат TwitchDownloader

def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def twitch_fragments(text, emotes):
    """Текст по кусочкам со смайлами Twitch: 'id:0-4,6-10/id2:12-15'."""
    ranges = []
    for group in filter(None, emotes.split("/")):
        emote_id, _, spans = group.partition(":")
        for span in filter(None, spans.split(",")):
            begin, _, end = span.partition("-")
            try:
                ranges.append((int(begin), int(end), emote_id))
            except ValueError:
                continue
    ranges.sort()
    fragments, emoticons, position = [], [], 0
    for begin, end, emote_id in ranges:
        if begin < position or end >= len(text):
            continue
        if begin > position:
            fragments.append({"text": text[position:begin], "emoticon": None})
        fragments.append({"text": text[begin:end + 1], "emoticon": {"emoticon_id": emote_id}})
        emoticons.append({"_id": emote_id, "begin": begin, "end": end})
        position = end + 1
    if position < len(text) or not fragments:
        fragments.append({"text": text[position:], "emoticon": None})
    return fragments, emoticons


def _badges(value, names):
    """
    Значки Twitch — строка 'имя/версия,…'; остальных площадок — список своих
    id, names — {id: имя встроенного значка} у тех, чья картинка скачалась.
    """
    if isinstance(value, list):
        return [{"_id": names[badge], "version": "1"} for badge in value if badge in names]
    badges = []
    for item in filter(None, (value or "").split(",")):
        name, _, version = item.partition("/")
        badges.append({"_id": name, "version": version})
    return badges


def _parts_fragments(parts, available):
    """
    Сообщение площадки, отличной от Twitch: [[текст], [название, id смайла], …].
    Смайл без картинки остаётся текстом. -> (текст, фрагменты, смайлы).
    """
    body, fragments, emoticons = "", [], []
    for part in parts:
        text = str(part[0]) if part else ""
        emote = part[1] if len(part) > 1 else None
        if emote is not None and emote in available and text:
            fragments.append({"text": text, "emoticon": {"emoticon_id": emote}})
            emoticons.append({"_id": emote, "begin": len(body), "end": len(body) + len(text) - 1})
        elif fragments and fragments[-1]["emoticon"] is None:
            fragments[-1]["text"] += text
        else:
            fragments.append({"text": text, "emoticon": None})
        body += text
    return body, fragments or [{"text": "", "emoticon": None}], emoticons


# Смайлы Twitch — 28 точек в высоту при обычном масштабе (56 и 112 —
# imageScale 2 и 4); значки TwitchDownloader хранит по 36 точек.
EMOTE_HEIGHT = 28
BADGE_SIZE = 36
IMAGE_TIMEOUT = 15
IMAGE_WORKERS = 8
IMAGE_LIMIT = 4 * 1024 * 1024
IMAGE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault"}
# Без единого встроенного значка TwitchDownloader идёт за значками на Twitch —
# и падает на канале, которого там нет. Прозрачная точка 1×1, ничья.
PLACEHOLDER_BADGE = {"name": "streamvault", "versions": {"1": {
    "title": "", "description": "",
    "bytes": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="}}}


def fetch_image(url):
    """Картинка смайла или значка. Не скачалась или слишком большая — None."""
    if not str(url).startswith("https://"):
        return None
    try:
        with requests.get(url, timeout=IMAGE_TIMEOUT, headers=IMAGE_HEADERS, stream=True) as response:
            if response.status_code != 200:
                return None
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > IMAGE_LIMIT:
                    return None
                chunks.append(chunk)
    except (requests.RequestException, OSError):
        return None
    return b"".join(chunks) or None


def _emote_entry(emote_id, name, data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        width, height = image.size
    scale = max(1, round(height / EMOTE_HEIGHT))
    return {"id": emote_id, "imageScale": scale, "data": base64.b64encode(data).decode("ascii"),
            "name": name, "width": max(1, round(width / scale)), "height": max(1, round(height / scale))}


def _badge_entry(badge_id, title, data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        frame = image.convert("RGBA")
    width = max(1, round(frame.width * BADGE_SIZE / frame.height))
    output = io.BytesIO()
    frame.resize((width, BADGE_SIZE), Image.LANCZOS).save(output, "PNG")
    return {"name": badge_id, "versions": {"1": {"title": title, "description": title,
                                                  "bytes": base64.b64encode(output.getvalue()).decode("ascii")}}}


def _embed(emotes, badges, fetch):
    """
    Картинки смайлов и значков внутрь .chat.json — как у чата записей,
    скачанного со смайлами. emotes и badges — {id: (название, адрес)}.
    """
    jobs = [("emote", item_id, name, url) for item_id, (name, url) in sorted(emotes.items())]
    jobs += [("badge", item_id, name, url) for item_id, (name, url) in sorted(badges.items())]
    embedded = {"thirdParty": [], "firstParty": [], "twitchBadges": [], "twitchBits": []}
    images = []
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(min(IMAGE_WORKERS, len(jobs))) as pool:
            images = list(pool.map(lambda job: fetch(job[3]), jobs))
    for (kind, item_id, name, _), data in zip(jobs, images):
        if not data:
            continue
        try:
            if kind == "emote":
                embedded["firstParty"].append(_emote_entry(item_id, name, data))
            else:
                embedded["twitchBadges"].append(_badge_entry(item_id, name, data))
        except Exception:
            # не картинка или формат, которого Pillow не знает: без неё
            continue
    if not embedded["twitchBadges"]:
        embedded["twitchBadges"].append(PLACEHOLDER_BADGE)
    return embedded


def _badge_names(badges):
    """
    {id: (название, адрес)} -> {id: имя}. Имя значка TwitchDownloader
    показывает подсказкой в HTML — пусть это будет название, а не id.
    """
    names, taken = {}, {PLACEHOLDER_BADGE["name"]}
    for badge_id, (title, _) in sorted(badges.items()):
        name, number = title or badge_id, 2
        while name in taken:
            name, number = "%s %d" % (title or badge_id, number), number + 1
        taken.add(name)
        names[badge_id] = name
    return names


# Запись с начала эфира (--hls-live-restart) начинается раньше, чем пошёл
# чат. Меньше этого — обычный разброс: streamlink начинает чуть позади эфира.
RESTART_SHIFT_MIN = 20


def part_times(source):
    """Когда часть началась и кончилась — по записям start и end в .chat.jsonl."""
    start = end = None
    try:
        with open(source, encoding="utf-8") as lines:
            for line in lines:
                if '"start"' not in line and '"end"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("type") == "start" and start is None:
                    start = record.get("ts")
                elif record.get("type") == "end":
                    end = record.get("ts")
    except OSError:
        pass
    return start, end


def video_backlog(duration, start, end):
    """
    Насколько видео части начинается раньше её чата, секунды. Видео длиннее,
    чем шла часть, — запись взяла эфир с начала, и чат надо сдвинуть на эту
    разницу. 0 — вровень.
    """
    if not duration or start is None or end is None:
        return 0.0
    backlog = duration - (end - start)
    return round(backlog, 3) if backlog > RESTART_SHIFT_MIN else 0.0


def convert(source, target, title="", channel="", fetch=fetch_image, shift=0.0):
    """
    .chat.jsonl -> .chat.json в формате TwitchDownloader. Возвращает число
    сообщений; без единого сообщения файл не создаётся (0). fetch(адрес) ->
    картинка смайла или значка площадок, кроме Twitch (тесты — свою).
    shift — на сколько секунд видео начинается раньше чата (video_backlog).
    """
    start, room_id, platform = None, None, "twitch"
    records, deleted, emotes, badges = [], set(), {}, {}
    with open(source, encoding="utf-8") as lines:
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue                 # оборванная последняя строка при сбое
            kind = record.get("type")
            if kind == "start":
                start = start if start is not None else record.get("ts")
                room_id = room_id or record.get("room_id")
                channel = channel or record.get("channel", "")
                platform = record.get("platform") or platform
            elif kind == "room":
                room_id = room_id or record.get("room_id")
            elif kind == "msg":
                records.append(record)
            elif kind == "delete":
                deleted.add(record.get("id"))
            elif kind == "delete_user":
                # бан: прячутся сообщения зрителя до него, после — нет
                deleted.update(earlier.get("id") for earlier in records
                               if earlier.get("user_id") == record.get("user_id"))
            elif kind in ("emote", "badge") and record.get("id") is not None:
                described = emotes if kind == "emote" else badges
                described[record["id"]] = (record.get("name") or "", record.get("url") or "")
    records = [record for record in records if record.get("id") not in deleted]
    if not records:
        return 0
    if start is None:
        start = records[0]["ts"]
    end = max(record["ts"] for record in records)
    length = max(1, int(round(end - start + shift)))

    embedded, have_emotes, badge_names = None, set(), {}
    if platform != "twitch":
        # только то, что встретилось в оставшихся сообщениях
        used_emotes = {part[1] for record in records for part in record.get("parts") or ()
                       if len(part) > 1 and part[1] in emotes}
        used_badges = {badge for record in records if isinstance(record.get("badges"), list)
                       for badge in record["badges"] if badge in badges}
        names = _badge_names({key: badges[key] for key in used_badges})
        embedded = _embed({key: emotes[key] for key in used_emotes},
                          {names[key]: badges[key] for key in used_badges}, fetch)
        have_emotes = {entry["id"] for entry in embedded["firstParty"]}
        embedded_badges = {entry["name"] for entry in embedded["twitchBadges"]}
        badge_names = {key: name for key, name in names.items() if name in embedded_badges}

    comments = []
    for record in records:
        system = record.get("system") or ""
        if "parts" in record:
            body, fragments, emoticons = _parts_fragments(record["parts"], have_emotes)
            if system:
                body = system + (" " + body if body else "")
                fragments, emoticons = [{"text": body, "emoticon": None}], []
        else:
            body = record.get("text", "")
            if system:
                body = system + (" " + body if body else "")
            fragments, emoticons = twitch_fragments(body, "" if system else record.get("emotes", ""))
        try:
            bits = int(record.get("bits") or 0)
        except ValueError:
            bits = 0
        login = record.get("login") or (record.get("name") or "").lower()
        comments.append({
            "_id": record.get("id"),
            "created_at": _iso(record["ts"]),
            "channel_id": room_id or "",
            "content_type": "video",
            "content_id": "",
            "content_offset_seconds": round(max(0.0, record["ts"] - start + shift), 3),
            "commenter": {"display_name": record.get("name") or login, "_id": str(record.get("user_id") or ""),
                          "name": login, "bio": None, "created_at": _iso(record["ts"]),
                          "updated_at": _iso(record["ts"]), "logo": None},
            "message": {"body": body, "bits_spent": bits, "fragments": fragments,
                        "user_badges": _badges(record.get("badges", ""), badge_names),
                        "user_color": record.get("color") or None, "emoticons": emoticons},
        })
    data = {
        "FileInfo": {"Version": {"Major": 1, "Minor": 4, "Patch": 0},
                     "CreatedAt": _iso(time.time()), "UpdatedAt": "0001-01-01T00:00:00"},
        "streamer": {"name": channel, "login": channel, "id": int(room_id) if str(room_id or "").isdigit() else 0},
        "clipper": None,
        "video": {"title": title, "description": None, "id": None, "created_at": _iso(start),
                  "start": 0, "end": length, "length": length, "viewCount": 0, "game": None,
                  "chapters": []},
        "comments": comments,
        # у Twitch смайлы и значки подставит TwitchDownloader; у остальных — уже здесь
        "embeddedData": embedded,
    }
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False)
    os.replace(temporary, target)
    return len(comments)


# ------------------------------------------------------------ после конвертации записи

class ChatFinisher(threading.Thread):
    """
    Чат части, которую только что сконвертировали: .chat.jsonl -> .chat.json
    рядом с видео — сразу: это быстро, даже когда надо скачать картинки
    смайлов (их немного, и качаются они параллельно). Затем .chat.html и, если выбрано,
    видео чата через TwitchDownloader — здесь, в своём потоке, чтобы не
    задерживать конвертацию следующих записей.

    Нет TwitchDownloader или программу закрыли раньше — .chat.json ждёт:
    rescan() подбирает чаты без HTML (при запуске и после установки
    TwitchDownloader).
    """

    def __init__(self, settings, ffmpeg, twitchdownloader, stop_event):
        super().__init__(name="live-chat", daemon=True)
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._twitchdownloader = twitchdownloader
        self._stopping = stop_event
        self._queue = queue.Queue()
        self._queued = set()
        self._lock = threading.Lock()
        self._process = None
        self._missing_reported = False

    def finish(self, recorded, video):
        """
        Вызывается очередью обработки, когда запись recorded стала файлом video —
        или, если её поделили на куски, списком файлов по порядку: тогда чат
        делится вместе с ней, у каждого куска свой, со временем от его начала.
        """
        # не при импорте: library тянет archive, а тот — live
        from ..library import parse_recording_name, probe_duration
        source = jsonl_path(recorded)
        if not os.path.exists(source):
            return
        videos = list(video) if isinstance(video, (list, tuple)) else [video]
        stem = os.path.splitext(os.path.basename(recorded))[0]
        lengths = [probe_duration(self._ffmpeg.path(), piece) for piece in videos]
        # запись взяла эфир с начала — чат сдвигается вслед за видео
        start, end = part_times(source)
        whole = sum(lengths) if all(lengths) else None
        shift = video_backlog(whole, start, end) if end else 0.0
        single = len(videos) == 1
        target = archive_chat.chat_paths(videos[0])["json"] if single else source[:-len(JSONL)] + ".whole.chat.json"
        try:
            count = convert(source, target, title=parse_recording_name(stem).get("title", ""), shift=shift)
        except (OSError, ValueError) as error:
            logging.error(tr("чат эфира не сохранился: {name} — {error}", name=stem, error=error))
            return
        remove_quietly(source)
        if not count:
            logging.info(tr("в чате эфира ни одного сообщения: {name}", name=stem))
            return
        logging.info(tr("чат эфира: {count} {messages} — {name}", count=count,
                        messages=plural(count, "сообщение|сообщения|сообщений"),
                        name=os.path.basename(target)))
        if single:
            self._enqueue(videos[0])
            return
        self._split(target, videos, lengths)

    def _split(self, whole, videos, lengths):
        """Чат поделённой записи — по кускам: границы — по настоящей длине каждого."""
        from ..editing import clip_chat
        begin = 0.0
        try:
            for number, (piece, length) in enumerate(zip(videos, lengths), 1):
                length = length or 0
                # последний кусок забирает и хвост чата после конца видео
                end = begin + length if number < len(videos) else float("inf")
                if clip_chat(whole, archive_chat.chat_paths(piece)["json"], begin, end, length):
                    self._enqueue(piece)
                begin += length
        except (OSError, ValueError) as error:
            logging.error(tr("чат не поделился по кускам записи: {error}", error=error))
        finally:
            remove_quietly(whole)

    def _enqueue(self, video):
        with self._lock:
            if video in self._queued:
                return
            self._queued.add(video)
        self._queue.put(video)

    def rescan(self):
        """
        Недоделанные чаты эфиров: нет HTML или, в режиме «чат + видео чата»,
        видео чата — программу закрыли раньше или не было TwitchDownloader.
        """
        for folder, _, names in os.walk(self._finished_dir()):
            for name in names:
                if not name.endswith(archive_chat.SUFFIXES["json"]):
                    continue
                base = os.path.join(folder, name[:-len(archive_chat.SUFFIXES["json"])])
                # видео чата бывает включено не у всех каналов
                render = self._mode(base) == archive_chat.RENDER
                if not os.path.exists(base + archive_chat.SUFFIXES["html"]) \
                        or render and not os.path.exists(base + archive_chat.SUFFIXES["video"]):
                    # запись могла остаться в .ts (автоконвертация выключена)
                    video = next((base + ext for ext in (".mp4", ".ts") if os.path.exists(base + ext)),
                                 base + ".mp4")
                    self._enqueue(video)

    def _mode(self, path):
        """Режим чата записи: свой у её канала или общий."""
        return channel_setting(self._settings, "live_chat",
                               *util.recording_channel(self._settings["root_path"], path))

    def _finished_dir(self):
        return os.path.join(self._settings["root_path"], util.FINISHED_DIR)

    def _platform_of(self, video):
        """Площадка записи — по папке: finished/<площадка>/<канал>/…"""
        try:
            relative = os.path.relpath(os.path.abspath(video), os.path.abspath(self._finished_dir()))
        except ValueError:              # другой диск
            return "twitch"
        return relative.split(os.sep)[0]

    def _track_process(self, process):
        self._process = process

    def _embed(self, cli, json_path, temp_dir):
        """True — смайлы встроены; False — нет (HTML скачает их сам); None — остановили."""
        step, temporary = archive_chat.embedding(cli, json_path, temp_dir)
        try:
            archive_chat.run([step], on_process=self._track_process, stopped=self._stopping.is_set)
            os.replace(temporary, json_path)
            return True
        except archive_chat.ChatStopped:
            return None
        except (archive_chat.ChatError, OSError) as error:
            remove_quietly(temporary)
            logging.info(tr("смайлы в чат эфира не встроены: {name} — {error}",
                            name=os.path.basename(json_path), error=error))
            return False
        finally:
            self._process = None

    def stop(self):
        self._stopping.set()
        process = self._process
        if process is not None:
            util.kill_process_tree(process)

    def run(self):
        self.rescan()
        while not self._stopping.is_set():
            try:
                video = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._render(video)
            except Exception:
                logging.exception(tr("чат эфира: сбой"))
            finally:
                with self._lock:
                    self._queued.discard(video)

    def _render(self, video):
        cli = self._twitchdownloader.command()
        if not cli:
            if not self._missing_reported:
                self._missing_reported = True
                logging.info(tr("чат эфира сохранён в .chat.json; HTML и видео чата появятся, "
                                "когда будет TwitchDownloader (настройки, раздел «Инструменты»)"))
            return
        mode = self._mode(video)
        ffmpeg = self._ffmpeg.path()
        if mode == archive_chat.RENDER and not ffmpeg:
            mode = archive_chat.TEXT
        size = (self._settings["chat_width"], self._settings["chat_height"],
                self._settings["chat_font_size"])
        temp_dir = os.path.join(os.path.dirname(os.path.abspath(self._settings.path)),
                                "cache", "twitchdownloader")
        paths = archive_chat.chat_paths(video)
        # смайлы и значки не Twitch уже внутри .chat.json: у Twitch их не искать
        embedded = self._platform_of(video) != "twitch"
        if not embedded and not os.path.exists(paths["html"]):
            # у Twitch — встроить: тогда HTML и видео чата делаются без сети,
            # а в проигрывателе окна видны смайлы 7TV, BTTV, FFZ и значки
            embedded = self._embed(cli, paths["json"], temp_dir)
            if embedded is None:
                return
        # HTML уже сделан в прошлый раз — осталось только видео чата
        steps = [(name, command) for name, command
                 in archive_chat.finishing(cli, video, mode, ffmpeg, size, temp_dir, embedded=embedded)
                 if not (name == "html" and os.path.exists(paths["html"]))]
        if not steps:
            return
        try:
            archive_chat.run(steps, on_process=self._track_process, stopped=self._stopping.is_set)
        except archive_chat.ChatStopped:
            return
        except archive_chat.ChatError as error:
            logging.warning(tr("чат эфира не доделан: {name} — {error}",
                               name=os.path.basename(paths["json"]), error=error))
            return
        finally:
            self._process = None
        logging.info(tr("чат эфира готов: {name}", name=os.path.basename(paths["html"])))
