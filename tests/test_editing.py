# -*- coding: utf-8 -*-
"""Правка записей: клип отрезка с его чатом и видео чата поверх записи."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from streamvault import editing, util
from streamvault.events import EventBus
from streamvault.library import broadcast_parts, edit_kind
from tests.helpers import EventLog, wait_for

FFMPEG = shutil.which("ffmpeg")


def make_video(path, seconds, size="320x180", audio=True):
    command = [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i",
               "testsrc=duration=%d:size=%s:rate=25" % (seconds, size)]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=duration=%d" % seconds, "-c:a", "aac", "-shortest"]
    # ключевой кадр каждую секунду: клип режется по ним
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "25", path]
    subprocess.run(command, check=True)


def duration(path):
    return editing.probe(FFMPEG, path)[0]


CHAT = {"video": {"start": 10, "length": 30},
        "comments": [{"content_offset_seconds": 10 + offset, "commenter": {"display_name": "Зритель"},
                      "message": {"body": "сообщение %d" % offset}} for offset in (1, 5, 6, 9, 20)],
        "embeddedData": {"thirdParty": [{"name": "KEKW", "data": "AAAA"}]}}


class NamesTest(unittest.TestCase):

    def test_marks_are_recognized(self):
        self.assertEqual(edit_kind("chan - 2026-10-25 12h00m00s - эфир [clip 00h01m02s-00h02m00s]"), "clip")
        self.assertEqual(edit_kind("chan - 2026-10-25 12h00m00s - эфир [clip 00h01m02s-00h02m00s] (2)"), "clip")
        self.assertEqual(edit_kind("chan - 2026-10-25 12h00m00s - эфир [chat]"), "chat")
        self.assertIsNone(edit_kind("chan - 2026-10-24 - клип [c1]"))

    def test_paths(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        source = os.path.join(folder, "chan - 2026-10-25 12h00m00s - эфир.ts")
        self.assertEqual(os.path.basename(editing.clip_path(source, 62.4, 3725)),
                         "chan - 2026-10-25 12h00m00s - эфир [clip 00h01m02s-01h02m05s].mp4")
        open(editing.overlay_path(source), "wb").close()
        self.assertTrue(editing.overlay_path(source).endswith(" [chat] (2).mp4"))

    def test_edits_are_not_parts_of_the_broadcast(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        names = ["chan - 2026-10-25 12h00m00s - эфир.mp4",
                 "chan - 2026-10-25 12h00m00s - эфир [clip 00h00m01s-00h00m05s].mp4",
                 "chan - 2026-10-25 12h00m00s - эфир [chat].mp4"]
        for name in names:
            open(os.path.join(folder, name), "wb").close()
        first = os.path.join(folder, names[0])
        self.assertEqual(broadcast_parts(first), [(1, first)])
        clip = os.path.join(folder, names[1])
        self.assertEqual(broadcast_parts(clip), [(1, clip)])


    def test_clip_needs_a_second(self):
        editor = editing.Editor(lambda: FFMPEG)
        for start, end in ((5, 5.5), (9, 4), (-1, 3)):
            with self.assertRaises(ValueError):
                editor.cut("запись.mp4", start, end)
        self.assertEqual(editor.snapshot(), [])


class ClipChatTest(unittest.TestCase):

    def test_messages_of_the_part_from_its_start(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        source, target = os.path.join(folder, "a.chat.json"), os.path.join(folder, "b.chat.json")
        with open(source, "w", encoding="utf-8") as output:
            json.dump(CHAT, output, ensure_ascii=False)
        self.assertEqual(editing.clip_chat(source, target, 4, 9), 3)
        with open(target, encoding="utf-8") as result:
            data = json.load(result)
        # время записи — от video.start; в клипе — от его начала
        self.assertEqual([c["content_offset_seconds"] for c in data["comments"]], [1, 2, 5])
        self.assertEqual((data["video"]["start"], data["video"]["length"]), (0, 5))
        self.assertEqual(data["embeddedData"], CHAT["embeddedData"])
        self.assertEqual(editing.clip_chat(source, os.path.join(folder, "c.chat.json"), 25, 28), 0)
        self.assertFalse(os.path.exists(os.path.join(folder, "c.chat.json")))


@unittest.skipUnless(FFMPEG, "нужен ffmpeg")
class EditorTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.samples = tempfile.mkdtemp()
        cls.sample = os.path.join(cls.samples, "sample.mp4")
        make_video(cls.sample, 12)
        cls.chat_video = os.path.join(cls.samples, "chat.mp4")
        make_video(cls.chat_video, 6, size="80x360", audio=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.samples, ignore_errors=True)

    def setUp(self):
        self.folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.source = os.path.join(self.folder, "chan - 2026-10-25 12h00m00s - эфир.mp4")
        shutil.copy(self.sample, self.source)
        self.bus = EventBus()
        self.events = EventLog(self.bus)
        self.editor = editing.Editor(lambda: FFMPEG, self.bus)
        self.editor.start()
        self.addCleanup(self.editor.stop)
        logs = self.assertLogs(level="INFO")
        logs.__enter__()
        self.addCleanup(logs.__exit__, None, None, None)

    def job(self, job_id):
        return next(job for job in self.editor.snapshot() if job["id"] == job_id)

    def finished(self, job_id, timeout=60):
        self.assertTrue(wait_for(lambda: self.job(job_id)["state"] in (editing.DONE, editing.FAILED,
                                                                       editing.CANCELLED), timeout))
        return self.job(job_id)

    def test_clip_with_chat(self):
        chat_json = os.path.splitext(self.source)[0] + ".chat.json"
        with open(chat_json, "w", encoding="utf-8") as output:
            json.dump(CHAT, output, ensure_ascii=False)
        job = self.finished(self.editor.cut(self.source, 4, 9, with_chat=True))
        self.assertEqual(job["state"], editing.DONE, job["error"])
        self.assertTrue(job["target"].endswith(" [clip 00h00m04s-00h00m09s].mp4"))
        self.assertAlmostEqual(duration(job["target"]), 5, delta=1.1)
        self.assertEqual(job["chat_messages"], 3)
        # ключевой кадр раз в 10 секунд: клип начнётся с нуля — и чат вместе с ним
        sparse = os.path.join(self.folder, "chan - 2026-10-25 13h00m00s - редкие кадры.mp4")
        subprocess.run([FFMPEG, "-v", "error", "-y", "-i", self.sample, "-c:v", "libx264", "-preset",
                        "ultrafast", "-g", "250", "-c:a", "copy", sparse], check=True)
        shutil.copy(chat_json, os.path.splitext(sparse)[0] + ".chat.json")
        job = self.finished(self.editor.cut(sparse, 4, 9, with_chat=True))
        self.assertAlmostEqual(duration(job["target"]), 9, delta=1.1)
        with open(os.path.splitext(job["target"])[0] + ".chat.json", encoding="utf-8") as result:
            offsets = [c["content_offset_seconds"] for c in json.load(result)["comments"]]
        self.assertAlmostEqual(offsets[0], 1, delta=0.3)       # сообщение на 1-й секунде записи
        self.assertTrue(os.path.isfile(os.path.splitext(job["target"])[0] + ".chat.json"))
        self.assertFalse(os.path.exists(job["partial"]))
        self.assertIn(editing.DONE, [event["job"]["state"] for event in self.events.of("edit")])

    def test_overlay_keeps_recording_length(self):
        with self.assertRaises(ValueError):
            self.editor.overlay(self.source)          # видео чата ещё нет
        shutil.copy(self.chat_video, os.path.splitext(self.source)[0] + ".chat.mp4")
        job = self.finished(self.editor.overlay(self.source, "left"), timeout=120)
        self.assertEqual(job["state"], editing.DONE, job["error"])
        self.assertTrue(job["target"].endswith(" [chat].mp4"))
        self.assertAlmostEqual(duration(job["target"]), 12, delta=0.5)
        self.assertEqual(editing.probe(FFMPEG, job["target"])[1], (320, 180))
        with self.assertRaises(ValueError):
            self.editor.overlay(self.source, "сверху")

    def test_cancel_queued_and_failure(self):
        broken = os.path.join(self.folder, "broken.mp4")
        with open(broken, "wb") as output:
            output.write(b"not a video")
        failed = self.finished(self.editor.cut(broken, 0, 5))
        self.assertEqual(failed["state"], editing.FAILED)
        self.assertTrue(failed["error"])
        self.assertFalse(os.path.exists(failed["partial"]))
        # пока занят — следующая стоит в очереди, её можно отменить
        shutil.copy(self.chat_video, os.path.splitext(self.source)[0] + ".chat.mp4")
        running = self.editor.overlay(self.source)
        queued = self.editor.cut(self.source, 1, 3)
        self.editor.cancel(queued)
        self.editor.cancel(running)
        self.assertEqual(self.finished(running)["state"], editing.CANCELLED)
        self.assertEqual(self.job(queued)["state"], editing.CANCELLED)
        leftovers = [name for name in os.listdir(self.folder) if name.endswith(util.PARTIAL_SUFFIX)]
        self.assertTrue(wait_for(lambda: not [name for name in os.listdir(self.folder)
                                              if name.endswith(util.PARTIAL_SUFFIX)]), leftovers)

    def test_no_ffmpeg(self):
        editor = editing.Editor(lambda: None)
        editor.start()
        self.addCleanup(editor.stop)
        job_id = editor.cut(self.source, 0, 5)
        self.assertTrue(wait_for(lambda: editor.snapshot()[0]["state"] == editing.FAILED))
        self.assertIn("ffmpeg", editor.snapshot()[0]["error"])
        self.assertEqual(editor.snapshot()[0]["id"], job_id)


if __name__ == "__main__":
    unittest.main()
