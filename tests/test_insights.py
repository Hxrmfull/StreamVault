# -*- coding: utf-8 -*-
"""
Что программа узнаёт о записях: пропуски, кадры для шкалы, график чата,
поиск по чатам всех записей, сводка эфира, свои настройки записи у канала.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest

from streamvault import integrity, platforms, util
from streamvault.chatindex import ChatIndex
from streamvault.chatreplay import ChatReplay, activity, activity_bucket, peaks
from streamvault.events import EventBus
from streamvault.integrity import IntegrityChecker, IntegrityStore, analyse, map_to_pieces
from streamvault.library import Library, probe_duration
from streamvault.live.processing import ProcessingQueue
from streamvault.settings import Settings, channel_setting
from streamvault.storyboard import Storyboards, interval_for
from streamvault.ui.api import Api
from tests.helpers import wait_for
from tests.test_ui import StubDownloads, StubLive

FFMPEG = shutil.which("ffmpeg")


def framecrc(times, step=3000, tb="1/90000"):
    """Строки framecrc: кадр на каждое время (в единицах 1/90000), длительность step."""
    return ["#tb 0: " + tb] + ["0, %d, %d, %d, 100, 0x0" % (dts, dts, step) for dts in times]


def comment(offset, name, text, emote=None):
    fragments = [{"text": text}]
    if emote:
        fragments.append({"text": emote, "emoticon": {"emoticon_id": "25"}})
    return {"content_offset_seconds": offset, "commenter": {"display_name": name},
            "message": {"body": text, "fragments": fragments, "user_badges": []}}


def write_chat(path, comments):
    with open(path, "w", encoding="utf-8") as target:
        json.dump({"video": {"start": 0}, "comments": comments, "embeddedData": {}}, target, ensure_ascii=False)


class AnalyseTest(unittest.TestCase):
    """Пропуски по времени кадров: без ffmpeg, на строках framecrc."""

    def test_ts_jumps(self):
        # 10 с кадров, прыжок на 3 с (останется) и на 20 с (конвертация склеит)
        times = list(range(0, 900000, 3000))
        times += [t + 270000 for t in range(900000, 1800000, 3000)]
        times += [t + 270000 + 1800000 for t in range(1800000, 2700000, 3000)]
        gaps, duration = analyse(framecrc(times), discontinuity=True)
        self.assertEqual([(gap["at"], gap["length"], gap["kept"]) for gap in gaps],
                         [(10.0, 3.0, True), (23.0, 20.0, False)])
        self.assertEqual(duration, 33.0)

    def test_mp4_long_frame(self):
        # в mp4 пропуск — кадр, который длится весь пропуск: смотрится шаг, а не длительность
        times = list(range(0, 900000, 3000)) + list(range(900000 + 270000, 1800000, 3000))
        lines = ["#tb 0: 1/90000"]
        for index, dts in enumerate(times):
            following = times[index + 1] if index + 1 < len(times) else dts + 3000
            lines.append("0, %d, %d, %d, 100, 0x0" % (dts, dts, following - dts))
        gaps, _ = analyse(lines, discontinuity=False)
        self.assertEqual([(gap["at"], gap["length"], gap["kept"]) for gap in gaps], [(10.0, 3.0, True)])

    def test_reset_and_smooth(self):
        gaps, duration = analyse(framecrc(list(range(0, 300000, 3000)) + list(range(0, 300000, 3000))))
        self.assertEqual(gaps, [{"at": 3.333, "length": None, "kept": False}])
        self.assertAlmostEqual(duration, 6.667, places=2)
        # неровность меньше GAP_MIN — не пропуск
        self.assertEqual(analyse(framecrc([0, 3000, 6000, 90000, 93000]))[0], [])

    def test_map_to_pieces(self):
        gaps = [{"at": 5.0, "length": 2.0, "kept": True}, {"at": 70.0, "length": None, "kept": False},
                {"at": 125.0, "length": 9.0, "kept": True}]
        self.assertEqual(map_to_pieces(gaps, [60.0, 60.0, 60.0]),
                         [[gaps[0]], [dict(gaps[1], at=10.0)], [dict(gaps[2], at=5.0)]])
        self.assertEqual(integrity.summary(gaps), {"count": 3, "lost": 11.0})

    def test_store(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        video = os.path.join(folder, "a.mp4")
        with open(video, "wb") as target:
            target.write(b"1")
        store = IntegrityStore(os.path.join(folder, "cache", "integrity.json"))
        self.assertIsNone(store.get(video))
        store.put(video, [{"at": 1.0, "length": 3.0, "kept": True}], 10.0)
        self.assertEqual(IntegrityStore(store.path).get(video)["duration"], 10.0)    # с диска
        # файл переписали — прошлая проверка не про него
        with open(video, "ab") as target:
            target.write(b"2")
        self.assertIsNone(store.get(video))
        self.assertIs(IntegrityStore.at(store.path), IntegrityStore.at(store.path))


class ChatMathTest(unittest.TestCase):

    def test_activity_and_peaks(self):
        self.assertEqual(activity_bucket(600), 10)
        self.assertEqual(activity_bucket(6 * 3600), 90)
        self.assertEqual(activity([1, 2, 15, 59.9], 60, 20), [3, 0, 1])
        counts = [1] * 100
        counts[10], counts[11], counts[60] = 30, 25, 20
        # соседний с пиком отрезок — не отдельный всплеск
        self.assertEqual(peaks(counts), [10, 60])
        self.assertEqual(peaks([2] * 50), [])        # ровный чат всплесков не даёт
        self.assertEqual(peaks([]), [])


class ChatIndexTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.folder = os.path.join(self.dir, "finished", "kick", "chan")
        os.makedirs(self.folder)
        self.first = os.path.join(self.folder, "chan - 2026-10-27 18h00m00s - эфир.chat.json")
        self.second = os.path.join(self.folder, "chan - 2026-10-28 18h00m00s - другой.chat.json")
        for chat_path in (self.first, self.second):
            open(chat_path[:-len(".chat.json")] + ".mp4", "wb").close()
        comments = [comment(5, "Алиса", "Привет ВСЕМ"), comment(6, "bob", "gg", emote="KEKW"),
                    comment(7, "Алиса", "ещё раз привет", emote="KEKW")]
        # всплеск чата на второй минуте
        comments += [comment(120 + index / 10, "fan%d" % (index % 5), "ура") for index in range(40)]
        comments += [comment(200 + index * 20, "bob", "тишина") for index in range(20)]
        write_chat(self.first, comments)
        write_chat(self.second, [comment(1, "Вася", "приветики")])
        self.index = ChatIndex(os.path.join(self.dir, "cache", "chats.sqlite"), lambda: [self.dir])
        self.addCleanup(self.index.close)

    def test_search(self):
        found = self.index.search("ПРИВЕТ")
        self.assertEqual(found["pending"], 0)
        by_chat = {result["chat"]: result for result in found["results"]}
        # кириллица — без учёта регистра, подстрока находит и «приветики»
        self.assertEqual(by_chat[self.first]["count"], 2)
        self.assertEqual(by_chat[self.second]["messages"], [[1.0, "Вася", "приветики"]])
        self.assertTrue(by_chat[self.first]["video"].endswith("эфир.mp4"))
        # смайл ищется по названию, ник — только в режиме «по нику»
        self.assertEqual(self.index.search("kekw")["results"][0]["count"], 2)
        self.assertEqual(self.index.search("алиса", by="name")["results"][0]["count"], 2)
        self.assertEqual(self.index.search("ура", by="name")["results"], [])

    def test_changes_on_disk(self):
        self.index.search("x")
        write_chat(self.second, [comment(1, "Вася", "новое сообщение")])
        os.utime(self.second, (time.time() + 5, time.time() + 5))
        self.assertEqual(self.index.search("новое")["results"][0]["count"], 1)
        os.remove(self.first)
        self.assertEqual(self.index.search("привет")["results"], [])
        # чаты переписываются в базу по частям: за раз — не дольше бюджета
        self.assertEqual(self.index.sync(budget=0), 0)

    def test_summary(self):
        summary = self.index.summary([self.first, self.second], [600, 30])
        self.assertEqual(summary["messages"], 64)
        self.assertEqual(summary["chatters"], 8)
        self.assertEqual(summary["top_chatters"][0], ["bob", 21])
        self.assertEqual(summary["top_emotes"], [["KEKW", 2]])
        self.assertEqual([part["messages"] for part in summary["parts"]], [63, 1])
        self.assertEqual([(moment["part"], moment["at"]) for moment in summary["moments"]], [(0, 120)])


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class MediaTest(unittest.TestCase):
    """Пропуски и раскадровка на настоящих файлах ffmpeg."""

    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.mkdtemp()
        pieces = []
        # 10 с, прыжок на 20 с (склеится) и на 3 с (останется)
        for index, offset in enumerate((0, 30, 43)):
            piece = os.path.join(cls.samples, "%d.ts" % index)
            subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=10:s=160x90:r=25",
                            "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-output_ts_offset", str(offset),
                            "-f", "mpegts", piece], check=True)
            pieces.append(piece)
        cls.gappy = os.path.join(cls.samples, "gappy.ts")
        with open(cls.gappy, "wb") as target:
            for piece in pieces:
                with open(piece, "rb") as source:
                    target.write(source.read())
        cls.audio = os.path.join(cls.samples, "audio.m4a")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=3", "-c:a", "aac", cls.audio],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def quiet(self):
        """Конвертация и проверка пишут в журнал — в тестах он не нужен."""
        logs = self.assertLogs(level="INFO")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def test_scan(self):
        result = integrity.scan(FFMPEG, self.gappy)
        self.assertEqual([(gap["at"], gap["kept"]) for gap in result["gaps"]], [(10.0, False), (20.0, True)])
        self.assertAlmostEqual(result["gaps"][1]["length"], 3.0, places=1)
        self.assertAlmostEqual(result["duration"], 33.0, delta=0.5)
        self.assertEqual(integrity.scan(FFMPEG, self.audio)["gaps"], [])
        self.assertIsNone(integrity.scan(None, self.gappy))

    def test_processing_checks_before_conversion(self):
        # после конвертации длинный пропуск склеен и не виден — его ищут в .ts
        self.quiet()
        root = os.path.join(self.dir, "streams")
        recorded = os.path.join(root, util.IN_PROGRESS_DIR, "kick", "chan", "chan - 2026-10-28 12h00m00s.ts")
        os.makedirs(os.path.dirname(recorded))
        shutil.copy(self.gappy, recorded)
        store = IntegrityStore(os.path.join(self.dir, "integrity.json"))
        done = []
        queue = ProcessingQueue(FFMPEG, False, threading.Event(), root_path=root, settle_seconds=0,
                                on_done=lambda source, target: done.append(target), integrity=store)
        processed = os.path.join(root, util.FINISHED_DIR, "kick", "chan", os.path.basename(recorded))
        queue._process(recorded, processed)
        checked = store.get(done[0])
        self.assertEqual([gap["kept"] for gap in checked["gaps"]], [False, True])
        self.assertAlmostEqual(checked["duration"], probe_duration(FFMPEG, done[0]), delta=0.5)
        self.assertEqual(integrity.scan(FFMPEG, done[0])["gaps"][0]["at"], checked["gaps"][1]["at"])

    def test_checker(self):
        self.quiet()
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))
        store = IntegrityStore(os.path.join(self.dir, "integrity.json"))
        checker = IntegrityChecker(lambda: FFMPEG, store, bus)
        checker.start()
        self.addCleanup(checker.stop)
        self.assertTrue(checker.check(self.gappy))
        self.assertTrue(wait_for(lambda: events, timeout=30))
        self.assertEqual(events[0]["type"], "integrity")
        self.assertEqual(len(events[0]["gaps"]), 2)
        self.assertEqual(len(store.get(self.gappy)["gaps"]), 2)

    def test_storyboard(self):
        boards = Storyboards(self.dir, lambda: FFMPEG)
        self.assertEqual(boards.get(self.gappy, 33)["state"], "working")
        self.assertTrue(wait_for(lambda: boards.get(self.gappy, 33)["state"] != "working", timeout=60))
        board = boards.get(self.gappy, 33)
        self.assertEqual((board["state"], board["interval"], board["columns"]), ("ready", 2, 10))
        self.assertTrue(all(os.path.getsize(sheet) for sheet in board["sheets"]))
        # у звука кадров нет — и это запоминается
        boards.get(self.audio, 3)
        self.assertTrue(wait_for(lambda: boards.get(self.audio, 3)["state"] == "none", timeout=60))
        self.assertEqual(Storyboards(self.dir, lambda: None).get(self.dir + "/нет.mp4", 10), {"state": "none"})
        self.assertEqual(interval_for(6 * 3600), 44)


class ChannelOptionsTest(unittest.TestCase):

    def test_values(self):
        self.assertIs(platforms.channel_option_value("record_from_start", False), False)
        self.assertEqual(platforms.channel_option_value("split_size_gb", 3), 3.0)
        self.assertEqual(platforms.channel_option_value("split_count", 4.0), 4)
        for key, value in (("record_from_start", "да"), ("live_chat", "всё"), ("split_minutes", 1),
                           ("split_count", True), ("quality", "best"), ("split_size_gb", "3,5")):
            with self.assertRaises(ValueError, msg=key):
                platforms.channel_option_value(key, value)

    def test_settings_file(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with self.assertLogs(level="WARNING"):
            settings = Settings(os.path.join(folder, "settings.json"), {"channels": [
                {"platform": "kick", "channel": "Chan", "options": {"split_mode": "count", "split_count": 50,
                                                                     "live_chat": "всё"}}]})
        # негодное пропускается с записью в лог, годное остаётся
        self.assertEqual(settings["channels"][0]["options"], {"split_mode": "count", "split_count": 50})
        self.assertEqual(channel_setting(settings, "split_mode", "kick", "chan"), "count")
        self.assertEqual(channel_setting(settings, "live_chat", "kick", "chan"), settings["live_chat"])
        self.assertEqual(channel_setting(settings, "split_mode", "twitch", "chan"), "off")
        self.assertEqual(util.recording_channel(folder, os.path.join(folder, "finished", "kick", "chan", "a.mp4")),
                         ("kick", "chan"))
        self.assertEqual(util.recording_channel(folder, os.path.join(folder, "downloads", "a.mp4")), (None, None))

    def test_api(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        live = StubLive()
        live.set_options = lambda cid, changes: live.calls.append(("options", cid, changes))
        api = Api(Settings(os.path.join(folder, "settings.json"), {"root_path": folder}), live, StubDownloads(folder))
        self.addCleanup(api._media_server.stop)
        api.set_channel_options("kick:chan", {"split_mode": "size", "split_size_gb": 2.5, "live_chat": None})
        self.assertEqual(live.calls[-1], ("options", "kick:chan", {"split_mode": "size", "split_size_gb": 2.5,
                                                                   "live_chat": None}))
        self.assertFalse(api.call("set_channel_options", ["kick:chan", {"split_count": 1}])["ok"])
        self.assertFalse(api.call("set_channel_options", ["kick:chan", {"root_path": "/"}])["ok"])


class ApiTest(unittest.TestCase):
    """Окно спрашивает: график чата, кадры, проверку, поиск и сводку."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "streams")
        folder = os.path.join(self.root, util.FINISHED_DIR, "kick", "chan")
        os.makedirs(folder)
        self.parts = [os.path.join(folder, "chan - 2026-10-28 12h00m00s - эфир%s.mp4" % suffix)
                      for suffix in ("", " - part02")]
        for part in self.parts:
            with open(part, "wb") as target:
                target.write(b"0" * 100)
        write_chat(self.parts[1][:-4] + ".chat.json",
                   [comment(1, "Алиса", "привет")] + [comment(60 + index / 10, "fan", "ура") for index in range(30)]
                   + [comment(100 + index * 30, "bob", "...") for index in range(20)])
        settings = Settings(os.path.join(self.dir, "settings.json"), {"root_path": self.root})
        downloads = StubDownloads(self.root)
        self.store = IntegrityStore(os.path.join(self.dir, "integrity.json"))
        self.store.put(self.parts[0], [{"at": 5.0, "length": 2.0, "kept": True}], 700.0)

        class Locator:
            def path(self):
                return None

        library = Library(settings, downloads, os.path.join(self.dir, "cache"), Locator(), integrity=self.store)
        self.index = ChatIndex(os.path.join(self.dir, "cache", "chats.sqlite"),
                               lambda: list(library.folders().values()))
        self.addCleanup(self.index.close)
        self.api = Api(settings, StubLive(), downloads, library=library, integrity=self.store,
                       storyboards=Storyboards(os.path.join(self.dir, "cache"), lambda: None),
                       chat_index=self.index)
        self.addCleanup(self.api._media_server.stop)

    def test_watch_and_library_know_gaps(self):
        self.assertEqual(self.api.watch(self.parts[0])["gaps"], [{"at": 5.0, "length": 2.0, "kept": True}])
        self.assertIsNone(self.api.watch(self.parts[1])["gaps"])
        items = {item["path"]: item for item in self.api.library()["items"]}
        self.assertEqual(items[self.parts[0]]["gaps"], {"count": 1, "lost": 2.0})
        self.assertIsNone(items[self.parts[1]]["gaps"])
        # без ffmpeg проверять нечем — честная ошибка, а не тишина
        self.assertFalse(self.api.call("check_recording", [self.parts[1]])["ok"])
        self.assertEqual(self.api.storyboard(self.parts[0]), {"state": "none"})

    def test_chat_activity(self):
        chat_id = self.api.watch(self.parts[1])["chat"]["id"]
        answer = self.api.chat_activity(chat_id, 700)
        self.assertEqual(answer["bucket"], 10)
        self.assertEqual(answer["counts"][6], 30)
        self.assertEqual(answer["peaks"], [6])
        self.assertFalse(self.api.call("chat_activity", ["нет такого", 10])["ok"])

    def test_search_and_summary(self):
        found = self.api.search_chats("ПРИВ")
        self.assertEqual(found["results"][0]["video"], self.parts[1])
        self.assertFalse(self.api.call("search_chats", ["x", "везде"])["ok"])
        summary = self.api.broadcast_summary(self.parts[0])
        # обе части эфира: у первой чата нет, всплеск — во второй
        self.assertEqual([part["path"] for part in summary["parts"]], self.parts)
        self.assertEqual([part["chat"] for part in summary["parts"]], [False, True])
        self.assertEqual([part["messages"] for part in summary["parts"]], [0, 51])
        self.assertEqual(summary["parts"][0]["gaps"], [{"at": 5.0, "length": 2.0, "kept": True}])
        self.assertEqual([(moment["part"], moment["at"]) for moment in summary["moments"]], [(1, 60)])
        self.assertEqual(summary["top_chatters"][0], ["fan", 30])


if __name__ == "__main__":
    unittest.main()
