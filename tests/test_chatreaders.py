# -*- coding: utf-8 -*-
"""
Чат эфира Kick, VK Video Live и YouTube: разбор сообщений, чтение (сеть
и yt-dlp — заглушки) и перевод в формат TwitchDownloader со смайлами
и значками внутри.
"""
import base64
import io
import json
import os
import queue
import shutil
import sys
import tempfile
import time
import unittest

import requests
from PIL import Image

from streamvault import util
from streamvault.live import chatreaders, livechat
from tests.helpers import wait_for

FAKE_YTDLP_CHAT = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                "fake_ytdlp_chat.py")]


def png(width, height):
    output = io.BytesIO()
    Image.new("RGBA", (width, height), (255, 0, 0, 255)).save(output, "PNG")
    return output.getvalue()


def image_size(data):
    with Image.open(io.BytesIO(base64.b64decode(data))) as image:
        return image.size


class Response:

    def __init__(self, data, status=200):
        self.status_code = status
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("%d" % self.status_code)

    def json(self):
        return self._data


class FakeHttp:
    """Вместо requests.Session: ответ — по методу и началу адреса."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for (route_method, prefix), answer in self.routes.items():
            if route_method == method and url.startswith(prefix):
                return Response(answer(kwargs) if callable(answer) else answer)
        return Response({}, 404)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class FakeSocket:
    """websocket Centrifugo: кадры из очереди, отправленное — в sent."""

    def __init__(self, frames):
        self.frames = queue.Queue()
        for frame in frames:
            self.frames.put(frame)
        self.sent = []
        self.closed = False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if self.closed:
            raise OSError("closed")
        try:
            return self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError

    def close(self):
        self.closed = True


class ChatFileTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.recorded = os.path.join(self.dir, "c - 2026-10-27 12h00m00s - стрим.ts")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def records(self):
        # метку конца части («end») проверяет отдельный тест
        with open(livechat.jsonl_path(self.recorded), encoding="utf-8") as source:
            return [record for record in map(json.loads, source) if record["type"] != "end"]


# ------------------------------------------------------------ Kick

KICK_INNER = {
    "id": "k1", "chatroom_id": 77, "content": "[emote:37233:PogU] привет [emote:1:A]", "type": "message",
    "created_at": "2026-10-27T15:13:58+00:00", "metadata": {"message_ref": "1793114038864"},
    "sender": {"id": 980826, "username": "xRawGoldx", "slug": "xrawgoldx", "identity": {
        "color": "#1475E1",
        "badges": [{"type": "og", "text": "OG"}, {"type": "subscriber", "text": "Subscriber", "count": 13}],
        "badges_v2": [
            {"name": "level", "image_url": "https://kick/badges/48.png", "metadata": {"level": 48}, "sort_order": 1},
            {"name": "Flyby", "image_url": "https://kick/badges/flyby.png", "sort_order": 4},
            {"name": "без картинки", "sort_order": 0}]}},
}
SUBSCRIBER_BADGES = [(1, "https://kick/sub1.png"), (12, "https://kick/sub12.png"), (24, "https://kick/sub24.png")]


def kick_push(event, inner):
    return json.dumps({"push": {"channel": "chatrooms.77.v2",
                                "pub": {"data": {"event": event, "data": json.dumps(inner)}}}})


class KickTest(ChatFileTest):

    def test_parse_message(self):
        record, emotes, badges = chatreaders.parse_kick_message(KICK_INNER, SUBSCRIBER_BADGES)
        self.assertEqual(record["parts"], [["PogU", "kick-37233"], [" привет "], ["A", "kick-1"]])
        self.assertEqual(emotes, [("kick-37233", "PogU", "https://files.kick.com/emotes/37233/fullsize"),
                                  ("kick-1", "A", "https://files.kick.com/emotes/1/fullsize")])
        # подписчик 13 месяцев — значок за 12; дальше значки по порядку Kick
        self.assertEqual([(title, url) for _, title, url in badges],
                         [("Subscriber", "https://kick/sub12.png"), ("level 48", "https://kick/badges/48.png"),
                          ("Flyby", "https://kick/badges/flyby.png")])
        self.assertEqual(record["badges"], [badge[0] for badge in badges])
        self.assertEqual((record["user_id"], record["login"], record["name"], record["color"]),
                         ("980826", "xrawgoldx", "xRawGoldx", "#1475E1"))
        self.assertEqual(record["ts"], 1793114038.864)     # точнее, чем created_at

    def test_plain_text_and_time_fallback(self):
        record, emotes, badges = chatreaders.parse_kick_message(
            {"id": "k2", "content": "просто текст", "created_at": "2026-10-27T15:13:58+00:00", "sender": {}})
        self.assertEqual((record["parts"], emotes, badges), ([["просто текст"]], [], []))
        self.assertEqual(record["ts"], 1793114038.0)

    def test_session(self):
        http = FakeHttp({
            ("GET", "https://kick.com/api/v2/channels/chan"): {
                "id": 5, "chatroom": {"id": 77},
                "subscriber_badges": [{"months": 1, "badge_image": {"src": "https://kick/sub1.png"}}]},
            ("POST", "https://web.kick.com/api/v1/realtime/channels/5/chat/connection"): {
                "data": {"connections": [{"credentials": {"url": "wss://centrifugo"}}]}},
            ("POST", "https://web.kick.com/api/v1/realtime/auth/connection"): {"data": {"token": "T"}},
        })
        socket = FakeSocket([
            json.dumps({"id": 1, "connect": {"expires": True, "ttl": 2700, "ping": 25}}),
            "{}",
            kick_push(chatreaders.KICK_MESSAGE, KICK_INNER),
            kick_push(chatreaders.KICK_DELETED, {"id": "x", "message": {"id": "k0"}}),
            kick_push(chatreaders.KICK_BANNED, {"id": "y", "user": {"id": 42, "username": "хам"}}),
        ])
        addresses = []
        chat = chatreaders.KickChat("chan", session=http, connect=lambda url: addresses.append(url) or socket)
        chat.begin_part(self.recorded, time.time())
        chat.start()
        self.assertTrue(wait_for(lambda: socket.frames.empty() and len(self.records()) >= 7, timeout=10))
        chat.stop()
        chat.join(5)
        self.assertFalse(chat.is_alive())
        self.assertTrue(socket.closed)
        self.assertEqual(addresses, ["wss://centrifugo"])
        self.assertEqual(json.loads(socket.sent[0]), {"connect": {"token": "T", "name": "js"}, "id": 1})
        self.assertEqual(json.loads(socket.sent[1])["subscribe"], {"channel": "chatrooms.77.v2"})
        self.assertIn("{}", socket.sent)                   # ответ на пинг
        # как у страницы канала: тот же заголовок, тот же клиент в обоих запросах
        posts = [kwargs for method, _, kwargs in http.calls if method == "POST"]
        self.assertEqual(posts[0]["headers"]["x-app-platform"], "web")
        self.assertEqual(posts[0]["json"]["client"]["id"], posts[1]["json"]["client_id"])

        records = self.records()
        self.assertEqual((records[0]["type"], records[0]["platform"]), ("start", "kick"))
        self.assertEqual([record["type"] for record in records[1:]],
                         ["emote", "emote", "badge", "badge", "badge", "msg", "delete", "delete_user"])
        self.assertEqual(records[-2], {"type": "delete", "id": "k0"})
        self.assertEqual(records[-1], {"type": "delete_user", "user_id": "42"})
        self.assertEqual(chat.room_id, "5")
        self.assertEqual(chat.messages, 1)

    def test_same_emote_described_once_per_part(self):
        chat = chatreaders.KickChat("chan", session=FakeHttp({}), connect=None)
        chat.begin_part(self.recorded, time.time())
        for _ in range(2):
            chat._emit(*chatreaders.parse_kick_message(KICK_INNER))
        other = os.path.join(self.dir, "c - часть 2.ts")
        chat.begin_part(other, time.time())
        chat._emit(*chatreaders.parse_kick_message(KICK_INNER))
        chat.end_part()
        kinds = [record["type"] for record in self.records()]
        self.assertEqual(kinds.count("emote"), 2)
        self.assertEqual(kinds.count("msg"), 2)
        with open(livechat.jsonl_path(other), encoding="utf-8") as source:
            # у следующей части — свой файл и свои описания
            self.assertEqual([json.loads(line)["type"] for line in source].count("emote"), 2)


# ------------------------------------------------------------ VK Video Live

def vk_message(message_id, created, text="привет", deleted=False):
    return {"id": message_id, "createdAt": created, "isDeleted": deleted, "isPrivate": False,
            "author": {"id": 7, "nick": "Zritel", "displayName": "Зритель", "nickColor": 2, "roles": []},
            "data": [{"type": "text", "content": json.dumps([text, "unstyled", []]), "modificator": ""}]}


class VkTest(ChatFileTest):

    def test_parse_message(self):
        message = {
            "id": 703014723, "createdAt": 1793113722,
            "author": {"id": 19746366, "nick": "Crystal", "displayName": "Crystal", "nickColor": 2,
                       "roles": [{"id": "r1", "name": "Модератор", "smallUrl": "https://vk/role-s.png",
                                  "mediumUrl": "https://vk/role-m.png"}],
                       "badges": [{"id": "b1", "name": "Без картинки"}]},
            "data": [
                {"type": "mention", "displayName": "Друг", "nick": "drug", "id": 1},
                {"type": "text", "content": json.dumps([", смотри ", "unstyled", []]), "modificator": ""},
                {"type": "smile", "id": "s1", "name": "Ура", "smallUrl": "https://vk/s-small.png",
                 "mediumUrl": "https://vk/s-medium.png"},
                {"type": "link", "url": "https://example.com", "content": json.dumps(["сайт", "unstyled", []])},
                {"type": "text", "content": json.dumps(["", "unstyled", []]), "modificator": "BLOCK_END"},
            ]}
        record, emotes, badges = chatreaders.parse_vk_message(message)
        self.assertEqual(record["parts"], [["Друг"], [", смотри "], ["Ура", "vk-s1"], ["сайт"]])
        self.assertEqual(record["text"], "Друг, смотри Урасайт")
        self.assertEqual(emotes, [("vk-s1", "Ура", "https://vk/s-medium.png")])
        self.assertEqual(badges, [("vk-r1", "Модератор", "https://vk/role-m.png")])
        self.assertEqual(record["color"], "#1D90FF")       # nickColor — номер цвета VK
        self.assertEqual((record["id"], record["ts"], record["login"]), ("vk-703014723", 1793113722.0, "crystal"))

    def test_new_messages_only_and_late_deletion(self):
        chat = chatreaders.VkVideoChat("blog", session=FakeHttp({}))
        chat.begin_part(self.recorded, time.time())
        now = int(time.time())
        # ответ — от новых к старым; сообщение до начала записи — уже не этот эфир
        chat._take([vk_message(2, now, "новое"), vk_message(1, now - 100, "старое")])
        chat._take([vk_message(3, now + 1, "ещё"), vk_message(2, now, deleted=True)])
        chat._take([vk_message(3, now + 1, "ещё")])
        chat.end_part()
        records = self.records()[1:]
        self.assertEqual([(record["type"], record.get("text") or record.get("id")) for record in records],
                         [("msg", "новое"), ("delete", "vk-2"), ("msg", "ещё")])
        self.assertEqual(chat.messages, 2)

    def test_gap_is_caught_up_with_longer_answer(self):
        now = int(time.time())
        holder = {}

        def answer(kwargs):
            limit = kwargs["params"]["limit"]
            if limit == chatreaders.VkVideoChat.LIMIT_CATCH_UP:
                holder["chat"]._stopping.set()
                return {"data": [vk_message(n, now) for n in (7, 6, 5, 4, 3)]}
            return {"data": [vk_message(n, now) for n in (7, 6)]}

        http = FakeHttp({("GET", "https://api.live.vkvideo.ru/v1/blog/blog/public_video_stream/chat"): answer})
        chat = holder["chat"] = chatreaders.VkVideoChat("blog", session=http)
        chat.LIMIT = 2
        chat._last_id = 3
        chat.begin_part(self.recorded, time.time())
        chat._session()
        chat.end_part()
        self.assertEqual([call[2]["params"]["limit"] for call in http.calls], [2, 300])
        self.assertEqual([record["id"] for record in self.records()[1:]], ["vk-4", "vk-5", "vk-6", "vk-7"])


# ------------------------------------------------------------ YouTube

def yt_text(message_id, sent, runs, author="@Зритель", badges=()):
    return {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
        "id": message_id, "timestampUsec": str(int(sent * 1e6)), "message": {"runs": runs},
        "authorName": {"simpleText": author}, "authorExternalChannelId": "UC" + author.strip("@"),
        "authorBadges": list(badges)}}}}


def yt_line(*actions):
    return json.dumps({"replayChatItemAction": {"actions": list(actions)}, "isLive": True}, ensure_ascii=False)


MEMBER_BADGE = {"liveChatAuthorBadgeRenderer": {
    "customThumbnail": {"thumbnails": [{"url": "https://yt/member=s16"}, {"url": "https://yt/member=s32"}]},
    "tooltip": "Спонсор (2 месяца)"}}
MODERATOR_BADGE = {"liveChatAuthorBadgeRenderer": {"icon": {"iconType": "MODERATOR"}, "tooltip": "Модератор"}}


class YouTubeTest(ChatFileTest):

    def test_text_with_emoji(self):
        action = yt_text("y1", 1793113000.5, [
            {"text": "привет "}, {"emoji": {"emojiId": "😀", "shortcuts": [":grinning:"]}},
            {"emoji": {"emojiId": "UCx/abc", "shortcuts": [":cat:"], "isCustomEmoji": True,
                       "image": {"thumbnails": [{"url": "//yt/cat=s24"}, {"url": "//yt/cat=s48"}]}}}],
            badges=[MODERATOR_BADGE, MEMBER_BADGE])
        kind, record, emotes, badges = chatreaders.parse_youtube_action(action)
        self.assertEqual(kind, "msg")
        emote_id = record["parts"][2][1]
        self.assertEqual(record["parts"], [["привет "], ["😀"], [":cat:", emote_id]])
        self.assertEqual(emotes, [(emote_id, ":cat:", "https://yt/cat=s48")])
        self.assertEqual([(title, url) for _, title, url in badges], [("Спонсор (2 месяца)", "https://yt/member=s32")])
        self.assertEqual(record["color"], chatreaders.YOUTUBE_COLORS["MODERATOR"])
        self.assertEqual((record["name"], record["login"], record["user_id"], record["ts"]),
                         ("@Зритель", "зритель", "UCЗритель", 1793113000.5))

    def test_super_chat_membership_and_moderation(self):
        paid = {"addChatItemAction": {"item": {"liveChatPaidMessageRenderer": {
            "id": "y2", "timestampUsec": "1793113000000000", "authorName": {"simpleText": "@Щедрый"},
            "purchaseAmountText": {"simpleText": "500,00 ₽"}, "message": {"runs": [{"text": "спасибо"}]}}}}}
        member = {"addChatItemAction": {"item": {"liveChatMembershipItemRenderer": {
            "id": "y3", "timestampUsec": "1793113000000000", "authorName": {"simpleText": "@Новый"},
            "headerSubtext": {"runs": [{"text": "Добро пожаловать в "}, {"text": "Клуб"}, {"text": "!"}]}}}}}
        _, record, _, _ = chatreaders.parse_youtube_action(paid)
        self.assertEqual((record["system"], record["text"]), ("Super Chat 500,00 ₽", "спасибо"))
        _, record, _, _ = chatreaders.parse_youtube_action(member)
        self.assertEqual((record["system"], record["parts"]), ("Добро пожаловать в Клуб!", [[""]]))
        self.assertEqual(chatreaders.parse_youtube_action({"markChatItemAsDeletedAction": {"targetItemId": "y1"}}),
                         ("delete", "y1"))
        self.assertEqual(chatreaders.parse_youtube_action(
            {"markChatItemsByAuthorAsDeletedAction": {"externalChannelId": "UCham"}}), ("delete_user", "UCham"))
        self.assertIsNone(chatreaders.parse_youtube_action({"addChatItemAction": {"item": {
            "liveChatViewerEngagementMessageRenderer": {}}}}))

    def reader(self, mode):
        now = time.time()
        lines = os.path.join(self.dir, "lines.jsonl")
        with open(lines, "w", encoding="utf-8") as target:
            # хвост чата до начала записи, затем эфир
            target.write(yt_line(yt_text("old", now - 600, [{"text": "давнее"}])) + "\n")
            target.write(yt_line(yt_text("new", now + 1, [{"text": "сейчас"}]),
                                 {"markChatItemAsDeletedAction": {"targetItemId": "gone"}}) + "\n")
        log = os.path.join(self.dir, "launches.jsonl")
        env = dict(util.child_environment(), FAKE_CHAT_LINES=lines, FAKE_CHAT_LOG=log, FAKE_CHAT_MODE=mode)
        chat = chatreaders.YouTubeChat("Chan", environment=lambda: env, command=FAKE_YTDLP_CHAT)
        chat.RECONNECT_DELAYS = (0.1,)
        chat.POLL = 0.1
        chat.begin_part(self.recorded, now)
        return chat, log

    def launches(self, log):
        if not os.path.exists(log):
            return []
        with open(log, encoding="utf-8") as source:
            return [json.loads(line) for line in source]

    def test_reads_ytdlp_file_as_it_grows(self):
        chat, log = self.reader("wait")
        chat.start()
        self.assertTrue(wait_for(lambda: chat.messages == 1 and len(self.records()) >= 3, timeout=15))
        process = chat._process
        chat.stop()
        chat.join(10)
        self.assertFalse(chat.is_alive())
        self.assertIsNotNone(process.poll())             # yt-dlp погашен
        args = self.launches(log)[0]
        self.assertEqual(args[args.index("--sub-langs") + 1], "live_chat")
        self.assertEqual(args[-1], "https://www.youtube.com/@Chan/live")
        folder = os.path.dirname(args[args.index("-o") + 1])
        self.assertFalse(os.path.exists(folder))          # временная папка убрана
        records = self.records()
        self.assertEqual([record["type"] for record in records], ["start", "msg", "delete"])
        self.assertEqual(records[1]["text"], "сейчас")

    def test_stale_temp_folders_removed(self):
        old = tempfile.mkdtemp(prefix=chatreaders.TEMP_PREFIX)
        fresh = tempfile.mkdtemp(prefix=chatreaders.TEMP_PREFIX)
        self.addCleanup(shutil.rmtree, fresh, True)
        long_ago = time.time() - chatreaders.STALE_AFTER - 60
        os.utime(old, (long_ago, long_ago))
        chatreaders._remove_stale_folders()
        self.assertFalse(os.path.exists(old))            # от аварийно закрытой программы
        self.assertTrue(os.path.exists(fresh))           # может читать другой эфир прямо сейчас

    def test_restart_does_not_repeat_messages(self):
        chat, log = self.reader("fail")
        with self.assertLogs(level="INFO") as logs:
            chat.start()
            self.assertTrue(wait_for(lambda: len(self.launches(log)) >= 3, timeout=20))
            chat.stop()
            chat.join(10)
        self.assertEqual(chat.messages, 1)
        # о повторяющемся сбое — одна строка, а не по строке на перезапуск
        self.assertEqual(sum("связь прервалась" in line for line in logs.output), 1)


# ------------------------------------------------------------ в формат TwitchDownloader

class ConvertTest(ChatFileTest):

    def write(self, *records):
        with open(livechat.jsonl_path(self.recorded), "w", encoding="utf-8") as target:
            for record in records:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")

    def convert(self, images):
        target = os.path.join(self.dir, "c.chat.json")
        fetched = []

        def fetch(url):
            fetched.append(url)
            return images.get(url)

        count = livechat.convert(livechat.jsonl_path(self.recorded), target, title="стрим", fetch=fetch)
        with open(target, encoding="utf-8") as source:
            return count, json.load(source), fetched

    def message(self, message_id, user, parts, badges=(), ts=101.0):
        return {"type": "msg", "ts": ts, "id": message_id, "user_id": user, "login": user, "name": user.upper(),
                "color": "#123456", "badges": list(badges), "parts": parts,
                "text": "".join(part[0] for part in parts), "system": ""}

    def test_images_embedded_and_bans_applied(self):
        self.write(
            {"type": "start", "ts": 100.0, "platform": "kick", "channel": "c", "room_id": "5"},
            {"type": "emote", "id": "kick-1", "name": "Pog", "url": "https://e/1"},
            {"type": "emote", "id": "kick-9", "name": "Unused", "url": "https://e/9"},
            {"type": "badge", "id": "kick-a", "name": "Flyby", "url": "https://b/a"},
            {"type": "badge", "id": "kick-b", "name": "Flyby", "url": "https://b/b"},
            {"type": "badge", "id": "kick-c", "name": "level 48", "url": "https://b/broken"},
            self.message("m1", "u1", [["Pog", "kick-1"], [" привет"]], ["kick-a", "kick-b", "kick-c"]),
            self.message("m2", "u2", [["до бана"]]),
            {"type": "delete_user", "user_id": "u2"},
            self.message("m3", "u2", [["после бана"]], ts=103.0),
            self.message("m4", "u1", [["Нет", "kick-2"], [" картинки"]], ts=104.0),
        )
        count, data, fetched = self.convert({"https://e/1": png(112, 112), "https://b/a": png(72, 72),
                                             "https://b/b": png(18, 18), "https://b/broken": b"not an image"})
        self.assertEqual(count, 3)
        self.assertNotIn("https://e/9", fetched)            # смайл ни в одном сообщении — не качается
        self.assertEqual(data["streamer"]["id"], 5)
        embedded = data["embeddedData"]
        emote = embedded["firstParty"][0]
        self.assertEqual((emote["id"], emote["name"], emote["imageScale"], emote["width"], emote["height"]),
                         ("kick-1", "Pog", 4, 28, 28))
        badges = {badge["name"]: badge["versions"]["1"] for badge in embedded["twitchBadges"]}
        self.assertEqual(sorted(badges), ["Flyby", "Flyby 2"])  # одноимённые различаются
        self.assertEqual({image_size(badge["bytes"]) for badge in badges.values()}, {(36, 36)})

        first, after_ban, no_image = data["comments"]
        self.assertEqual(first["message"]["user_badges"], [{"_id": "Flyby", "version": "1"},
                                                           {"_id": "Flyby 2", "version": "1"}])
        self.assertEqual([(f["text"], (f["emoticon"] or {}).get("emoticon_id")) for f in first["message"]["fragments"]],
                         [("Pog", "kick-1"), (" привет", None)])
        self.assertEqual(first["message"]["emoticons"], [{"_id": "kick-1", "begin": 0, "end": 2}])
        self.assertEqual(first["message"]["user_color"], "#123456")
        self.assertEqual(after_ban["message"]["body"], "после бана")
        self.assertEqual(no_image["message"]["fragments"], [{"text": "Нет картинки", "emoticon": None}])

    def test_without_badges_placeholder_keeps_twitchdownloader_offline(self):
        self.write({"type": "start", "ts": 100.0, "platform": "youtube", "channel": "c"},
                   dict(self.message("y1", "u1", [["привет"]]), system="Super Chat 5 $"))
        count, data, fetched = self.convert({})
        self.assertEqual((count, fetched), (1, []))
        self.assertEqual(data["embeddedData"]["twitchBadges"], [livechat.PLACEHOLDER_BADGE])
        self.assertEqual(data["embeddedData"]["firstParty"], [])
        self.assertEqual(data["comments"][0]["message"]["body"], "Super Chat 5 $ привет")

    def test_twitch_left_to_twitchdownloader(self):
        self.write({"type": "start", "ts": 100.0, "channel": "c", "room_id": "111"},
                   {"type": "msg", "ts": 101.0, "id": "t1", "user_id": "1", "login": "x", "name": "X",
                    "color": "", "badges": "", "emotes": "", "bits": "", "text": "привет", "system": ""})
        count, data, fetched = self.convert({})
        self.assertEqual((count, fetched), (1, []))
        self.assertIsNone(data["embeddedData"])


class MakeChatTest(unittest.TestCase):

    def test_each_platform_has_its_reader(self):
        cases = {"twitch": livechat.TwitchChat, "kick": chatreaders.KickChat,
                 "vkvideolive": chatreaders.VkVideoChat, "youtube": chatreaders.YouTubeChat}
        for platform, reader in cases.items():
            self.assertIsInstance(chatreaders.make_chat(platform, "c"), reader)
        # ссылка на видео YouTube — её поймёт yt-dlp; у остальных канал ссылкой не читается
        chat = chatreaders.make_chat("youtube", "https://www.youtube.com/watch?v=abc")
        self.assertEqual(chat._url, "https://www.youtube.com/watch?v=abc")
        self.assertIsNone(chatreaders.make_chat("kick", "https://kick.com/xqc?clip=1"))
        self.assertIsNone(chatreaders.make_chat("другая", "c"))


if __name__ == "__main__":
    unittest.main()
