# -*- coding: utf-8 -*-
import datetime
import os
import unittest

from streamvault.live.recorder import quality_with_fallback, streamlink_output_arg
from streamvault.platforms import (channel_label, normalize_channels, parse_channel,
                                   stream_url)
from streamvault.util import MAX_PATH_LEN, build_recording_path


class ParseChannelTest(unittest.TestCase):

    def test_twitch_link_becomes_login(self):
        # иначе Helix получал ссылку вместо логина и отвечал 400 вечно
        self.assertEqual(parse_channel("https://www.twitch.tv/LegendaAFK/"),
                         ("twitch", "legendaafk"))

    def test_prefix_and_case(self):
        self.assertEqual(parse_channel("kick:XQC"), ("kick", "xqc"))
        self.assertEqual(parse_channel("legendaafk"), ("twitch", "legendaafk"))

    def test_uppercase_scheme(self):
        self.assertEqual(parse_channel("HTTPS://twitch.tv/x")[0], "twitch")

    def test_kick_clip_stays_link(self):
        link = "https://kick.com/xqc?clip=abc"
        self.assertEqual(parse_channel(link), ("kick", link))

    def test_youtube_handle(self):
        self.assertEqual(parse_channel("https://www.youtube.com/@SomeHandle/live"),
                         ("youtube", "SomeHandle"))
        self.assertEqual(stream_url("youtube", "SomeHandle"),
                         "https://www.youtube.com/@SomeHandle/live")

    def test_rejects(self):
        self.assertIsNone(parse_channel("foo:bar")[0])
        self.assertIsNone(parse_channel("https://example.com/x")[0])
        self.assertIsNone(parse_channel("kick:")[0])
        self.assertIsNone(parse_channel("  ")[0])


class LabelTest(unittest.TestCase):

    def test_reserved_device_name(self):
        self.assertEqual(channel_label("nul"), "_nul")

    def test_youtube_watch_link(self):
        self.assertEqual(channel_label("https://www.youtube.com/watch?v=abc123"), "abc123")

    def test_meaningless_tail(self):
        self.assertEqual(channel_label("https://www.youtube.com/@Handle/live"), "Handle")


class NormalizeChannelsTest(unittest.TestCase):

    def test_duplicates_strings_and_objects(self):
        with self.assertLogs(level="INFO"):
            result = normalize_channels([
                "legendaafk", "LegendaAFK", "https://twitch.tv/legendaafk",
                {"platform": "kick", "channel": "XQC", "enabled": False, "quality": "720p"},
                "kick:a, kick:b",
                "bad:x",
            ])
        # автозапись — только явно: у канала строкой без "enabled" она выключена
        self.assertEqual(result, [
            {"platform": "twitch", "channel": "legendaafk", "enabled": False},
            {"platform": "kick", "channel": "xqc", "enabled": False, "quality": "720p"},
            {"platform": "kick", "channel": "a", "enabled": False},
            {"platform": "kick", "channel": "b", "enabled": False},
        ])

    def test_console_channels_record(self):
        # каналы консольной записи (-u) и старого рекордера пишутся — для этого их и указали
        self.assertEqual(normalize_channels(["kick:a", {"platform": "kick", "channel": "b", "enabled": False}],
                                            enabled=True),
                         [{"platform": "kick", "channel": "a", "enabled": True},
                          {"platform": "kick", "channel": "b", "enabled": False}])

    def test_single_string(self):
        # строка вместо списка не должна разбираться посимвольно
        self.assertEqual(normalize_channels("kick:xqc"),
                         [{"platform": "kick", "channel": "xqc", "enabled": False}])

    def test_profile_survives_and_is_cleaned(self):
        result = normalize_channels([
            {"platform": "youtube", "channel": "xQcOW", "profile": {"name": "xQc", "avatar": "https://yt/a.jpg"}},
            {"platform": "kick", "channel": "b", "profile": {"name": 5, "avatar": "http://x/a.png"}},
            {"platform": "kick", "channel": "c", "profile": "мусор"},
        ])
        self.assertEqual(result[0]["profile"], {"name": "xQc", "avatar": "https://yt/a.jpg"})
        self.assertEqual(result[1]["profile"], {"name": "5", "avatar": ""})   # не https — без аватарки
        self.assertNotIn("profile", result[2])


class FileNamesTest(unittest.TestCase):

    def test_quality_falls_back_to_best(self):
        # было: "160p" не находило поток "160p30", а у канала без перекодирования
        # не находилось ничего — и эфир не писался вовсе
        self.assertTrue(quality_with_fallback("160p").startswith("160p,160p60,160p30,"))
        self.assertTrue(quality_with_fallback("720p60").startswith("720p60,720p,720p30,"))
        # нет такого — ближайшее по высоте, а не лучшее: у YouTube нет 160p, у VK Video Live — ниже 240p
        options = quality_with_fallback("160p30").split(",")
        self.assertLess(options.index("144p"), options.index("240p60"))
        self.assertLess(options.index("240p60"), options.index("1080p60"))
        self.assertEqual(options[-1], "best")
        self.assertEqual(len(options), len(set(options)))
        self.assertEqual(quality_with_fallback("audio_only"), "audio_only,best")
        self.assertEqual(quality_with_fallback("best"), "best")
        self.assertEqual(quality_with_fallback("480p,worst"), "480p,worst")
        self.assertEqual(quality_with_fallback(None), "best")

    def test_braces_are_escaped_for_streamlink(self):
        self.assertEqual(streamlink_output_arg(r"D:\x\смайл :} и {.ts"),
                         r"D:\x\смайл :}} и {{.ts")

    def test_long_title_fits_path_limit(self):
        directory = os.path.join("D:" + os.sep, "Streams", "recorded", "twitch", "channel")
        path = build_recording_path(directory, "channel", "очень длинный заголовок " * 30,
                                    datetime.datetime(2026, 10, 25, 12, 0, 0), 2)
        self.assertLessEqual(len(path), MAX_PATH_LEN)
        self.assertTrue(path.endswith(" - part02.ts"))

    def test_forbidden_characters_removed(self):
        path = build_recording_path("d", "c", 'a<b>c:"d"/e\\f|g?h*',
                                    datetime.datetime(2026, 1, 1), 1)
        self.assertNotRegex(os.path.basename(path), r'[<>:"/\\|?*]')


if __name__ == "__main__":
    unittest.main()
