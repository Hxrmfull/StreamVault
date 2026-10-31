# -*- coding: utf-8 -*-
"""
Чат записей YouTube и Kick (archive/vodchat.py) и перепаковка записи в .ts
для проигрывателя окна (ui/media.py). Сеть и yt-dlp — заглушки.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from streamvault import util
from streamvault.archive import chat, vkrecords, vodchat
from streamvault.ui.media import MediaServer, remux_command
from tests.test_chatreaders import FAKE_YTDLP_CHAT, KICK_INNER, FakeHttp, vk_message, yt_line, yt_text

FFMPEG = shutil.which("ffmpeg")


def replay(offset, *actions):
    """Строка live_chat.json прошедшего эфира: действия и их время в записи."""
    line = json.loads(yt_line(*actions))
    line["replayChatItemAction"]["videoOffsetTimeMsec"] = str(int(offset * 1000))
    return json.dumps(line, ensure_ascii=False)


class YoutubeRecordsTest(unittest.TestCase):

    LINES = [
        replay(-30, yt_text("before", 1000.0, [{"text": "до эфира"}])),
        replay(5, yt_text("a", 1036.0, [{"text": "раз"}])),
        replay(12, yt_text("b", 1042.5, [{"text": "два"}]),
               {"markChatItemAsDeletedAction": {"targetItemId": "a"}}),
        replay(70, yt_text("c", 1101.0, [{"text": "три"}])),
    ]

    def test_time_in_recording_from_video_offset(self):
        records = vodchat.youtube_records(self.LINES)
        start = records[0]
        self.assertEqual((start["type"], start["platform"]), ("start", "youtube"))
        messages = {record["id"]: record["ts"] - start["ts"] for record in records if record["type"] == "msg"}
        # одна поправка часов на всю запись; сказанное до начала записи — не её чат
        self.assertEqual({key: round(value, 3) for key, value in messages.items()},
                         {"a": 5.0, "b": 12.0, "c": 70.0})
        self.assertIn({"type": "delete", "id": "a"}, records)

    def test_section(self):
        records = vodchat.youtube_records(self.LINES, section=(10, 60))
        start = records[0]["ts"]
        self.assertEqual([(record["id"], round(record["ts"] - start, 3)) for record in records
                          if record["type"] == "msg"], [("b", 2.0)])

    def test_download_with_ytdlp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        lines = os.path.join(folder, "lines.jsonl")
        with open(lines, "w", encoding="utf-8") as target:
            target.write("\n".join(self.LINES) + "\n")
        log = os.path.join(folder, "launches.jsonl")
        env = dict(util.child_environment(), FAKE_CHAT_LINES=lines, FAKE_CHAT_LOG=log, FAKE_CHAT_MODE="done")
        target = os.path.join(folder, "запись.chat.json")
        count = vodchat.download("youtube", "https://www.youtube.com/watch?v=x", target, title="эфир",
                                 channel="канал", duration=100, env=env, command=FAKE_YTDLP_CHAT,
                                 fetch=lambda url: None)
        self.assertEqual(count, 2)                      # удалённое модератором не попало
        with open(target, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual([comment["message"]["body"] for comment in data["comments"]], ["два", "три"])
        self.assertEqual([comment["content_offset_seconds"] for comment in data["comments"]], [12.0, 70.0])
        self.assertEqual(data["video"]["title"], "эфир")
        with open(log, encoding="utf-8") as source:
            args = json.loads(source.readline())
        self.assertEqual(args[args.index("--sub-langs") + 1], "live_chat")
        self.assertIn("--skip-download", args)

    def test_video_without_chat(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        lines = os.path.join(folder, "lines.jsonl")
        open(lines, "w").close()
        env = dict(util.child_environment(), FAKE_CHAT_LINES=lines, FAKE_CHAT_LOG=os.path.join(folder, "log"),
                   FAKE_CHAT_MODE="fail")
        with self.assertRaisesRegex(chat.ChatError, "чат недоступен"):
            vodchat.download("youtube", "https://www.youtube.com/watch?v=x", os.path.join(folder, "c.chat.json"),
                             env=env, command=FAKE_YTDLP_CHAT)


VOD_ID = "01a17d6e-19d0-7022-8ef8-a5ad7e355259"


def kick_message(message_id, created, text="привет", user=1):
    message = dict(KICK_INNER, id=message_id, content=text, created_at=created,
                   metadata=json.dumps({"message_ref": ""}))
    message["sender"] = dict(KICK_INNER["sender"], id=user)
    return message


class KickTest(unittest.TestCase):

    def http(self, pages):
        """История чата: страницы от конца записи к началу, по курсору."""
        def history(kwargs):
            return {"data": pages[kwargs["params"]["cursor"]]}

        return FakeHttp({
            ("GET", "https://kick.com/api/v2/channels/chan"): {"id": 5, "subscriber_badges": []},
            ("GET", "https://web.kick.com/api/v1/channels/5/videos"): {"data": [
                {"id": "другая", "start_time": "2026-10-27T10:00:00Z", "duration": 50},
                {"id": VOD_ID, "start_time": "2026-10-27T12:00:00Z", "duration": 600}]},
            ("GET", "https://web.kick.com/api/v1/chat/5/history"): history,
        })

    def test_history_walked_back_to_start(self):
        start = 1793102400            # 2026-10-27 12:00:00 UTC
        end = int((start + 600) * 1e6)
        pages = {
            end: {"messages": [kick_message("m3", "2026-10-27T12:09:00Z", "три"),
                               kick_message("m2", "2026-10-27T12:05:00Z", "два")], "cursor": str(end - 400 * 10 ** 6)},
            end - 400 * 10 ** 6: {"messages": [kick_message("m2", "2026-10-27T12:05:00Z", "два"),
                                               kick_message("m1", "2026-10-27T12:00:30Z", "раз"),
                                               kick_message("m0", "2026-10-27T11:59:00Z", "до записи")],
                                  "cursor": str(end - 700 * 10 ** 6)},
        }
        http = self.http(pages)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "c.chat.json")
        progress = []
        with mock.patch.object(vodchat, "PAGE_PAUSE", 0):
            count = vodchat.download("kick", "https://kick.com/chan/videos/" + VOD_ID, target, session=http,
                                     progress=progress.append, fetch=lambda url: None)
        self.assertEqual(count, 3)
        with open(target, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual([(comment["message"]["body"], comment["content_offset_seconds"])
                          for comment in data["comments"]], [("раз", 30.0), ("два", 300.0), ("три", 540.0)])
        self.assertEqual(data["streamer"]["id"], 5)
        self.assertEqual(progress, [50])        # прошли 12:05–12:10 из 12:00–12:10
        # третьей страницы нет: история дошла до начала записи
        self.assertEqual(len([call for call in http.calls if "history" in call[1]]), 2)

    def test_section_and_old_link(self):
        start = 1793102400
        # старая ссылка — с uuid видео: его описание у kick.com/api/v1/video
        end = int((start + 120) * 1e6)
        http = self.http({end: {"messages": [kick_message("m1", "2026-10-27T12:01:30Z", "в отрезке"),
                                             kick_message("m0", "2026-10-27T11:59:00Z")], "cursor": "1"}})
        http.routes[("GET", "https://kick.com/api/v1/video/")] = {"livestream": {
            "channel_id": 5, "start_time": "2026-10-27T12:00:00+00:00", "duration": 600000}}
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "c.chat.json")
        count = vodchat.download("kick", "https://kick.com/video/ddadacbe-64f5-438a-b4e9-7e10bee748bf", target,
                                 section=(60, 120), session=http, fetch=lambda url: None)
        self.assertEqual(count, 1)
        with open(target, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["comments"][0]["content_offset_seconds"], 30.0)

    def test_clip(self):
        # клип Kick — кусок эфира: начало и длительность из его описания
        start = 1793102400
        end = int((start + 60) * 1e6)
        http = self.http({end: {"messages": [kick_message("m1", "2026-10-27T12:00:40Z", "в клипе"),
                                             kick_message("m0", "2026-10-27T11:59:00Z", "до клипа")], "cursor": "1"}})
        http.routes[("GET", "https://kick.com/api/v2/clips/clip_01ABC")] = {"clip": {
            "channel_id": 5, "started_at": "2026-10-27T12:00:00Z", "duration": 60}}
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "c.chat.json")
        count = vodchat.download("kick", "https://kick.com/chan/clips/clip_01ABC", target, session=http,
                                 fetch=lambda url: None)
        self.assertEqual(count, 1)
        with open(target, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["comments"][0]["content_offset_seconds"], 40.0)

    def test_unknown_link(self):
        with self.assertRaises(chat.ChatError):
            vodchat.download("kick", "https://kick.com/chan", "x.chat.json", session=FakeHttp({}))


RECORD = "c0135f88-b075-4b41-a0b1-f70b108af812"


class VkTest(unittest.TestCase):

    def record_message(self, message_id, time_code, text="привет"):
        message = vk_message(message_id, 1793033881 + time_code + 4, text)
        message["timeCode"] = time_code
        return message

    def test_record_walked_forward(self):
        # чат записи: limit сообщений до места и столько же после
        chat_log = [self.record_message(n, n * 10, "№%d" % n) for n in range(1, 8)]
        chat_log[2]["isDeleted"] = True

        def answer(kwargs):
            limit, at = kwargs["params"]["limit"], kwargs["params"]["time_code"]
            before = [m for m in chat_log if m["timeCode"] < at][-limit:]
            after = [m for m in chat_log if m["timeCode"] >= at][:limit]
            return {"data": list(reversed(before + after))}

        http = FakeHttp({("GET", "https://api.live.vkvideo.ru/v1/channel/blog/record/" + RECORD + "/chat/"): answer})
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "c.chat.json")
        with mock.patch.object(vodchat, "VK_PAGE", 2), mock.patch.object(vodchat, "PAGE_PAUSE", 0):
            count = vodchat.download("vkvideolive", "https://live.vkvideo.ru/blog/record/" + RECORD, target,
                                     section=(15, 65), session=http, fetch=lambda url: None)
        self.assertEqual(count, 4)                      # 20, 40, 50, 60; 30 — удалено
        with open(target, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual([(c["message"]["body"], c["content_offset_seconds"]) for c in data["comments"]],
                         [("№2", 5.0), ("№4", 25.0), ("№5", 35.0), ("№6", 45.0)])
        self.assertEqual(data["comments"][0]["message"]["user_color"], "#1D90FF")
        self.assertEqual([call[2]["params"]["time_code"] for call in http.calls], [15, 30, 40, 50, 60])

    def test_clip_is_part_of_record(self):
        # чат клипа — чат его отрезка записи, время — от начала клипа
        chat_log = [self.record_message(n, n * 10, "№%d" % n) for n in range(1, 8)]

        def answer(kwargs):
            at = kwargs["params"]["time_code"]
            return {"data": list(reversed([m for m in chat_log if m["timeCode"] >= at]))}

        http = FakeHttp({
            ("GET", "https://api.live.vkvideo.ru/v1/channel/blog/clip/" + RECORD): {"data": {"clip": {
                "recordId": "r1", "offsetStart": 25, "offsetEnd": 55, "duration": 30}}},
            ("GET", "https://api.live.vkvideo.ru/v1/channel/blog/record/r1/chat/"): answer,
        })
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        target = os.path.join(folder, "c.chat.json")
        count = vodchat.download("vkvideolive", "https://live.vkvideo.ru/blog/clip/" + RECORD, target,
                                 session=http, fetch=lambda url: None)
        self.assertEqual(count, 3)
        with open(target, encoding="utf-8") as source:
            self.assertEqual([c["content_offset_seconds"] for c in json.load(source)["comments"]], [5.0, 15.0, 25.0])

    def test_unknown_link(self):
        with self.assertRaises(chat.ChatError):
            vodchat.download("vkvideolive", "https://live.vkvideo.ru/blog", "x.chat.json", session=FakeHttp({}))


class VkRecordsTest(unittest.TestCase):
    """Список записей канала VK: открытый запрос сайта, страницами по offset."""

    def test_page(self):
        records = [{"id": "r1", "title": "эфир", "duration": 3600, "startTime": 1793000000, "processState": "done",
                    "previewUrl": "https://images/r1", "count": {"views": 12},
                    "blog": {"owner": {"displayName": "Блогер"}}},
                   {"id": "r2", "title": "идёт", "processState": "processing"}]
        http = FakeHttp({("GET", "https://api.live.vkvideo.ru/v1/blog/blog/public_video_stream/record/"):
                         {"data": {"records": records}, "extra": {"isLast": True}}})
        result = vkrecords.list_records("blog", limit=30, start=31, session=http)
        self.assertEqual(http.calls[0][2]["params"], {"limit": 30, "offset": 30})
        (entry,) = result["entries"]                    # ещё обрабатываемая — не в списке
        self.assertEqual(entry["url"], "https://live.vkvideo.ru/blog/record/r1")
        self.assertEqual((entry["platform"], entry["kind"], entry["channel"], entry["view_count"]),
                         ("vkvideolive", "vod", "Блогер", 12))
        self.assertTrue(chat.supported(entry["platform"], entry["kind"], entry["live_status"]))

    def test_clips(self):
        clips = [{"id": "c1", "title": "клип", "duration": 46, "videoCreatedAt": 1793000000, "state": "active",
                  "previewUrl": "https://images/c1", "count": {"views": 5}, "blog": {"owner": {"displayName": "Блогер"}}},
                 {"id": "c2", "title": "удалён", "state": "deleted", "deletedAt": 1793000100}]
        http = FakeHttp({("GET", "https://api.live.vkvideo.ru/v1/channel/blog/clip/"):
                         {"data": {"clips": clips}, "extra": {"isLast": True}}})
        result = vkrecords.list_records("blog", kind="clips", session=http)
        self.assertEqual(http.calls[0][2]["params"]["order"], "created")
        (entry,) = result["entries"]
        self.assertEqual((entry["kind"], entry["url"]), ("clip", "https://live.vkvideo.ru/blog/clip/c1"))
        self.assertTrue(chat.supported(entry["platform"], entry["kind"]))

    def test_unknown_channel(self):
        with self.assertRaisesRegex(vkrecords.VkRecordsError, "не найден"):
            vkrecords.list_records("nobody", session=FakeHttp({}))


class VkClipPluginTest(unittest.TestCase):
    """Расширение yt-dlp для клипов VK: подключено ко всем запускам yt-dlp и входит в сборку."""

    def test_plugin_dir(self):
        self.assertEqual(util.YTDLP[-2:], ["--plugin-dirs", util.YTDLP_PLUGINS])
        plugin = os.path.join(util.YTDLP_PLUGINS, "streamvault", "yt_dlp_plugins", "extractor",
                              "streamvault_vkclip.py")
        self.assertTrue(os.path.isfile(plugin))
        spec = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "packaging",
                            "StreamVault.spec")
        with open(spec, encoding="utf-8") as source:
            self.assertIn('"ytdlp_plugins"', source.read())

    def test_extractor_matches_clip_links(self):
        import importlib.util
        path = os.path.join(util.YTDLP_PLUGINS, "streamvault", "yt_dlp_plugins", "extractor", "streamvault_vkclip.py")
        module_spec = importlib.util.spec_from_file_location("streamvault_vkclip", path)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        clip = "https://live.vkvideo.ru/blog/clip/663e02df-a403-4682-9868-e04e9f362b81"
        self.assertTrue(module.VKPlayClipIE.suitable(clip))
        self.assertFalse(module.VKPlayClipIE.suitable(clip.replace("/clip/", "/record/")))


class RemuxTest(unittest.TestCase):

    def test_command(self):
        command = remux_command("ffmpeg.exe", "запись.ts", 75.5)
        self.assertEqual(command[command.index("-ss") + 1], "75.500")
        self.assertLess(command.index("-ss"), command.index("-i"))      # быстрый переход до чтения
        self.assertIn("aac_adtstoasc", command)                         # звук .ts в mp4
        self.assertEqual(command[command.index("-c") + 1], "copy")      # без перекодирования
        self.assertNotIn("-ss", remux_command("ffmpeg.exe", "запись.ts", 0))

    @unittest.skipUnless(FFMPEG, "нужен ffmpeg")
    def test_stream_from_position(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        source = os.path.join(folder, "запись.ts")
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=10:duration=6",
                        "-f", "lavfi", "-i", "sine=duration=6", "-c:v", "libx264", "-g", "10", "-c:a", "aac",
                        "-shortest", source], check=True)
        server = MediaServer(ffmpeg=lambda: FFMPEG)
        self.addCleanup(server.stop)
        import urllib.request
        with urllib.request.urlopen(server.remux_url(source) + "?start=2", timeout=30) as response:
            self.assertEqual(response.headers["Content-Type"], "video/mp4")
            data = response.read()
        self.assertEqual(data[4:8], b"ftyp")
        self.assertIn(b"moof", data)                    # фрагменты mp4 — играет по мере прихода
        mp4 = os.path.join(folder, "out.mp4")
        with open(mp4, "wb") as target:
            target.write(data)
        probe = subprocess.run([FFMPEG, "-i", mp4], capture_output=True, text=True)
        self.assertIn("Audio: aac", probe.stderr)
        no_ffmpeg = MediaServer(ffmpeg=lambda: None)
        self.addCleanup(no_ffmpeg.stop)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(no_ffmpeg.remux_url(source), timeout=10)


if __name__ == "__main__":
    unittest.main()
