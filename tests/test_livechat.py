# -*- coding: utf-8 -*-
"""
Чат прямых эфиров: разбор IRC, запись во время эфира, перевод в формат
TwitchDownloader и HTML после конвертации. Чат Twitch изображает локальный
сервер, TwitchDownloader — заглушка.
"""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest

from streamvault import util
from streamvault.archive import chat as archive_chat
from streamvault.live import livechat
from streamvault.live.probes import Status
from streamvault.live.recorder import ChannelRecorder
from streamvault.settings import Settings
from tests.helpers import OFFLINE, FakeProcessing, FakeStreamlink, ScriptedProbe, wait_for
from tests.test_chat import FAKE_TDL, FakeTdl, StubLocator

SENT = 1792000000000     # tmi-sent-ts первого сообщения, мс

PRIVMSG = ("@badge-info=subscriber/14;badges=subscriber/12,premium/1;color=#FF4500;"
           "display-name=Зритель;emotes=25:0-4/1902:18-22;id=m1;mod=0;room-id=111;"
           "tmi-sent-ts={ts};user-id=777 :zritel!zritel@zritel.tmi.twitch.tv PRIVMSG #chan "
           ":Kappa привет всем Keepo")
SUB = ("@badges=;color=;display-name=Даритель;emotes=;id=m2;login=daritel;"
       "msg-id=sub;system-msg=Даритель\\ssubscribed\\sat\\sTier\\s1.;tmi-sent-ts={ts};user-id=888 "
       ":tmi.twitch.tv USERNOTICE #chan :ура")
DELETED = ("@badges=;color=;display-name=Хам;emotes=;id=m3;tmi-sent-ts={ts};user-id=999 "
           ":ham!ham@ham.tmi.twitch.tv PRIVMSG #chan :плохое слово")
ACTION = ("@badges=;color=;display-name=Тихий;emotes=;id=m4;tmi-sent-ts={ts};user-id=555 "
          ":tihiy!tihiy@tihiy.tmi.twitch.tv PRIVMSG #chan :\x01ACTION машет рукой\x01")


class FakeTwitchChat:
    """IRC-сервер чата: каждое подключение получает script(connection_number) строк."""

    def __init__(self, script):
        self.script = script
        self.received = []       # что прислал клиент
        self.connections = 0
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def connect(self, host, port):
        self.target = (host, port)
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def _serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._talk, args=(conn, self.connections), daemon=True).start()

    def _talk(self, conn, number):
        with conn:
            conn.settimeout(5)
            buffer = b""
            while b"JOIN #" not in buffer:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buffer += chunk
            self.received.append(buffer.decode())
            for line in self.script(number):
                if line == "CLOSE":
                    return
                if line == "READ":
                    # дать клиенту ответить (PONG) и прочитать ответ
                    time.sleep(0.3)
                    self.received.append(conn.recv(4096).decode())
                    continue
                conn.sendall((line + "\r\n").encode("utf-8"))
                time.sleep(0.02)
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    self.received.append(chunk.decode())
            except OSError:
                return

    def close(self):
        self.server.close()


class ParseTest(unittest.TestCase):

    def test_line_with_tags(self):
        tags, command, params, text = livechat.parse_line(PRIVMSG.format(ts=SENT))
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(params, ["#chan"])
        self.assertEqual(text, "Kappa привет всем Keepo")
        self.assertEqual((tags["display-name"], tags["user-id"], tags["emotes"]),
                         ("Зритель", "777", "25:0-4/1902:18-22"))

    def test_tag_escapes(self):
        self.assertEqual(livechat.unescape_tag(r"a\sb\:c\\d"), "a b;c\\d")

    def test_ping(self):
        self.assertEqual(livechat.parse_line("PING :tmi.twitch.tv")[1:], ("PING", [], "tmi.twitch.tv"))


class RecordTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.recorded = os.path.join(self.dir, "chan - 2026-10-26 12h00m00s - стрим.ts")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def script(self, number):
        if number == 1:
            return [":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands",
                    "@room-id=111 :tmi.twitch.tv ROOMSTATE #chan",
                    PRIVMSG.format(ts=SENT), SUB.format(ts=SENT + 2000), DELETED.format(ts=SENT + 3000),
                    "@target-msg-id=m3 :tmi.twitch.tv CLEARMSG #chan :плохое слово",
                    "PING :tmi.twitch.tv", "READ", "CLOSE"]  # и связь рвётся
        return [ACTION.format(ts=SENT + 9000)]               # после переподключения

    def record(self):
        server = FakeTwitchChat(self.script)
        self.addCleanup(server.close)
        chat = livechat.TwitchChat("Chan", connect=server.connect)
        chat.RECONNECT_DELAYS = (0.1,)
        chat.begin_part(self.recorded, SENT / 1000 - 5)
        chat.start()
        self.assertTrue(wait_for(lambda: chat.messages >= 4, timeout=10))
        chat.end_part()
        chat.stop()
        chat.join(5)
        return server, chat

    def records(self):
        # метку конца части («end») проверяет отдельный тест
        with open(livechat.jsonl_path(self.recorded), encoding="utf-8") as source:
            return [record for record in map(json.loads, source) if record["type"] != "end"]

    def test_anonymous_login_and_pong(self):
        server, chat = self.record()
        login = server.received[0]
        self.assertIn("NICK justinfan", login)
        self.assertIn("JOIN #chan", login)
        self.assertIn("PONG :tmi.twitch.tv", "".join(server.received))
        self.assertEqual(server.connections, 2)              # переподключился после обрыва
        self.assertEqual(chat.room_id, "111")

    def test_jsonl_written_as_it_goes(self):
        self.record()
        kinds = [record["type"] for record in self.records()]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(self.records()[0]["platform"], "twitch")
        self.assertEqual(kinds.count("msg"), 4)
        self.assertIn("delete", kinds)

    def test_convert_to_twitchdownloader(self):
        self.record()
        target = os.path.join(self.dir, "chan.chat.json")
        self.assertEqual(livechat.convert(livechat.jsonl_path(self.recorded), target, title="стрим"), 3)
        with open(target, encoding="utf-8") as source:
            data = json.load(source)
        self.assertEqual(data["streamer"], {"name": "chan", "login": "chan", "id": 111})
        self.assertEqual(data["video"]["title"], "стрим")
        first, sub, action = data["comments"]
        self.assertEqual(first["content_offset_seconds"], 5.0)
        self.assertEqual(first["commenter"]["display_name"], "Зритель")
        self.assertEqual(first["commenter"]["_id"], "777")
        message = first["message"]
        self.assertEqual(message["user_badges"], [{"_id": "subscriber", "version": "12"},
                                                  {"_id": "premium", "version": "1"}])
        self.assertEqual(message["user_color"], "#FF4500")
        self.assertEqual([(f["text"], (f["emoticon"] or {}).get("emoticon_id")) for f in message["fragments"]],
                         [("Kappa", "25"), (" привет всем ", None), ("Keepo", "1902")])
        self.assertEqual(sub["message"]["body"], "Даритель subscribed at Tier 1. ура")
        self.assertEqual(action["message"]["body"], "машет рукой")
        # удалённое модератором в файл не попало
        self.assertNotIn("плохое слово", json.dumps(data, ensure_ascii=False))

    def test_ban_hides_messages_of_user(self):
        chat = livechat.TwitchChat("chan")
        chat.begin_part(self.recorded, 0)
        chat._on_line(None, "@room-id=111;target-user-id=777 :tmi.twitch.tv CLEARCHAT #chan :zritel")
        chat._on_line(None, "@room-id=111 :tmi.twitch.tv CLEARCHAT #chan")      # весь чат — не трогаем
        chat.end_part()
        self.assertEqual(self.records()[1:], [{"type": "delete_user", "user_id": "777"}])

    def test_empty_chat_makes_no_file(self):
        with open(livechat.jsonl_path(self.recorded), "w", encoding="utf-8") as target:
            target.write(json.dumps({"type": "start", "ts": 1, "channel": "chan"}) + "\n{оборвано")
        target = os.path.join(self.dir, "chan.chat.json")
        self.assertEqual(livechat.convert(livechat.jsonl_path(self.recorded), target), 0)
        self.assertFalse(os.path.exists(target))


class FinisherTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.settings = Settings(os.path.join(self.dir, "settings.json"),
                                 {"root_path": self.dir, "live_chat": "text"})
        self.recorded = os.path.join(self.dir, util.IN_PROGRESS_DIR, "twitch", "chan",
                                     "chan - 2026-10-26 12h00m00s - стрим.ts")
        self.video = os.path.join(self.dir, util.FINISHED_DIR, "twitch", "chan",
                                  "chan - 2026-10-26 12h00m00s - стрим.mp4")
        for path in (self.recorded, self.video):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(livechat.jsonl_path(self.recorded), "w", encoding="utf-8") as target:
            for record in ({"type": "start", "ts": 100.0, "channel": "chan", "room_id": "111"},
                           {"type": "msg", "ts": 101.5, "id": "a", "login": "x", "name": "X", "text": "привет",
                            "emotes": "", "badges": "", "color": "", "bits": "", "user_id": "1"}):
                target.write(json.dumps(record, ensure_ascii=False) + "\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def finisher(self, command=FAKE_TDL):
        finisher = livechat.ChatFinisher(self.settings, StubLocator(path="ffmpeg.exe"), StubLocator(command),
                                         threading.Event())
        self.addCleanup(finisher.stop)
        return finisher

    def finished(self, path):
        """Файл готов и работа над чатом кончилась: строка журнала пишется уже после файла."""
        finishers = [finisher for finisher in threading.enumerate() if isinstance(finisher, livechat.ChatFinisher)]
        return wait_for(lambda: os.path.exists(path) and all(not finisher._queued for finisher in finishers),
                        timeout=10)

    def test_json_then_html(self):
        finisher = self.finisher()
        with self.assertLogs(level="INFO"):
            finisher.finish(self.recorded, self.video)
        paths = archive_chat.chat_paths(self.video)
        self.assertTrue(os.path.exists(paths["json"]))
        self.assertFalse(os.path.exists(livechat.jsonl_path(self.recorded)))
        with FakeTdl("ok"), self.assertLogs(level="INFO"):
            finisher.start()
            self.assertTrue(self.finished(paths["html"]))
        self.assertFalse(os.path.exists(paths["video"]))      # видео чата — только в режиме render

    def test_render_mode(self):
        self.settings.update(live_chat="render")
        finisher = self.finisher()
        with FakeTdl("ok"), self.assertLogs(level="INFO"):
            finisher.finish(self.recorded, self.video)
            finisher.start()
            self.assertTrue(self.finished(archive_chat.chat_paths(self.video)["video"]))

    def test_without_twitchdownloader_waits_for_it(self):
        finisher = self.finisher(command=None)
        with self.assertLogs(level="INFO"):
            finisher.finish(self.recorded, self.video)
            finisher.start()
            time.sleep(0.5)
        paths = archive_chat.chat_paths(self.video)
        self.assertTrue(os.path.exists(paths["json"]))
        self.assertFalse(os.path.exists(paths["html"]))
        # поставили TwitchDownloader — при следующем запуске чат подхватится
        later = self.finisher()
        with FakeTdl("ok"), self.assertLogs(level="INFO"):
            later.start()
            self.assertTrue(self.finished(paths["html"]))

    def test_unfinished_chat_video_is_made_on_start(self):
        # программу закрыли, пока рисовалось видео чата; запись осталась в .ts
        self.settings.update(live_chat="render")
        video = os.path.splitext(self.video)[0] + ".ts"
        open(video, "wb").close()
        with self.assertLogs(level="INFO"):
            self.finisher().finish(self.recorded, video)
        paths = archive_chat.chat_paths(video)
        with open(paths["html"], "w", encoding="utf-8") as target:
            target.write("готовый")
        later = self.finisher()
        with FakeTdl("ok"), self.assertLogs(level="INFO"):
            later.start()
            self.assertTrue(self.finished(paths["video"]))
        with open(paths["html"], encoding="utf-8") as source:
            self.assertEqual(source.read(), "готовый")   # HTML второй раз не делается

    def test_twitch_emotes_embedded_into_json_first(self):
        # смайлы и значки — внутрь .chat.json: HTML и видео чата без сети, а в окне видны все смайлы
        self.settings.update(live_chat="render")
        log = os.path.join(self.dir, "tdl.log")
        finisher = self.finisher()
        paths = archive_chat.chat_paths(self.video)
        with FakeTdl("ok", log=log), self.assertLogs(level="INFO"):
            finisher.finish(self.recorded, self.video)
            finisher.start()
            self.assertTrue(self.finished(paths["video"]))
        with open(log, encoding="utf-8") as source:
            embed, html, render = [json.loads(line) for line in source]
        self.assertEqual(embed[:3], ["chatupdate", "-i", paths["json"]])
        self.assertIn("--embed-missing", embed)
        self.assertNotIn("--embed-missing", html)
        self.assertIn("--offline", render)
        with open(paths["json"], encoding="utf-8") as source:
            self.assertEqual(source.read(), "чат chatupdate")     # встроенный встал на место
        self.assertEqual(sorted(name for name in os.listdir(os.path.dirname(self.video)) if "embedding" in name), [])

    def test_other_platform_needs_nothing_from_twitch(self):
        # смайлы и значки Kick уже внутри .chat.json: TwitchDownloader не ищет их у Twitch
        self.settings.update(live_chat="render")
        recorded = self.recorded.replace(os.sep + "twitch" + os.sep, os.sep + "kick" + os.sep)
        video = self.video.replace(os.sep + "twitch" + os.sep, os.sep + "kick" + os.sep)
        os.makedirs(os.path.dirname(recorded), exist_ok=True)
        os.makedirs(os.path.dirname(video), exist_ok=True)
        with open(livechat.jsonl_path(recorded), "w", encoding="utf-8") as target:
            for record in ({"type": "start", "ts": 100.0, "platform": "kick", "channel": "chan"},
                           {"type": "msg", "ts": 101.5, "id": "a", "login": "x", "name": "X", "user_id": "1",
                            "color": "", "badges": [], "parts": [["привет"]], "text": "привет", "system": ""}):
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
        log = os.path.join(self.dir, "tdl.log")
        finisher = self.finisher()
        with FakeTdl("ok", log=log), self.assertLogs(level="INFO"):
            finisher.finish(recorded, video)
            finisher.start()
            self.assertTrue(self.finished(archive_chat.chat_paths(video)["video"]))
        with open(log, encoding="utf-8") as source:
            html, render = [json.loads(line) for line in source]
        self.assertNotIn("--embed-missing", html)
        self.assertIn("--offline", render)

    def test_no_chat_file_nothing_to_do(self):
        os.remove(livechat.jsonl_path(self.recorded))
        self.finisher().finish(self.recorded, self.video)
        self.assertEqual(os.listdir(os.path.dirname(self.video)), [])


class RecorderChatTest(unittest.TestCase):
    """Чат пишется рядом с частью записи, пока идёт эфир."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.processing = FakeProcessing()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_chat_next_to_recording(self):
        server = FakeTwitchChat(lambda number: ["@room-id=111 :tmi.twitch.tv ROOMSTATE #c",
                                                PRIVMSG.format(ts=int(time.time() * 1000))])
        self.addCleanup(server.close)
        chats = []

        def factory(platform, channel):
            chat = livechat.TwitchChat(channel, connect=server.connect)
            chats.append(chat)
            return chat

        probe = ScriptedProbe((Status.ONLINE, {"title": "стрим"}), OFFLINE)
        recorder = ChannelRecorder("twitch", "c", probe, "best", 15, self.root, self.processing,
                                   live_chat=lambda: "text", chat_factory=factory)
        recorder.prepare_directories()
        with FakeStreamlink("forever"), self.assertLogs(level="INFO"):
            worker = threading.Thread(target=recorder._iteration)
            worker.start()
            self.assertTrue(wait_for(lambda: chats and chats[0].messages == 1, timeout=10))
            self.assertEqual(recorder.snapshot()["recording"]["chat_messages"], 1)
            recorder.stop(grace=0)
            worker.join(15)
        recorded = self.processing.submitted[0][0]
        with open(livechat.jsonl_path(recorded), encoding="utf-8") as source:
            records = [json.loads(line) for line in source]
        # часть кончилась — последней строкой метка конца, перед ней сообщение
        self.assertEqual([record["type"] for record in records][-2:], ["msg", "end"])
        self.assertGreaterEqual(records[-1]["ts"], records[0]["ts"])
        self.assertTrue(wait_for(lambda: not chats[0].is_alive(), timeout=5))

    def test_platform_without_reader_and_setting_off(self):
        made = []
        for platform, mode in (("kick", "text"), ("twitch", "none")):
            probe = ScriptedProbe((Status.ONLINE, {"title": "стрим"}), OFFLINE)
            recorder = ChannelRecorder(platform, "c", probe, "best", 15, self.root, self.processing,
                                       live_chat=lambda mode=mode: mode,
                                       chat_factory=lambda *args: made.append(args))   # None — чата нет
            recorder._sleep = lambda seconds: None        # паузы между проверками не ждём
            recorder.prepare_directories()
            with FakeStreamlink("once"), self.assertLogs(level="INFO"):
                recorder._iteration()
        # чат выключен — его и не спрашивают; нет читателя — запись идёт без чата
        self.assertEqual(made, [("kick", "c")])


if __name__ == "__main__":
    unittest.main()
