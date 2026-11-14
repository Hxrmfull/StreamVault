# -*- coding: utf-8 -*-
"""
Чат прямого эфира Kick, VK Video Live и YouTube — так же, как у Twitch
(livechat.py): анонимно, обычными открытыми запросами, как страница канала
у зрителя без входа.

- Kick — websocket Centrifugo: гостевой токен и адрес выдаёт сам Kick.
- VK Video Live — последние сообщения чата раз в несколько секунд.
- YouTube — yt-dlp: он умеет читать чат идущего эфира и дописывает его
  в файл, а программа читает этот файл по мере роста.

Сообщение пишется частями: [[текст], [название смайла, id смайла], …] —
картинки смайлов и значков livechat.convert встраивает в .chat.json.
"""

import collections
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.parse import quote, urlsplit

import requests
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect as ws_connect

from .. import netproxy, util
from ..platforms import stream_url
from .livechat import ChatReader, TwitchChat

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault",
           "Accept": "application/json"}
REQUEST_TIMEOUT = 15
# Сообщения, отправленные раньше этого (с)… до начала чтения, — уже не эфир:
# VK и YouTube сперва отдают хвост чата за прошлые минуты.
CLOCK_SLACK = 2
# Сколько id уже записанных сообщений помнить, чтобы не писать их дважды.
SEEN_LIMIT = 5000


class _Seen:
    """Последние SEEN_LIMIT id: повтор после переподключения не пишется."""

    def __init__(self):
        self._order = collections.deque()
        self._ids = set()

    def add(self, item_id):
        """True — впервые."""
        if item_id in self._ids:
            return False
        self._ids.add(item_id)
        self._order.append(item_id)
        if len(self._order) > SEEN_LIMIT:
            self._ids.discard(self._order.popleft())
        return True

    def __contains__(self, item_id):
        return item_id in self._ids


def _short_id(text):
    """Короткий постоянный id картинки по её адресу."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------ Kick

KICK_CHANNEL = "https://kick.com/api/v2/channels/{slug}"
KICK_CONNECTION = "https://web.kick.com/api/v1/realtime/channels/{id}/chat/connection"
KICK_AUTH = "https://web.kick.com/api/v1/realtime/auth/connection"
KICK_EMOTE = "https://files.kick.com/emotes/{id}/fullsize"
KICK_EMOTE_TAG = re.compile(r"\[emote:(\d+):([^\]]*)\]")
KICK_MESSAGE = "App\\Events\\ChatMessageEvent"
KICK_DELETED = "App\\Events\\MessageDeletedEvent"
KICK_BANNED = "App\\Events\\UserBannedEvent"


# На сколько секунд часы зрителя могут расходиться с часами Kick, чтобы им верить.
KICK_CLOCK_SLACK = 3


def _kick_time(message):
    """
    Когда отправлено сообщение. created_at ставит Kick — с точностью до секунды;
    metadata.message_ref — миллисекунды отправки, но по часам зрителя. А они врут:
    у кого на секунды, у кого на часы (не тот часовой пояс) — такое сообщение
    вставало в чате записи на два часа позже, и чат выходил длиннее видео.
    Поэтому message_ref только уточняет секунду created_at, пока сходится с ним.
    В истории чата metadata приходит строкой JSON.
    """
    metadata = message.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    ref = str((metadata if isinstance(metadata, dict) else {}).get("message_ref") or "")
    try:
        created = datetime.datetime.fromisoformat(message["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, AttributeError):
        created = time.time()
    if ref.isdigit() and len(ref) == 13 and abs(int(ref) / 1000 - created) <= KICK_CLOCK_SLACK:
        return int(ref) / 1000
    return created


def kick_subscriber_badges(channel):
    """Значки подписчиков канала: [(месяцев, адрес картинки)] по возрастанию."""
    badges = []
    for badge in channel.get("subscriber_badges") or ():
        try:
            badges.append((int(badge["months"]), badge["badge_image"]["src"]))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(badges)


def parse_kick_message(message, subscriber_badges=()):
    """ChatMessageEvent Kick -> (запись, смайлы, значки) для ChatReader._emit."""
    sender = message.get("sender") or {}
    identity = sender.get("identity") or {}
    content = message.get("content") or ""
    parts, emotes, position = [], [], 0
    for match in KICK_EMOTE_TAG.finditer(content):
        if match.start() > position:
            parts.append([content[position:match.start()]])
        emote_id, name = "kick-" + match.group(1), match.group(2)
        parts.append([name, emote_id])
        emotes.append((emote_id, name, KICK_EMOTE.format(id=match.group(1))))
        position = match.end()
    if position < len(content) or not parts:
        parts.append([content[position:]])

    badges = []
    # значок подписчика — свой у каждого канала, по числу месяцев
    months = next((badge.get("count") for badge in identity.get("badges") or ()
                   if badge.get("type") == "subscriber"), None)
    if months and subscriber_badges:
        fitting = [url for need, url in subscriber_badges if need <= months] or [subscriber_badges[0][1]]
        badges.append(("kick-sub-" + _short_id(fitting[-1]), "Subscriber", fitting[-1]))
    for badge in sorted(identity.get("badges_v2") or (), key=lambda badge: badge.get("sort_order") or 0):
        url = badge.get("image_url")
        if url:
            title = badge.get("name") or ""
            level = (badge.get("metadata") or {}).get("level")
            badges.append(("kick-" + _short_id(url), "%s %s" % (title, level) if level else title, url))

    record = {"type": "msg", "ts": _kick_time(message), "id": str(message.get("id") or uuid.uuid4().hex),
              "user_id": str(sender.get("id") or ""), "login": sender.get("slug") or "",
              "name": sender.get("username") or "", "color": identity.get("color") or "",
              "badges": [badge[0] for badge in badges], "parts": parts, "text": content, "system": ""}
    return record, emotes, badges


class KickChat(ChatReader):
    """Чат Kick: гостевое подключение к его websocket, как у страницы канала."""

    platform = "kick"
    ERRORS = ChatReader.ERRORS + (WebSocketException, ValueError, KeyError, TypeError)
    # Centrifugo шлёт пустой пинг раз в 25 с
    IDLE_TIMEOUT = 90

    def __init__(self, channel, session=None, connect=None):
        """session — requests; connect(адрес) -> websocket. Тесты подставляют свои."""
        super().__init__(channel)
        self._http = session or netproxy.session("kick")
        self._ws_connect = connect or self._connect
        self._ws = None
        self._subscriber_badges = []

    def _interrupt(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    @staticmethod
    def _connect(url):
        """Websocket чата; с прокси для Kick — через него (сам websockets прокси не знает)."""
        options = {}
        if netproxy.for_platform("kick"):
            parts = urlsplit(url)
            options["sock"] = netproxy.connect(parts.hostname, parts.port or (443 if parts.scheme == "wss" else 80),
                                               timeout=REQUEST_TIMEOUT, platform="kick")
        return ws_connect(url, open_timeout=REQUEST_TIMEOUT, close_timeout=2, max_size=2 ** 22, **options)

    def _json(self, method, url, **kwargs):
        headers = dict(HEADERS, **{"x-app-platform": "web"})
        response = self._http.request(method, url, headers=headers, timeout=REQUEST_TIMEOUT, **kwargs)
        response.raise_for_status()
        return response.json()

    def _session(self):
        channel = self._json("GET", KICK_CHANNEL.format(slug=quote(self.channel)))
        channel_id, chatroom = channel["id"], channel["chatroom"]["id"]
        self.room_id = str(channel_id)
        self._subscriber_badges = kick_subscriber_badges(channel)
        client = str(uuid.uuid4())
        connection = self._json("POST", KICK_CONNECTION.format(id=channel_id), json={
            "client": {"id": client, "type": "web"},
            "capabilities": {"accepted_providers": [{"provider": "centrifugo"}]}})
        url = connection["data"]["connections"][0]["credentials"]["url"]

        ws = self._ws_connect(url)
        self._ws = ws
        ws.send(json.dumps({"connect": {"token": self._token(client), "name": "js"}, "id": 1}))
        ws.send(json.dumps({"subscribe": {"channel": "chatrooms.%s.v2" % chatroom}, "id": 2}))
        self._refresh_at = None
        last_data = time.monotonic()
        while not self._stopping.is_set():
            if self._refresh_at is not None and time.monotonic() > self._refresh_at:
                # гостевой токен живёт минут 45: новый — до того, как сервер отключит
                self._refresh_at = None
                ws.send(json.dumps({"refresh": {"token": self._token(client)}, "id": 3}))
            try:
                raw = ws.recv(timeout=1)
            except TimeoutError:
                if time.monotonic() - last_data > self.IDLE_TIMEOUT:
                    raise OSError("idle")
                continue
            last_data = time.monotonic()
            self._mark_alive()
            for line in str(raw).splitlines():
                if line.strip():
                    self._on_frame(ws, json.loads(line))

    def _token(self, client):
        return self._json("POST", KICK_AUTH, json={"client_id": client})["data"]["token"]

    def _on_frame(self, ws, frame):
        if frame == {}:
            ws.send("{}")             # пинг Centrifugo — ответить пустым
            return
        if frame.get("error"):
            raise OSError("centrifugo: %s" % frame["error"])
        answer = frame.get("connect") or frame.get("refresh")
        if isinstance(answer, dict) and answer.get("expires") and answer.get("ttl"):
            self._refresh_at = time.monotonic() + max(30, int(answer["ttl"]) - 120)
            return
        data = ((frame.get("push") or {}).get("pub") or {}).get("data") or {}
        event, inner = data.get("event"), data.get("data")
        if isinstance(inner, str):
            inner = json.loads(inner)
        if not isinstance(inner, dict):
            return
        if event == KICK_MESSAGE:
            self._emit(*parse_kick_message(inner, self._subscriber_badges))
        elif event == KICK_DELETED and (inner.get("message") or {}).get("id"):
            self._write({"type": "delete", "id": str(inner["message"]["id"])})
        elif event == KICK_BANNED and (inner.get("user") or {}).get("id"):
            self._write({"type": "delete_user", "user_id": str(inner["user"]["id"])})


# ------------------------------------------------------------ VK Video Live

VK_CHAT = "https://api.live.vkvideo.ru/v1/blog/{name}/public_video_stream/chat"
# Цвета ников чата VK Video Live: nickColor — номер в этом списке.
VK_NICK_COLORS = ("#D66E34", "#B8AAFF", "#1D90FF", "#9961F9", "#59A840", "#E73629", "#DE6489", "#20BBA1",
                  "#F8B301", "#0099BB", "#7BBEFF", "#E542FF", "#A36C59", "#8BA259", "#00A9FF", "#A20BFF")


def _vk_text(content):
    """Текст VK — JSON-строка '["текст","unstyled",[]]'."""
    try:
        value = json.loads(content)
    except (TypeError, ValueError):
        return content if isinstance(content, str) else ""
    return value[0] if isinstance(value, list) and value and isinstance(value[0], str) else ""


def parse_vk_message(message):
    """Сообщение VK Video Live -> (запись, смайлы, значки) для ChatReader._emit."""
    author = message.get("author") or {}
    parts, emotes = [], []
    for block in message.get("data") or ():
        kind = block.get("type")
        if kind == "smile" and block.get("id"):
            emote_id, name = "vk-" + str(block["id"]), block.get("name") or ""
            parts.append([name, emote_id])
            emotes.append((emote_id, name, block.get("mediumUrl") or block.get("smallUrl")
                           or block.get("largeUrl") or ""))
        elif kind == "mention":
            parts.append([block.get("displayName") or block.get("nick") or block.get("name") or ""])
        elif kind == "link":
            parts.append([_vk_text(block.get("content")) or block.get("url") or ""])
        elif kind == "text":
            parts.append([_vk_text(block.get("content"))])
    parts = [part for part in parts if part[0]] or [[""]]

    badges = []
    for badge in list(author.get("roles") or ()) + list(author.get("badges") or ()):
        if not isinstance(badge, dict):
            continue
        url = badge.get("mediumUrl") or badge.get("smallUrl") or badge.get("largeUrl")
        if url:
            badges.append(("vk-" + str(badge.get("id") or _short_id(url)), badge.get("name") or "", url))
    color = author.get("nickColor")
    record = {"type": "msg", "ts": float(message.get("createdAt") or time.time()),
              "id": "vk-" + str(message.get("id")), "user_id": str(author.get("id") or ""),
              "login": (author.get("nick") or "").lower(), "name": author.get("displayName") or author.get("nick") or "",
              "color": VK_NICK_COLORS[color % len(VK_NICK_COLORS)] if isinstance(color, int) else "",
              "badges": [badge[0] for badge in badges], "parts": parts,
              "text": "".join(part[0] for part in parts), "system": ""}
    return record, emotes, badges


class VkVideoChat(ChatReader):
    """
    Чат VK Video Live: последние сообщения раз в POLL секунд. Новые — по id,
    он растёт; пропустить ничего нельзя: если за паузу сообщений больше,
    чем в ответе, берётся ответ подлиннее.
    """

    platform = "vkvideolive"
    ERRORS = ChatReader.ERRORS + (ValueError, KeyError, TypeError)
    POLL = 3
    LIMIT = 100
    LIMIT_CATCH_UP = 300

    def __init__(self, channel, session=None):
        super().__init__(channel)
        self._http = session or netproxy.session("vkvideolive")
        self._since = time.time() - CLOCK_SLACK
        self._last_id = None
        self._written = _Seen()
        self._deleted = set()

    def _fetch(self, limit):
        response = self._http.get(VK_CHAT.format(name=quote(self.channel)), params={"limit": limit},
                                  headers=HEADERS, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        return [message for message in response.json().get("data") or () if isinstance(message.get("id"), int)]

    def _session(self):
        while not self._stopping.is_set():
            messages = self._fetch(self.LIMIT)
            # ответ дошёл до уже записанного (или короче предела) — пропущенного нет
            caught_up = len(messages) < self.LIMIT or (
                self._last_id is not None and min(message["id"] for message in messages) <= self._last_id)
            if not caught_up and self._last_id is not None:
                messages = self._fetch(self.LIMIT_CATCH_UP)
                caught_up = (len(messages) < self.LIMIT_CATCH_UP
                             or min(message["id"] for message in messages) <= self._last_id)
            # после разрыва связи VK отдаёт и то, что написали за это время: разрыв отмечается,
            # только если сообщений было больше, чем он отдаёт за раз (было: всегда —
            # и чат записи показывал пропуск там, где все сообщения на месте)
            self._mark_alive(lost=not caught_up)
            self._take(messages)
            self._stopping.wait(self.POLL)

    def _take(self, messages):
        for message in sorted(messages, key=lambda message: message["id"]):
            key = "vk-%d" % message["id"]
            if message.get("isDeleted"):
                # удалили после того, как сообщение уже записано
                if key in self._written and key not in self._deleted:
                    self._deleted.add(key)
                    self._write({"type": "delete", "id": key})
                continue
            if self._last_id is not None and message["id"] <= self._last_id:
                continue
            self._last_id = message["id"]
            if (message.get("createdAt") or 0) < self._since or message.get("isPrivate"):
                continue
            self._written.add(key)
            self._emit(*parse_vk_message(message))


# ------------------------------------------------------------ YouTube

# Цвета имён в чате YouTube: владелец, модераторы, спонсоры.
YOUTUBE_COLORS = {"OWNER": "#FFD600", "MODERATOR": "#5E84F1", "MEMBER": "#2BA640"}
# Временная папка yt-dlp на время чтения чата. Оставшиеся после аварийного
# закрытия программы убираются при следующем чтении, если старше суток.
TEMP_PREFIX = "sv-ytchat-"
STALE_AFTER = 24 * 3600


def _remove_stale_folders():
    for folder in glob.glob(os.path.join(glob.escape(tempfile.gettempdir()), TEMP_PREFIX + "*")):
        try:
            if time.time() - os.path.getmtime(folder) > STALE_AFTER:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            continue


def _runs(value):
    """Текст YouTube: {"simpleText": …} или {"runs": [{"text": …} | {"emoji": …}]} -> части."""
    if not isinstance(value, dict):
        return [], []
    if "simpleText" in value:
        return [[value["simpleText"]]], []
    parts, emotes = [], []
    for run in value.get("runs") or ():
        if "text" in run:
            parts.append([run["text"]])
            continue
        emoji = run.get("emoji") or {}
        if not emoji.get("isCustomEmoji"):
            # обычный эмодзи — просто символ в тексте
            parts.append([emoji.get("emojiId") or ""])
            continue
        thumbnails = (emoji.get("image") or {}).get("thumbnails") or ()
        shortcut = next(iter(emoji.get("shortcuts") or ()), "") or emoji.get("emojiId", "")
        emote_id = "yt-" + _short_id(emoji.get("emojiId") or shortcut)
        url = thumbnails[-1].get("url", "") if thumbnails else ""
        parts.append([shortcut, emote_id])
        emotes.append((emote_id, shortcut, "https:" + url if url.startswith("//") else url))
    return parts, emotes


def _plain(value):
    return "".join(part[0] for part in _runs(value)[0])


def parse_youtube_action(action):
    """
    Одно действие чата YouTube -> ("msg", запись, смайлы, значки),
    ("delete", id), ("delete_user", id канала) или None.
    """
    deleted = action.get("markChatItemAsDeletedAction")
    if deleted and deleted.get("targetItemId"):
        return "delete", deleted["targetItemId"]
    banned = action.get("markChatItemsByAuthorAsDeletedAction")
    if banned and banned.get("externalChannelId"):
        return "delete_user", banned["externalChannelId"]
    item = (action.get("addChatItemAction") or {}).get("item") or {}
    kind = next((key for key in ("liveChatTextMessageRenderer", "liveChatPaidMessageRenderer",
                                 "liveChatPaidStickerRenderer", "liveChatMembershipItemRenderer")
                 if key in item), None)
    if kind is None:
        return None
    renderer = item[kind]
    parts, emotes = _runs(renderer.get("message"))
    system = ""
    if kind in ("liveChatPaidMessageRenderer", "liveChatPaidStickerRenderer"):
        amount = _plain(renderer.get("purchaseAmountText"))
        system = ("Super Sticker " if kind == "liveChatPaidStickerRenderer" else "Super Chat ") + amount
    elif kind == "liveChatMembershipItemRenderer":
        system = " ".join(filter(None, (_plain(renderer.get("headerPrimaryText")),
                                        _plain(renderer.get("headerSubtext")))))

    badges, color = [], ""
    for badge in renderer.get("authorBadges") or ():
        badge = badge.get("liveChatAuthorBadgeRenderer") or {}
        icon = (badge.get("icon") or {}).get("iconType")
        thumbnails = (badge.get("customThumbnail") or {}).get("thumbnails") or ()
        if icon in YOUTUBE_COLORS and not color:
            color = YOUTUBE_COLORS[icon]
        if thumbnails:
            # значок спонсора канала — своя картинка
            url = thumbnails[-1].get("url", "")
            color = color or YOUTUBE_COLORS["MEMBER"]
            badges.append(("yt-" + _short_id(url), badge.get("tooltip") or "", url))
    name = _plain(renderer.get("authorName"))
    try:
        sent = int(renderer["timestampUsec"]) / 1e6
    except (KeyError, TypeError, ValueError):
        sent = time.time()
    record = {"type": "msg", "ts": sent, "id": renderer.get("id") or uuid.uuid4().hex,
              "user_id": renderer.get("authorExternalChannelId") or "",
              "login": name.lstrip("@").lower(), "name": name, "color": color,
              "badges": [badge[0] for badge in badges], "parts": parts or [[""]],
              "text": "".join(part[0] for part in parts), "system": system}
    return "msg", record, emotes, badges


class YouTubeChat(ChatReader):
    """
    Чат YouTube: yt-dlp читает чат идущего эфира и дописывает его в файл
    построчно — программа читает этот файл по мере роста. yt-dlp вышел
    (эфир кончился, сбой) — запускается снова; повторы отсеиваются по id.
    """

    platform = "youtube"
    ERRORS = ChatReader.ERRORS + (ValueError,)
    POLL = 1
    # Сколько ждать первых строк чата после того, как yt-dlp создал файл: по ним видно,
    # покрыт ли разрыв связи. yt-dlp пишет их не сразу — бывает, секунд через 15 после
    # создания файла; а чат может и молчать — дольше не ждём.
    FIRST_LINES_WAIT = 30

    def __init__(self, channel, environment=None, command=None):
        """environment() — окружение yt-dlp (с обновлённой копией); command — сам yt-dlp."""
        super().__init__(channel)
        self._url = stream_url("youtube", channel)
        self._environment = environment
        self._command = list(command or util.YTDLP)
        self._process = None
        self._since = time.time() - CLOCK_SLACK
        self._seen = _Seen()
        # самое раннее сообщение, отданное этим запуском yt-dlp (с хвостом чата за прошлые минуты)
        self._oldest = None

    def _interrupt(self):
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            util.kill_process_tree(process)

    def _session(self):
        _remove_stale_folders()
        folder = tempfile.mkdtemp(prefix=TEMP_PREFIX)
        process = None
        try:
            log_path = os.path.join(folder, "yt-dlp.log")
            env = self._environment() if self._environment else util.child_environment()
            with open(log_path, "wb") as log:
                process = self._process = subprocess.Popen(
                    [*self._command, *netproxy.ytdlp_args(platform="youtube"),
                     "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                     "--no-part", "--no-warnings", "--color", "never", "--no-progress",
                     "-o", os.path.join(folder, "chat"), "--", self._url],
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=env, creationflags=util.subprocess_flags())
            self._follow(process, folder, log_path)
        finally:
            self._interrupt()
            if process is not None:
                # пока погашенный yt-dlp держит файлы, Windows их не удалит
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    pass
            shutil.rmtree(folder, ignore_errors=True)

    def _follow(self, process, folder, log_path):
        """Читает файл чата, пока yt-dlp его дописывает."""
        source, position, pending, appeared = None, 0, b"", None
        self._oldest = None
        while not self._stopping.is_set():
            finished = process.poll() is not None
            if source is None:
                source = next(iter(glob.glob(os.path.join(glob.escape(folder), "*.live_chat.json"))), None)
            if source is not None:
                with open(source, "rb") as chat:
                    chat.seek(position)
                    chunk = chat.read()
                position += len(chunk)
                *lines, pending = (pending + chunk).split(b"\n")
                for line in lines:
                    if line.strip():
                        self._on_line(json.loads(line.decode("utf-8", "replace")))
            if finished:
                raise OSError(self._last_error(log_path) or "yt-dlp: %s" % process.returncode)
            if source is not None:
                appeared = appeared or time.monotonic()
                # yt-dlp жив и пишет чат — связь есть, даже если чат молчит. Первые сообщения —
                # хвост чата за последние минуты: дошёл до разрыва связи — потерь нет. Пока
                # не разобрано ни одного (файл создан, строка ещё не дописана) — недолго ждём
                if self._oldest is not None or time.monotonic() - appeared > self.FIRST_LINES_WAIT:
                    self._mark_alive(lost=not self._covered())
            self._stopping.wait(self.POLL)

    def _covered(self):
        """
        После разрыва связи YouTube отдаёт и то, что написали за последние минуты: если
        этот хвост начинается раньше разрыва, сообщения за время разрыва уже в файле
        (было: участок всё равно отмечался как пропуск чата).
        """
        with self._lock:
            lost = self._lost
        return lost is not None and self._oldest is not None and self._oldest <= lost

    @staticmethod
    def _last_error(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as log:
                errors = [line.strip() for line in log if line.startswith("ERROR:")]
        except OSError:
            return ""
        return errors[-1] if errors else ""

    def _on_line(self, line):
        for action in (line.get("replayChatItemAction") or {}).get("actions") or ():
            parsed = parse_youtube_action(action)
            if parsed is None:
                continue
            kind = parsed[0]
            if kind == "delete":
                self._write({"type": "delete", "id": parsed[1]})
            elif kind == "delete_user":
                self._write({"type": "delete_user", "user_id": parsed[1]})
            else:
                record = parsed[1]
                if self._oldest is None or record["ts"] < self._oldest:
                    self._oldest = record["ts"]
                # хвост чата до начала записи и повторы после перезапуска yt-dlp — мимо
                if record["ts"] >= self._since and self._seen.add(record["id"]):
                    self._emit(record, parsed[2], parsed[3])


# ------------------------------------------------------------ какой чат у площадки

def make_chat(platform, channel, ytdlp_environment=None):
    """Чат эфира канала или None, если его читать не умеем, — по реестру площадок."""
    from .. import sites
    return sites.make_chat(platform, channel, ytdlp_environment=ytdlp_environment)
