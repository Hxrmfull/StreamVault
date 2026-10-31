# -*- coding: utf-8 -*-
"""
Чат записи для просмотра в окне программы.

.chat.json в формате TwitchDownloader — у записей эфиров и у скачанного
с Twitch — превращается в сообщения по времени записи и список картинок
смайлов и значков. Сообщение: [время в записи, имя, цвет, [значки], [части]];
часть — текст или [номер картинки, название смайла]. Картинки встроены
в .chat.json (их отдаёт сервер окна, ui/media.py); смайлы Twitch без
встроенной картинки берутся с сайта Twitch.

Смайлы 7TV, BTTV и FFZ сообщения не отмечают — как и TwitchDownloader,
их узнают по словам текста.
"""

import base64
import bisect
import json
import math
import re
import threading
import zlib

TWITCH_EMOTE = "https://static-cdn.jtvnw.net/emoticons/v2/{id}/default/dark/2.0"
# Цвета ников Twitch для тех, кто свой не выбрал: выбираются по имени.
DEFAULT_COLORS = ("#FF0000", "#0000FF", "#008000", "#B22222", "#FF7F50", "#9ACD32", "#FF4500",
                  "#2E8B57", "#DAA520", "#D2691E", "#5F9EA0", "#1E90FF", "#FF69B4", "#8A2BE2",
                  "#00FF7F")
# Ниже этой яркости ник на тёмном фоне не читается — он осветляется.
MIN_LUMINANCE = 0.22
_COLOR = re.compile(r"^#([0-9a-fA-F]{6})$")
_SPACES = re.compile(r"(\s+)")
# Сколько найденных сообщений отдавать окну за раз.
SEARCH_LIMIT = 200
# График чата под шкалой: примерно столько столбиков; всплесков — не больше PEAKS.
ACTIVITY_BARS = 240
PEAKS = 8
_IMAGE_TYPES = ((b"\x89PNG", "image/png"), (b"GIF8", "image/gif"), (b"\xff\xd8", "image/jpeg"))


def _luminance(rgb):
    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def readable_color(color, name=""):
    """Цвет ника для тёмного фона: свой или по имени, тёмный — светлее."""
    match = _COLOR.match(color or "")
    if not match:
        color = DEFAULT_COLORS[zlib.crc32(name.lower().encode("utf-8")) % len(DEFAULT_COLORS)]
        match = _COLOR.match(color)
    rgb = [int(match.group(1)[i:i + 2], 16) for i in (0, 2, 4)]
    for _ in range(10):
        if _luminance(rgb) >= MIN_LUMINANCE:
            break
        rgb = [round(value + (255 - value) * 0.1) for value in rgb]
    return "#%02X%02X%02X" % tuple(rgb)


def image_type(data):
    for magic, mime in _IMAGE_TYPES:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return "image/png"


class ChatReplay:
    """Чат одной записи. images — base64 встроенной картинки или адрес https."""

    def __init__(self, path):
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
        self.images = []
        self._urls = {}
        embedded = data.get("embeddedData") or {}

        first_party = {}
        for emote in embedded.get("firstParty") or ():
            if emote.get("data") and emote.get("id") is not None:
                first_party[str(emote["id"])] = self._add(emote["data"])
        third_party = {}
        for emote in embedded.get("thirdParty") or ():
            if emote.get("data") and emote.get("name"):
                third_party.setdefault(emote["name"], self._add(emote["data"]))
        badges = {}
        for badge in embedded.get("twitchBadges") or ():
            for version, value in (badge.get("versions") or {}).items():
                if isinstance(value, dict) and value.get("bytes") and badge.get("name"):
                    badges[(badge["name"], str(version))] = self._add(value["bytes"])

        try:
            # у отрезка записи Twitch время сообщений — от начала всей записи
            start = float((data.get("video") or {}).get("start") or 0)
        except (TypeError, ValueError):
            start = 0.0
        messages = []
        for comment in data.get("comments") or ():
            try:
                offset = float(comment["content_offset_seconds"]) - start
            except (KeyError, TypeError, ValueError):
                continue
            commenter = comment.get("commenter") or {}
            message = comment.get("message") or {}
            name = commenter.get("display_name") or commenter.get("name") or ""
            user_badges = [badges.get((badge.get("_id"), str(badge.get("version"))))
                           for badge in message.get("user_badges") or () if isinstance(badge, dict)]
            parts = []
            for fragment in message.get("fragments") or [{"text": message.get("body") or ""}]:
                text = fragment.get("text") or ""
                emote_id = str((fragment.get("emoticon") or {}).get("emoticon_id") or "")
                if emote_id:
                    index = first_party.get(emote_id)
                    if index is None:
                        index = self._add_url(TWITCH_EMOTE.format(id=emote_id))
                    parts.append([index, text])
                else:
                    parts.extend(self._split(text, third_party))
            messages.append([round(max(0.0, offset), 3), name, readable_color(message.get("user_color"), name),
                             [index for index in user_badges if index is not None], _merge(parts)])
        messages.sort(key=lambda message: message[0])
        self.messages = messages
        self._times = [message[0] for message in messages]

    def _add(self, data):
        self.images.append(data)
        return len(self.images) - 1

    def _add_url(self, url):
        if url not in self._urls:
            self._urls[url] = self._add(url)
        return self._urls[url]

    @staticmethod
    def _split(text, third_party):
        if not third_party:
            return [text]
        return [[third_party[word], word] if word in third_party else word for word in _SPACES.split(text)]

    def window(self, start, end):
        """Сообщения со временем в записи от start (включительно) до end."""
        return self.messages[bisect.bisect_left(self._times, start):bisect.bisect_left(self._times, end)]

    def search(self, text, limit=SEARCH_LIMIT):
        """
        Сообщения, где есть text — в нике, тексте или названии смайла, без
        учёта регистра. Не больше limit: окно показывает их списком.
        """
        needle = str(text).strip().casefold()
        if not needle:
            return []
        found = []
        for message in self.messages:
            words = [message[1]] + [part if isinstance(part, str) else part[1] for part in message[4]]
            if needle in " ".join(words).casefold():
                found.append(message)
                if len(found) >= limit:
                    break
        return found

    def activity(self, duration):
        """График чата: {"bucket": секунд в столбике, "counts": [...], "peaks": [номера столбиков]}."""
        bucket = activity_bucket(duration)
        counts = activity(self._times, duration, bucket)
        return {"bucket": bucket, "counts": counts, "peaks": peaks(counts)}

    def image(self, index):
        """Встроенная картинка: (байты, тип) или None."""
        if not 0 <= index < len(self.images) or self.images[index].startswith("https://"):
            return None
        try:
            data = base64.b64decode(self.images[index])
        except ValueError:
            return None
        return data, image_type(data)


def activity(times, duration, bucket):
    """Сколько сообщений в каждом отрезке по bucket секунд: [число, …] от начала записи."""
    count = max(1, math.ceil(max(duration or 0, times[-1] if times else 0) / bucket))
    counts = [0] * count
    for offset in times:
        counts[min(count - 1, max(0, int(offset // bucket)))] += 1
    return counts


def activity_bucket(duration):
    """Ширина столбика графика чата: около ACTIVITY_BARS столбиков, круглыми секундами."""
    raw = max(1.0, (duration or 0) / ACTIVITY_BARS)
    for step in (10, 15, 20, 30, 60, 90, 120, 180, 300, 600):
        if raw <= step:
            return step
    return 600 * math.ceil(raw / 600)


def peaks(counts, limit=PEAKS, spacing=3):
    """
    Всплески чата: отрезки, где сообщений заметно больше обычного, — по
    убыванию силы, не ближе spacing отрезков друг к другу, потом по времени.
    [номер отрезка, …].
    """
    if not counts:
        return []
    mean = sum(counts) / len(counts)
    deviation = math.sqrt(sum((count - mean) ** 2 for count in counts) / len(counts))
    # ровный чат всплесков не даёт: порог — заметно выше обычного, и не меньше пары сообщений
    threshold = max(3.0, mean + 2 * deviation)
    chosen = []
    for index in sorted(range(len(counts)), key=lambda index: -counts[index]):
        if counts[index] < threshold or len(chosen) >= limit:
            break
        if all(abs(index - other) >= spacing for other in chosen):
            chosen.append(index)
    return sorted(chosen)


def _merge(parts):
    """Соседние куски текста — в один."""
    merged = []
    for part in parts:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != "":
            merged.append(part)
    return merged


class LiveChatReplay:
    """
    Чат записи, которая ещё идёт: .chat.jsonl дочитывается при каждом вопросе.
    Встроенных картинок у такой записи ещё нет — смайлы и значки идут прямо
    адресами площадок (https) там, где у ChatReplay номера картинок, а images
    пуст. У Twitch так видны его смайлы; значки Twitch, смайлы 7TV, BTTV и FFZ
    появятся, когда запись допишется и чат переведётся в .chat.json.

    shift — на сколько видео части начинается раньше её чата: запись взяла
    эфир с начала (livechat.video_backlog). Его уточняет Api по мере записи.
    """

    def __init__(self, path):
        self.path = path
        self.images = []
        self.shift = 0.0
        self.start = None
        self._lock = threading.Lock()
        self._position = 0
        self._rest = b""
        self._entries = []         # (время от начала части, id, зритель, время отправки, сообщение)
        self._times = []
        self._emotes, self._badges = {}, {}
        self._deleted, self._banned = set(), {}
        self.refresh()

    def refresh(self):
        """Дочитать то, что дописано в файл с прошлого раза."""
        with self._lock:
            try:
                with open(self.path, "rb") as source:
                    source.seek(self._position)
                    data = source.read()
            except OSError:
                return
            self._position += len(data)
            *lines, self._rest = (self._rest + data).split(b"\n")
            for line in lines:
                try:
                    record = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if isinstance(record, dict):
                    self._take(record)

    def _take(self, record):
        kind = record.get("type")
        if kind == "start":
            if self.start is None:
                self.start = record.get("ts")
        elif kind in ("emote", "badge") and str(record.get("url") or "").startswith("https://"):
            (self._emotes if kind == "emote" else self._badges)[record.get("id")] = record["url"]
        elif kind == "delete":
            self._deleted.add(record.get("id"))
        elif kind == "delete_user":
            # бан прячет сказанное зрителем до него
            self._banned[record.get("user_id")] = record.get("ts") or float("inf")
        elif kind == "msg" and isinstance(record.get("ts"), (int, float)):
            if self.start is None:
                self.start = record["ts"]
            offset = round(max(0.0, record["ts"] - self.start), 3)
            index = bisect.bisect_right(self._times, offset)
            self._times.insert(index, offset)
            self._entries.insert(index, (offset, record.get("id"), record.get("user_id"), record["ts"],
                                         self._message(record)))

    def _message(self, record):
        name = record.get("name") or record.get("login") or ""
        system = record.get("system") or ""
        if "parts" in record:
            parts = []
            for part in record.get("parts") or ():
                text = str(part[0]) if part else ""
                url = self._emotes.get(part[1]) if len(part) > 1 else None
                parts.append([url, text] if url and text else text)
        else:
            from .live.livechat import twitch_fragments
            fragments, _ = twitch_fragments(record.get("text") or "", "" if system else record.get("emotes") or "")
            parts = [[TWITCH_EMOTE.format(id=fragment["emoticon"]["emoticon_id"]), fragment["text"]]
                     if fragment["emoticon"] else fragment["text"] for fragment in fragments]
        if system:
            parts = [system + " "] + parts
        badges = record.get("badges")
        badges = [self._badges[badge] for badge in badges if badge in self._badges] if isinstance(badges, list) else []
        return [name, readable_color(record.get("color"), name), badges, _merge(parts)]

    def _visible(self, entry):
        _, message_id, user_id, sent, _ = entry
        return message_id not in self._deleted and not sent <= self._banned.get(user_id, float("-inf"))

    def _shown(self, entry):
        return [round(entry[0] + self.shift, 3)] + entry[4]

    @property
    def count(self):
        with self._lock:
            return sum(1 for entry in self._entries if self._visible(entry))

    def window(self, start, end):
        """Сообщения со временем в видео от start (включительно) до end."""
        self.refresh()
        with self._lock:
            low = bisect.bisect_left(self._times, start - self.shift)
            high = bisect.bisect_left(self._times, end - self.shift)
            return [self._shown(entry) for entry in self._entries[low:high] if self._visible(entry)]

    def search(self, text, limit=SEARCH_LIMIT):
        needle = str(text).strip().casefold()
        if not needle:
            return []
        self.refresh()
        found = []
        with self._lock:
            for entry in self._entries:
                message = entry[4]
                words = [message[0]] + [part if isinstance(part, str) else part[1] for part in message[3]]
                if self._visible(entry) and needle in " ".join(words).casefold():
                    found.append(self._shown(entry))
                    if len(found) >= limit:
                        break
        return found

    def activity(self, duration):
        """График чата идущей записи — по уже записанному, со сдвигом, как в window."""
        self.refresh()
        with self._lock:
            times = [entry[0] + self.shift for entry in self._entries if self._visible(entry)]
        bucket = activity_bucket(duration)
        counts = activity(times, duration, bucket)
        return {"bucket": bucket, "counts": counts, "peaks": peaks(counts)}

    def image(self, index):
        return None
