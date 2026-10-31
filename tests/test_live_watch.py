# -*- coding: utf-8 -*-
"""
Просмотр записи, которая ещё идёт: чат из растущего .chat.jsonl, поток из
растущего .ts, сдвиг чата, когда запись взяла эфир с начала.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import uuid

from streamvault import util
from streamvault.chatreplay import TWITCH_EMOTE, LiveChatReplay
from streamvault.live import livechat
from streamvault.settings import Settings
from streamvault.ui.api import Api
from streamvault.ui.media import FOLLOW_TIMEOUT, remux_command
from tests.test_ui import StubDownloads, StubLive

FFMPEG = shutil.which("ffmpeg")


def msg(ts, text, **extra):
    record = {"type": "msg", "ts": ts, "id": uuid.uuid4().hex, "user_id": extra.pop("user_id", "1"),
              "login": "x", "name": extra.pop("name", "Зритель"), "color": "", "badges": "", "emotes": "",
              "bits": "", "text": text, "system": ""}
    record.update(extra)
    return record


class LiveChatTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "chan - 2026-10-28 12h00m00s.chat.jsonl")

    def write(self, *records, partial=""):
        with open(self.path, "a", encoding="utf-8") as target:
            for record in records:
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.write(partial)

    def test_reads_as_the_file_grows(self):
        self.write({"type": "start", "ts": 100.0, "platform": "twitch", "channel": "chan"},
                   msg(101, "Kappa привет", emotes="25:0-4"))
        replay = LiveChatReplay(self.path)
        self.assertEqual(replay.count, 1)
        first = replay.window(0, 60)[0]
        self.assertEqual(first[0], 1.0)
        # смайл Twitch — сразу адресом: встроенных картинок у идущей записи нет
        self.assertEqual(first[4], [[TWITCH_EMOTE.format(id="25"), "Kappa"], " привет"])
        self.assertEqual(replay.images, [])
        # дописали — и оборванную строку тоже: она дочитается в следующий раз
        line = json.dumps(msg(130, "позже"), ensure_ascii=False)
        self.write(msg(110, "ещё"), partial=line[:20])
        self.assertEqual([m[4] for m in replay.window(0, 60)], [first[4], ["ещё"]])
        self.write(partial=line[20:] + "\n")
        self.assertEqual(replay.window(25, 60)[0][4], ["позже"])
        self.assertEqual(replay.count, 3)

    def test_other_platforms_parts_badges_and_moderation(self):
        self.write({"type": "start", "ts": 100.0, "platform": "kick", "channel": "chan"},
                   {"type": "emote", "id": "e1", "name": "KEKW", "url": "https://files.kick.com/e1"},
                   {"type": "badge", "id": "kick-sub", "name": "Подписчик", "url": "https://files.kick.com/b"},
                   msg(102, "", parts=[["KEKW", "e1"], [" смешно"]], badges=["kick-sub"], user_id="7"),
                   msg(103, "", parts=[["удалят"]], id="gone"),
                   msg(104, "", parts=[["забанят"]], user_id="9"),
                   {"type": "delete", "id": "gone"},
                   {"type": "delete_user", "user_id": "9", "ts": 105.0},
                   msg(106, "", parts=[["после бана"]], user_id="9"))
        replay = LiveChatReplay(self.path)
        shown = replay.window(0, 60)
        self.assertEqual([m[4] for m in shown], [[["https://files.kick.com/e1", "KEKW"], " смешно"], ["после бана"]])
        self.assertEqual(shown[0][3], ["https://files.kick.com/b"])
        self.assertEqual([m[0] for m in replay.search("СМЕШНО")], [2.0])

    def test_shift_when_recording_took_the_stream_from_its_start(self):
        self.write({"type": "start", "ts": 100.0, "platform": "twitch", "channel": "chan"}, msg(110, "раз"))
        replay = LiveChatReplay(self.path)
        replay.shift = 300.0
        self.assertEqual(replay.window(0, 60), [])
        self.assertEqual(replay.window(300, 320)[0][0], 310.0)

    def test_backlog(self):
        # видео длиннее, чем шла часть, — запись взяла эфир с начала
        self.assertEqual(livechat.video_backlog(1000, 100, 400), 700)
        # обычная запись: streamlink начинает на пару сегментов позади эфира
        self.assertEqual(livechat.video_backlog(306, 100, 400), 0.0)
        self.assertEqual(livechat.video_backlog(None, 100, 400), 0.0)
        self.write({"type": "start", "ts": 100.0}, msg(101, "x"), {"type": "end", "ts": 400.0})
        self.assertEqual(livechat.part_times(self.path), (100.0, 400.0))
        target = os.path.join(self.dir, "chan.chat.json")
        livechat.convert(self.path, target, shift=700)
        with open(target, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(data["comments"][0]["content_offset_seconds"], 701.0)
        self.assertEqual(data["video"]["length"], 701)


class RemuxTest(unittest.TestCase):

    def test_follow_reads_a_growing_file(self):
        command = remux_command("ffmpeg", "x.ts", 12.5, follow=True)
        self.assertEqual(command[command.index("-follow") + 1], "1")
        self.assertEqual(command[command.index("-rw_timeout") + 1], str(FOLLOW_TIMEOUT))
        self.assertLess(command.index("-follow"), command.index("-i"))   # настройка чтения файла
        self.assertNotIn("-follow", remux_command("ffmpeg", "x.ts", 0))


class Recording(StubLive):
    """Канал, который сейчас пишется: снимок как у LiveService."""

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.state = "recording"

    def channel_snapshot(self, cid):
        if cid != "twitch:chan":
            raise KeyError(cid)
        return {"id": cid, "platform": "twitch", "channel": "chan", "label": "chan", "state": self.state,
                "stream": {"title": "Эфир"},
                "recording": {"file": self.path, "part": 1, "started": 1.0} if self.state == "recording" else None}


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class WatchLiveApiTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        root = os.path.join(self.dir, "streams")
        folder = os.path.join(root, util.IN_PROGRESS_DIR, "twitch", "chan")
        os.makedirs(folder)
        self.ts = os.path.join(folder, "chan - 2026-10-28 12h00m00s - эфир.ts")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=6:s=160x90:r=25",
                        "-c:v", "libx264", "-preset", "ultrafast", "-f", "mpegts", self.ts], check=True)
        with open(livechat.jsonl_path(self.ts), "w", encoding="utf-8") as target:
            for record in ({"type": "start", "ts": time.time() - 6, "platform": "twitch"},
                           msg(time.time() - 3, "привет")):
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.live = Recording(self.ts)

        class Locator:
            def path(self):
                return FFMPEG

        self.api = Api(Settings(os.path.join(self.dir, "settings.json"), {"root_path": root}),
                       self.live, StubDownloads(root), ffmpeg=Locator())
        self.addCleanup(self.api._media_server.stop)

    def test_watch_live_and_status(self):
        result = self.api.call("watch_live", ["twitch:chan"])
        self.assertTrue(result["ok"], result)
        result = result["result"]
        self.assertTrue(result["live"] and result["remux"])
        self.assertIn("/remux/", result["video"])
        self.assertAlmostEqual(result["duration"], 6, delta=0.5)
        self.assertEqual((result["item"]["title"], result["item"]["path"]), ("Эфир", self.ts))
        chat = result["chat"]
        self.assertTrue(chat["live"])
        self.assertEqual(chat["count"], 1)
        messages = self.api.call("chat_replay", [chat["id"], 0, 60])["result"]
        self.assertEqual(messages[0][4], ["привет"])
        # тот же чат при повторном открытии — он дочитывается, а не открывается заново
        self.assertEqual(self.api.watch_live("twitch:chan")["chat"]["id"], chat["id"])
        self.assertEqual(self.api.live_status("twitch:chan", self.ts)["growing"], True)

    def test_part_ended_and_converted(self):
        self.api.watch_live("twitch:chan")
        # часть кончилась, эфир пошёл новой частью, а старую .ts уже сконвертировали
        self.live.path = self.ts.replace(".ts", " - part02.ts")
        os.remove(self.ts)
        status = self.api.call("live_status", ["twitch:chan", self.ts])["result"]
        self.assertEqual(status, {"duration": None, "growing": False, "next": True})
        self.live.state = "offline"
        self.assertEqual(self.api.live_status("twitch:chan", self.ts)["next"], False)
        # путь вне папок программы — по-прежнему нельзя
        self.assertFalse(self.api.call("live_status", ["twitch:chan", "/etc/passwd.ts"])["ok"])

    def test_not_recording(self):
        self.live.state = "live"
        self.assertFalse(self.api.call("watch_live", ["twitch:chan"])["ok"])
        self.assertFalse(self.api.call("watch_live", ["kick:нет"])["ok"])


if __name__ == "__main__":
    unittest.main()
