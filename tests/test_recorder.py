# -*- coding: utf-8 -*-
import os
import shutil
import tempfile
import time
import unittest

from streamvault.events import EventBus
from streamvault.live.probes import Status
from streamvault.live.recorder import ChannelRecorder
from tests.helpers import (OFFLINE, ONLINE, EventLog, FakeProcessing, FakeStreamlink,
                           ScriptedProbe, wait_for)


class InstantRecorder(ChannelRecorder):
    """Паузы не ждутся, а запоминаются: логику можно проверить без часов."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sleeps = []
        # то, что поток делает в run() перед первой проверкой
        self.prepare_directories()

    def _sleep(self, seconds):
        self.sleeps.append(seconds)


class QuickCrashRecorder(ChannelRecorder):
    CRASH_RETRY = 0.1


class RecorderTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.processing = FakeProcessing()
        self.bus = EventBus()
        self.events = EventLog(self.bus)
        self.threads = []

    def tearDown(self):
        for recorder in self.threads:
            recorder.stop(grace=0)
            recorder.join(10)
        shutil.rmtree(self.root, ignore_errors=True)

    def make(self, probe, cls=InstantRecorder, fallback=None, refresh=15):
        recorder = cls("twitch", "c", probe, "best", refresh, self.root, self.processing,
                       fallback_probe=fallback, bus=self.bus)
        if cls is not InstantRecorder:
            self.threads.append(recorder)
        return recorder


class StatusHandlingTest(RecorderTestCase):

    def test_offline_is_announced_once(self):
        recorder = self.make(ScriptedProbe(OFFLINE))
        with self.assertLogs(level="DEBUG") as logs:
            for _ in range(3):
                recorder._iteration()
        announced = [line for line in logs.output if "жду выхода" in line]
        self.assertEqual(len(announced), 1)
        self.assertEqual(recorder.sleeps, [15, 15, 15])
        self.assertEqual(recorder.snapshot()["state"], "offline")

    def test_not_found_keeps_reason(self):
        probe = ScriptedProbe((Status.NOT_FOUND, {"reason": "такого канала на Twitch нет"}))
        recorder = self.make(probe)
        with self.assertLogs(level="ERROR"):
            recorder._iteration()
        state = recorder.snapshot()
        self.assertEqual(state["state"], "not_found")
        self.assertEqual(state["message"], "такого канала на Twitch нет")
        self.assertEqual(recorder.sleeps, [600])

    def test_unauthorized_backs_off_then_falls_back(self):
        # было: 2,4 млн запросов за 2 секунды при упорном 401
        probe = ScriptedProbe((Status.UNAUTHORIZED, None))
        fallback = ScriptedProbe(OFFLINE, name="streamlink")
        recorder = self.make(probe, fallback=fallback)
        with self.assertLogs(level="INFO"):
            for _ in range(5):
                recorder._iteration()
        self.assertEqual(recorder.sleeps[:3], [0, 5, 10])
        self.assertEqual(probe.refreshes, 3)
        self.assertTrue(probe.disabled)
        self.assertEqual(probe.calls, 4)
        state = recorder.snapshot()
        self.assertEqual((state["probe"], state["refresh"], state["state"]),
                         ("streamlink", 30, "offline"))

    def test_network_errors_back_off(self):
        recorder = self.make(ScriptedProbe((Status.NETWORK_ERROR, None)))
        with self.assertLogs(level="WARNING"):
            for _ in range(5):
                recorder._iteration()
        self.assertEqual(recorder.sleeps, [15, 30, 45, 60, 60])
        self.assertEqual(recorder.snapshot()["message"], "нет связи")

    def test_api_error_shows_plugin_reason(self):
        recorder = self.make(ScriptedProbe((Status.API_ERROR, {"reason": "гео-блок"})))
        with self.assertLogs(level="WARNING"):
            for _ in range(6):
                recorder._iteration()
        self.assertEqual(recorder.sleeps, [30, 60, 120, 240, 300, 300])
        self.assertEqual(recorder.snapshot()["message"], "гео-блок")


class RecordingTest(RecorderTestCase):

    def test_records_and_hands_over_file(self):
        title = "смайл :} и {"
        probe = ScriptedProbe((Status.ONLINE, {"title": title, "game_name": "Just Chatting",
                                               "viewer_count": 42}), OFFLINE)
        recorder = self.make(probe)
        with FakeStreamlink("once"), self.assertLogs(level="INFO"):
            recorder._iteration()

        self.assertEqual(len(self.processing.submitted), 1)
        recorded, processed, trusted = self.processing.submitted[0]
        self.assertTrue(trusted)
        self.assertEqual(os.path.getsize(recorded), 188 * 50)
        # фигурные скобки дошли до имени файла буквально
        self.assertIn("смайл } и {", os.path.basename(recorded))
        self.assertEqual(os.path.basename(processed), os.path.basename(recorded))

        states = self.events.states(recorder.id)
        self.assertIn("recording", states)
        self.assertEqual(states[-1], "offline")
        recording_events = [event["channel"] for event in self.events.of("channel")
                            if event["channel"]["recording"]]
        self.assertEqual(recording_events[0]["stream"]["game"], "Just Chatting")
        self.assertEqual(recording_events[0]["stream"]["viewers"], 42)
        self.assertIsNone(recorder.snapshot()["recording"])

    def test_unrecordable_stream_backs_off_with_reason(self):
        recorder = self.make(ScriptedProbe(ONLINE))
        with FakeStreamlink("empty"), self.assertLogs(level="WARNING"):
            recorder._iteration()
            recorder._iteration()
        self.assertEqual(recorder.sleeps, [15, 15, 30, 15, 15, 60])
        self.assertEqual(self.processing.submitted, [])
        state = recorder.snapshot()
        self.assertEqual(state["state"], "unavailable")
        self.assertEqual(state["message"], "This stream is subscriber-only")
        self.assertEqual(os.listdir(recorder.recorded_path), [])

    def test_stop_kills_running_streamlink(self):
        recorder = self.make(ScriptedProbe(ONLINE), cls=ChannelRecorder)
        with FakeStreamlink("forever"), self.assertLogs(level="INFO"):
            recorder.start()
            self.assertTrue(wait_for(lambda: (recorder.snapshot()["recording"] or {})
                                     .get("bytes", 0) > 0))
            began = time.monotonic()
            recorder.stop(grace=0)
            recorder.join(10)
            took = time.monotonic() - began

        self.assertFalse(recorder.is_alive())
        self.assertLess(took, 5)
        self.assertEqual(len(self.processing.submitted), 1)
        recorded = self.processing.submitted[0][0]
        size = os.path.getsize(recorded)
        time.sleep(1)
        # файл больше не растёт: streamlink не остался сиротой
        self.assertEqual(os.path.getsize(recorded), size)
        self.assertEqual(recorder.snapshot()["state"], "stopped")


class ManualRecordingTest(RecorderTestCase):
    """Наблюдение отдельно от записи: автозапись, «Начать» и «Остановить запись»."""

    def make_thread(self, probe, **kwargs):
        recorder = ChannelRecorder("twitch", "Chan", probe, "best", 15, self.root, self.processing,
                                   bus=self.bus, **kwargs)
        self.threads.append(recorder)
        return recorder

    def recording_bytes(self, recorder):
        return (recorder.snapshot()["recording"] or {}).get("bytes", 0)

    def test_live_without_autorecord_is_only_shown(self):
        recorder = InstantRecorder("twitch", "Chan", ScriptedProbe(ONLINE), "best", 15, self.root,
                                   self.processing, bus=self.bus, autorecord=False)
        with self.assertLogs(level="INFO") as logs:
            recorder._iteration()
            recorder._iteration()
        state = recorder.snapshot()
        self.assertEqual((state["state"], state["enabled"]), ("live", False))
        self.assertEqual(state["stream"]["title"], "тестовый стрим")
        # кадр эфира Twitch — по имени канала, без ключей API
        self.assertEqual(state["stream"]["thumbnail"],
                         "https://static-cdn.jtvnw.net/previews-ttv/live_user_chan-640x360.jpg")
        self.assertEqual(self.processing.submitted, [])
        self.assertEqual(len([line for line in logs.output if "не записывается" in line]), 1)

    def test_record_now_then_stop_keeps_watching(self):
        recorder = self.make_thread(ScriptedProbe(ONLINE), autorecord=False)
        with FakeStreamlink("forever"), self.assertLogs(level="INFO"):
            recorder.start()
            self.assertTrue(wait_for(lambda: recorder.snapshot()["state"] == "live"))
            recorder.record_now()
            self.assertTrue(wait_for(lambda: self.recording_bytes(recorder) > 0))
            recorder.stop_recording()
            self.assertTrue(wait_for(lambda: len(self.processing.submitted) == 1))
            self.assertTrue(wait_for(lambda: recorder.snapshot()["state"] == "live"))
            # эфир идёт, но остановленная запись сама не начинается
            recorder.check_now()
            time.sleep(1)
            self.assertEqual(recorder.snapshot()["state"], "live")
            self.assertEqual(len(self.processing.submitted), 1)
            self.assertTrue(recorder.is_alive())

    def test_autorecord_off_does_not_cut_recording(self):
        recorder = self.make_thread(ScriptedProbe(ONLINE))
        with FakeStreamlink("forever"), self.assertLogs(level="INFO"):
            recorder.start()
            self.assertTrue(wait_for(lambda: self.recording_bytes(recorder) > 0))
            recorder.set_autorecord(False)
            time.sleep(1)
            state = recorder.snapshot()
            self.assertEqual((state["state"], state["enabled"]), ("recording", False))
            self.assertEqual(self.processing.submitted, [])
            # запись остановили — следующий эфир сам писаться не будет
            recorder.stop_recording()
            self.assertTrue(wait_for(lambda: recorder.snapshot()["state"] == "live"))

    def test_stream_end_resets_buttons(self):
        recorder = InstantRecorder("twitch", "c", ScriptedProbe(ONLINE, OFFLINE, ONLINE), "best", 15,
                                   self.root, self.processing, bus=self.bus, autorecord=False)
        recorder.stop_recording()            # запись этого эфира остановили
        with self.assertLogs(level="INFO"):
            recorder._iteration()            # эфир идёт — не пишется
            recorder._iteration()            # эфир кончился
            recorder._iteration()            # новый эфир: автозапись выключена — не пишется
        self.assertEqual(recorder.snapshot()["state"], "live")
        self.assertFalse(recorder._skip or recorder._manual)

    def test_once_ends_with_its_recording(self):
        exited = []
        recorder = self.make_thread(ScriptedProbe(ONLINE), autorecord=False, once=True,
                                    on_exit=exited.append)
        recorder.record_now()
        with FakeStreamlink("forever"), self.assertLogs(level="INFO"):
            recorder.start()
            self.assertTrue(wait_for(lambda: self.recording_bytes(recorder) > 0))
            self.assertTrue(recorder.snapshot()["temporary"])
            recorder.stop_recording()
            recorder.join(10)
        self.assertFalse(recorder.is_alive())
        self.assertEqual(exited, [recorder])
        self.assertEqual(len(self.processing.submitted), 1)


class ThreadTest(RecorderTestCase):

    def test_survives_crash(self):
        probe = ScriptedProbe(OSError("диск отключён"), OFFLINE)
        recorder = self.make(probe, cls=QuickCrashRecorder)
        with self.assertLogs(level="INFO") as logs:
            recorder.start()
            self.assertTrue(wait_for(lambda: recorder.snapshot()["state"] == "offline"))
        self.assertTrue(recorder.is_alive())
        self.assertIn("error", self.events.states(recorder.id))
        self.assertTrue(any("сбой, повтор" in line and "диск отключён" in line
                            for line in logs.output))

    def test_check_now_wakes_from_pause(self):
        probe = ScriptedProbe(OFFLINE)
        recorder = self.make(probe, cls=ChannelRecorder)
        with self.assertLogs(level="INFO"):
            recorder.start()
            self.assertTrue(wait_for(lambda: probe.calls == 1))
            time.sleep(0.2)
            recorder.check_now()
            self.assertTrue(wait_for(lambda: probe.calls == 2, timeout=3))

    def test_stop_as_paused(self):
        recorder = self.make(ScriptedProbe(OFFLINE), cls=ChannelRecorder)
        with self.assertLogs(level="INFO"):
            recorder.start()
            wait_for(lambda: recorder.snapshot()["state"] == "offline")
            recorder.stop(grace=0, final_state="paused")
            recorder.join(5)
        self.assertEqual(recorder.snapshot()["state"], "paused")

    def test_quality_change(self):
        recorder = self.make(ScriptedProbe(OFFLINE))
        recorder.set_quality("720p")
        self.assertEqual(recorder.snapshot()["quality"], "720p")
        self.assertEqual(self.events.of("channel")[-1]["channel"]["quality"], "720p")


if __name__ == "__main__":
    unittest.main()
