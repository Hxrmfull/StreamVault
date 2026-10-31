# -*- coding: utf-8 -*-
"""
Просмотр записи с чатом в окне: разбор .chat.json, сервер видео и картинок
(перемотка через Range, доступ только по токену), вызовы окна.
"""
import base64
import http.client
import io
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock
from urllib.parse import urlsplit

from PIL import Image

from streamvault.chatreplay import TWITCH_EMOTE, ChatReplay, readable_color
from streamvault.library import broadcast_parts
from streamvault.positions import Positions
from streamvault.settings import Settings
from streamvault.ui.api import Api
from streamvault.ui.media import MediaServer, parse_range
from tests.test_ui import StubDownloads, StubLive


def png_b64(width=4, height=4):
    output = io.BytesIO()
    Image.new("RGBA", (width, height), (0, 255, 0, 255)).save(output, "PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def comment(offset, name, fragments, color=None, badges=()):
    return {"content_offset_seconds": offset, "commenter": {"display_name": name, "name": name.lower()},
            "message": {"body": "".join(fragment["text"] for fragment in fragments), "fragments": fragments,
                        "user_color": color, "user_badges": [{"_id": n, "version": v} for n, v in badges]}}


def text(value):
    return {"text": value, "emoticon": None}


def emote(value, emote_id):
    return {"text": value, "emoticon": {"emoticon_id": emote_id}}


CHAT = {
    "video": {"start": 10, "length": 100},
    "comments": [
        comment(70, "Поздний", [text("последнее")]),
        comment(12.5, "Зритель", [emote("Kappa", "25"), text(" привет and KEKW там")], "#FF4500",
                badges=[("subscriber", "12"), ("нет-такого", "1")]),
        comment(20, "Синий", [emote("Свой", "own1"), text(" ok")], "#0000FF"),
        comment(5, "До отрезка", [text("раньше начала")]),
    ],
    "embeddedData": {
        "firstParty": [{"id": "own1", "data": png_b64(), "imageScale": 1}],
        "thirdParty": [{"name": "KEKW", "data": png_b64(8, 4)}, {"name": "and", "data": png_b64()}],
        "twitchBadges": [{"name": "subscriber", "versions": {"12": {"title": "Sub", "bytes": png_b64()}}}],
        "twitchBits": [],
    },
}


class ReplayTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "запись.chat.json")
        with open(self.path, "w", encoding="utf-8") as target:
            json.dump(CHAT, target, ensure_ascii=False)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_messages_by_recording_time(self):
        replay = ChatReplay(self.path)
        # время — от начала отрезка (video.start); сказанное до него — в самом начале
        self.assertEqual([(message[0], message[1]) for message in replay.messages],
                         [(0.0, "До отрезка"), (2.5, "Зритель"), (10.0, "Синий"), (60.0, "Поздний")])
        self.assertEqual([message[1] for message in replay.window(2.5, 60)], ["Зритель", "Синий"])
        self.assertEqual(replay.window(61, 1000), [])

    def test_emotes_badges_and_colors(self):
        replay = ChatReplay(self.path)
        _, _, color, badges, parts = replay.messages[1]
        self.assertEqual(color, "#FF4500")
        self.assertEqual(len(badges), 1)                     # значка без картинки нет
        # смайл Twitch без встроенной картинки — с сайта Twitch; смайлы 7TV, BTTV
        # и FFZ — по словам текста, как у TwitchDownloader
        self.assertEqual([part if isinstance(part, str) else part[1] for part in parts],
                         ["Kappa", " привет ", "and", " ", "KEKW", " там"])
        kappa = parts[0]
        self.assertEqual(replay.images[kappa[0]], TWITCH_EMOTE.format(id="25"))
        self.assertTrue(all(isinstance(parts[i], list) for i in (0, 2, 4)))
        own = replay.messages[2][4][0]
        data, mime = replay.image(own[0])
        self.assertEqual(mime, "image/png")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertIsNone(replay.image(kappa[0]))            # адрес, а не встроенная картинка
        self.assertIsNone(replay.image(999))
        # синий ник на тёмном фоне осветлён, а без цвета — цвет по имени
        self.assertNotEqual(replay.messages[2][2], "#0000FF")
        self.assertRegex(replay.messages[0][2], r"^#[0-9A-F]{6}$")

    def test_readable_color(self):
        self.assertEqual(readable_color("#FF4500"), "#FF4500")
        self.assertEqual(readable_color(None, "имя"), readable_color("", "имя"))
        self.assertEqual(readable_color("red; background:url(x)", "имя"), readable_color("", "имя"))

    def test_chat_without_embedded_data(self):
        with open(self.path, "w", encoding="utf-8") as target:
            json.dump({"comments": [comment(1, "A", [text("hi")])], "embeddedData": None}, target)
        replay = ChatReplay(self.path)
        self.assertEqual(replay.messages[0][4], ["hi"])
        self.assertEqual(replay.images, [])


class ChatSearchTest(ReplayTest):

    def test_search_name_text_and_emote_names(self):
        replay = ChatReplay(self.path)
        self.assertEqual([message[1] for message in replay.search("ПРИВЕТ")], ["Зритель"])
        self.assertEqual([message[1] for message in replay.search("синий")], ["Синий"])
        self.assertEqual([message[1] for message in replay.search("kekw")], ["Зритель"])   # смайл словом
        self.assertEqual([message[1] for message in replay.search("свой")], ["Синий"])     # смайл с картинкой
        self.assertEqual(replay.search("  "), [])
        self.assertEqual(len(replay.search("а", limit=1)), 1)


class PositionsTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = os.path.join(self.dir, "cache", "positions.json")

    def test_saved_on_disk_and_forgotten_when_watched(self):
        positions = Positions(self.file)
        positions.save("a.mp4", 600, 3600)
        self.assertEqual(Positions(self.file).get("a.mp4"), 600)       # переживает перезапуск
        positions.save("a.mp4", 3590, 3600)                           # последние 2 % — досмотрено
        self.assertIsNone(Positions(self.file).get("a.mp4"))
        positions.save("b.mp4", 3)                                     # самое начало — не повод
        self.assertIsNone(positions.get("b.mp4"))

    def test_limit_drops_oldest(self):
        clock = iter(range(10 ** 6))
        positions = Positions(clock=lambda: next(clock))
        with unittest.mock.patch("streamvault.positions.LIMIT", 3):
            for name in "abcd":
                positions.save(name + ".mp4", 100)
        self.assertIsNone(positions.get("a.mp4"))
        self.assertEqual(positions.get("d.mp4"), 100)

    def test_broken_file_is_ignored(self):
        os.makedirs(os.path.dirname(self.file))
        with open(self.file, "w") as target:
            target.write("{оборвано")
        self.assertIsNone(Positions(self.file).get("a.mp4"))


class PartsTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def touch(self, name):
        path = os.path.join(self.dir, name)
        open(path, "wb").close()
        return path

    def test_parts_of_one_broadcast_in_order(self):
        first = self.touch("chan - 2026-10-25 12h00m00s - старое название.mp4")
        third = self.touch("chan - 2026-10-25 12h00m00s - новое название - part03.ts")
        second = self.touch("chan - 2026-10-25 12h00m00s - новое название - part02.mp4")
        self.touch("chan - 2026-10-25 12h00m00s - новое название - part02.ts")   # ещё не убран
        self.touch("chan - 2026-10-25 12h00m00s - старое название.chat.json")
        self.touch("chan - 2026-10-25 18h00m00s - другой эфир.mp4")
        self.touch("chan - 2026-10-25 12h00m00s - новое название - part04.ts.part")
        self.assertEqual(broadcast_parts(third), [(1, first), (2, second), (3, third)])

    def test_single_and_unknown_names(self):
        alone = self.touch("chan - 2026-10-25 12h00m00s.mp4")
        self.assertEqual(broadcast_parts(alone), [(1, alone)])
        other = self.touch("скачанное видео.mp4")
        self.assertEqual(broadcast_parts(other), [(1, other)])


class RangeTest(unittest.TestCase):

    def test_parse_range(self):
        self.assertIsNone(parse_range(None, 100))
        self.assertEqual(parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(parse_range("bytes=-30", 100), (70, 99))
        for bad in ("bytes=100-", "bytes=20-10", "bytes=-", "items=0-1", "bytes=0-1,5-6"):
            with self.assertRaises(ValueError, msg=bad):
                parse_range(bad, 100)


class MediaServerTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.video = os.path.join(self.dir, "эфир 1.mp4")
        with open(self.video, "wb") as target:
            target.write(bytes(range(256)) * 40)
        self.server = MediaServer(images=lambda chat_id, index: (b"\x89PNGkartinka", "image/png")
                                  if (chat_id, index) == ("c1", 3) else None)

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def get(self, url, headers=None, method="GET"):
        parts = urlsplit(url)
        connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        connection.request(method, parts.path, headers=headers or {})
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def test_video_with_seeking(self):
        url = self.server.file_url(self.video)
        self.assertEqual(url, self.server.file_url(self.video))    # один адрес на файл
        response, data = self.get(url)
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "video/mp4"))
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
        self.assertEqual(len(data), 10240)
        response, data = self.get(url, {"Range": "bytes=256-511"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.getheader("Content-Range"), "bytes 256-511/10240")
        self.assertEqual(data, bytes(range(256)))
        response, _ = self.get(url, {"Range": "bytes=99999-"})
        self.assertEqual(response.status, 416)
        response, data = self.get(url, method="HEAD")
        self.assertEqual((response.status, response.getheader("Content-Length"), data), (200, "10240", b""))

    def test_images_of_open_chat(self):
        response, data = self.get(self.server.image_url("c1", 3))
        self.assertEqual((response.status, response.getheader("Content-Type"), data),
                         (200, "image/png", b"\x89PNGkartinka"))
        self.assertEqual(self.get(self.server.image_url("c1", 4))[0].status, 404)

    def test_only_with_token_host_and_known_files(self):
        url = self.server.file_url(self.video)
        parts = urlsplit(url)
        wrong_token = url.replace(self.server.token, "x" * len(self.server.token))
        self.assertEqual(self.get(wrong_token)[0].status, 403)
        # подмена адреса через DNS: токен верный, а Host — чужой сайт
        self.assertEqual(self.get(url, {"Host": "evil.example:%d" % parts.port})[0].status, 403)
        base = url[:url.index("/file/") + 1]
        for path in ("file/unknown/x.mp4", "file/../../settings.json", "other/1", "img/c1/x"):
            self.assertEqual(self.get(base + path)[0].status, 404, path)


class WatchApiTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.root = os.path.join(self.dir, "streams")
        settings = Settings(os.path.join(self.dir, "settings.json"), {"root_path": self.root})
        self.api = Api(settings, StubLive(), StubDownloads(self.root))
        self.addCleanup(self.api._media_server.stop)
        folder = os.path.join(self.root, "finished", "twitch", "chan")
        os.makedirs(folder)
        self.video = os.path.join(folder, "chan - стрим.mp4")
        open(self.video, "wb").close()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_watch_with_chat(self):
        self.assertIsNone(self.api.watch(self.video)["chat"])
        with open(self.video[:-4] + ".chat.json", "w", encoding="utf-8") as target:
            json.dump(CHAT, target, ensure_ascii=False)
        reply = self.api.call("watch", [self.video])
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertTrue(result["video"].startswith("http://127.0.0.1:"))
        chat = result["chat"]
        self.assertEqual(chat["count"], 4)
        local = result["video"][:result["video"].index("/file/") + 1]
        self.assertTrue(all(url.startswith((local, "https://")) for url in chat["images"]))
        messages = self.api.call("chat_replay", [chat["id"], 0, 120])["result"]
        self.assertEqual([message[1] for message in messages], ["До отрезка", "Зритель", "Синий", "Поздний"])
        # тот же файл — тот же открытый чат
        self.assertEqual(self.api.watch(self.video)["chat"]["id"], chat["id"])
        self.assertFalse(self.api.call("chat_replay", ["нет такого", 0, 10])["ok"])

    def test_ts_is_remuxed_when_ffmpeg_is_there(self):
        ts = self.video[:-4] + ".ts"
        open(ts, "wb").close()

        class Locator:
            def __init__(self, path):
                self._path = path

            def path(self):
                return self._path

        class Library:
            def preview(self, path):
                return {"thumbnail": "", "duration": 136.5}

        self.assertFalse(self.api.watch(ts)["remux"])          # без ffmpeg — как есть (окно предложит mp4)
        self.api._ffmpeg, self.api._library = Locator("ffmpeg.exe"), Library()
        result = self.api.watch(ts)
        self.assertTrue(result["remux"])
        self.assertEqual(result["duration"], 136.5)
        self.assertIn("/remux/", result["video"])
        self.assertFalse(self.api.watch(self.video)["remux"])   # mp4 — без перепаковки

    def test_position_and_parts(self):
        folder = os.path.dirname(self.video)
        first = os.path.join(folder, "chan - 2026-10-25 12h00m00s - эфир.mp4")
        second = os.path.join(folder, "chan - 2026-10-25 12h00m00s - эфир - part02.mp4")
        for path in (first, second):
            open(path, "wb").close()
        result = self.api.watch(second)
        self.assertIsNone(result["position"])
        self.assertEqual([(part["part"], part["path"]) for part in result["parts"]], [(1, first), (2, second)])
        self.assertTrue(self.api.call("save_position", [second, 125.5, 3600])["ok"])
        self.assertEqual(self.api.watch(second)["position"], 125.5)
        for bad in (["x", 1], [second, "сто"], [second, True], [second, 10, "долго"]):
            self.assertFalse(self.api.call("save_position", bad)["ok"], bad)

    def test_chat_search(self):
        with open(self.video[:-4] + ".chat.json", "w", encoding="utf-8") as target:
            json.dump(CHAT, target, ensure_ascii=False)
        chat_id = self.api.watch(self.video)["chat"]["id"]
        found = self.api.call("chat_search", [chat_id, "привет"])["result"]
        self.assertEqual([message[1] for message in found], ["Зритель"])
        self.assertFalse(self.api.call("chat_search", ["закрыт", "привет"])["ok"])

    def test_edits(self):
        self.assertFalse(self.api.call("cut_clip", [self.video, 1, 5])["ok"])     # правки нет — ошибка, а не сбой

        class Editor:
            def __init__(self):
                self.calls = []

            def cut(self, path, start, end, with_chat):
                self.calls.append(("cut", path, start, end, with_chat))
                return "e1"

            def overlay(self, path, side):
                self.calls.append(("overlay", path, side))
                return "e2"

            def cancel(self, job_id):
                self.calls.append(("cancel", job_id))

            def snapshot(self):
                return []

        editor = self.api._editor = Editor()
        self.assertEqual(self.api.call("cut_clip", [self.video, 1, 5.5, True])["result"], "e1")
        self.assertEqual(self.api.call("overlay_chat", [self.video, "left"])["result"], "e2")
        self.api.call("cancel_edit", ["e1"])
        self.assertEqual(editor.calls, [("cut", self.video, 1, 5.5, True), ("overlay", self.video, "left"),
                                        ("cancel", "e1")])
        for bad in ([self.video, "1", 5], [self.video, 1, None], [os.path.join(self.dir, "чужое.mp4"), 1, 5]):
            self.assertFalse(self.api.call("cut_clip", bad)["ok"], bad)

    def test_only_recordings(self):
        outside = os.path.join(self.dir, "чужое.mp4")
        open(outside, "wb").close()
        html = self.video[:-4] + ".chat.html"
        open(html, "wb").close()
        for path in (outside, html):
            self.assertFalse(self.api.call("watch", [path])["ok"], path)

    def test_broken_chat_is_reported(self):
        with open(self.video[:-4] + ".chat.json", "w", encoding="utf-8") as target:
            target.write("{оборвано")
        reply = self.api.call("watch", [self.video])
        self.assertFalse(reply["ok"])
        self.assertIn("чат", reply["error"])


if __name__ == "__main__":
    unittest.main()
