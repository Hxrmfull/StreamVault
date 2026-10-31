# -*- coding: utf-8 -*-
import datetime
import os
import unittest

from streamvault.archive import ytdlp
from streamvault.archive.downloads import build_download_base
from streamvault.util import MAX_PATH_LEN

# Урезанные ответы yt-dlp 2026.10.21 на настоящие ссылки.
TWITCH_VOD = {
    "_type": "video", "extractor_key": "TwitchVod", "id": "v2909822254",
    "title": "GTA V", "uploader": "xQc", "uploader_id": "xqc", "channel": None,
    "upload_date": "20261024", "timestamp": 1792875046, "duration": 42700,
    "thumbnail": "https://static-cdn.jtvnw.net/thumb0-0x0.jpg", "live_status": "was_live",
    "webpage_url": "https://www.twitch.tv/videos/2909822254",
    "formats": [
        {"format_id": "sb0", "height": 124, "ext": "mhtml", "vcodec": "none", "acodec": "none"},
        {"format_id": "Audio_Only", "ext": "mp4", "vcodec": "none", "acodec": "mp4a.40.2"},
        {"format_id": "160p", "height": 160, "fps": 30.0, "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "720p60", "height": 720, "fps": 60.0, "vcodec": "avc1", "acodec": "mp4a"},
        {"format_id": "1080p60", "height": 1080, "fps": 60.0, "vcodec": "avc1", "acodec": "mp4a"},
    ],
}

TWITCH_CLIP = {
    "_type": "video", "extractor_key": "TwitchClips", "id": "1492893388",
    "title": "МУХА КЛОУН", "uploader": "rev1lxx", "uploader_id": "474174597",
    "channel": "LegendaAFK", "timestamp": 1656873059, "duration": 4,
    "formats": [{"format_id": "360", "height": 360, "fps": 0.0, "ext": "mp4"},
                {"format_id": "1080", "height": 1080, "fps": 0.0, "ext": "mp4"}],
}

KICK_LISTING = {
    "_type": "playlist", "extractor_key": "KickVideos", "id": "xqc",
    "title": "xQc - Videos sorted by Date", "webpage_url": "https://kick.com/xqc/videos",
    "entries": [{
        "_type": "url_transparent", "ie_key": "KickVOD", "id": "01a16fae",
        "title": "NOPIXEL", "duration": 42709, "timestamp": 1792875047,
        "uploader": "xQc", "uploader_id": "xqc", "channel": "xQc",
        "url": "https://kick.com/xqc/videos/01a16fae#__youtubedl_smuggle=%7B%7D",
    }],
}

YOUTUBE_ENTRY = {
    "_type": "url", "ie_key": "Youtube", "id": "Z1sqWFs86uU",
    "title": "Building the Rarest PC", "url": "https://www.youtube.com/watch?v=Z1sqWFs86uU",
    "duration": 13091, "timestamp": None, "live_status": "was_live",
    "thumbnails": [{"url": "https://i.ytimg.com/small.jpg"},
                   {"url": "https://i.ytimg.com/big.jpg"}],
}


class DescribeTest(unittest.TestCase):

    def test_twitch_vod_qualities(self):
        info = ytdlp.describe(TWITCH_VOD)
        self.assertEqual((info["type"], info["platform"], info["kind"], info["channel"]),
                         ("video", "twitch", "vod", "xQc"))
        self.assertEqual([option["label"] for option in info["qualities"]],
                         ["лучшее", "1080p60", "720p60", "160p", "только звук"])
        self.assertEqual(info["qualities"][1]["quality"], "1080")

    def test_clip_channel_is_broadcaster_not_clipper(self):
        info = ytdlp.describe(TWITCH_CLIP)
        self.assertEqual((info["kind"], info["channel"]), ("clip", "LegendaAFK"))
        self.assertEqual([option["label"] for option in info["qualities"]],
                         ["лучшее", "1080p", "360p"])

    def test_listing_cleans_urls(self):
        listing = ytdlp.describe(KICK_LISTING)
        self.assertEqual(listing["type"], "playlist")
        entry = listing["entries"][0]
        self.assertEqual(entry["url"], "https://kick.com/xqc/videos/01a16fae")
        self.assertEqual((entry["platform"], entry["kind"], entry["channel"]),
                         ("kick", "vod", "xQc"))

    def test_youtube_entry_takes_best_thumbnail(self):
        entry = ytdlp.describe_entry(YOUTUBE_ENTRY)
        self.assertEqual(entry["thumbnail"], "https://i.ytimg.com/big.jpg")
        self.assertEqual((entry["platform"], entry["kind"]), ("youtube", "video"))
        self.assertFalse(entry["is_live"])

    def test_date_from_upload_date(self):
        entry = ytdlp.describe_entry({"id": "x", "upload_date": "20260101"})
        moment = datetime.datetime.fromtimestamp(entry["timestamp"], datetime.timezone.utc)
        self.assertEqual(moment.date(), datetime.date(2026, 1, 1))

    def test_twitch_vod_in_listing(self):
        # в списке записей Twitch дата только в адресе превью, а само превью — 320x180
        thumb = ("https://static-cdn.jtvnw.net/cf_vods/dgeft87wbj63p/"
                 "d18d4bda975d8c77a0cb_recrent_317182973524_1793019575//thumb/thumb0-320x180.jpg")
        entry = ytdlp.describe_entry({"id": "v1", "ie_key": "TwitchVod", "thumbnail": thumb})
        self.assertEqual(entry["timestamp"], 1793019575)
        self.assertTrue(entry["thumbnail"].endswith("/thumb0-640x360.jpg"))
        # у чужого превью ничего не трогаем
        other = ytdlp.describe_entry({"id": "x", "thumbnail": "https://example.com/a-320x180.jpg"})
        self.assertEqual((other["thumbnail"], other["timestamp"]), ("https://example.com/a-320x180.jpg", None))


class ListingTest(unittest.TestCase):

    def test_urls(self):
        self.assertEqual(ytdlp.listing_url("twitch", "xqc", "clips", "7d"),
                         "https://www.twitch.tv/xqc/clips?filter=clips&range=7d")
        self.assertEqual(ytdlp.listing_url("youtube", "@Handle"),
                         "https://www.youtube.com/@Handle/streams")
        self.assertEqual(ytdlp.listing_url("vkvideolive", "blog"), "https://live.vkvideo.ru/blog/records")

    def test_rejects(self):
        for args in [("vkvideolive", "x", "shorts"), ("other", "x"), ("twitch", "xqc", "shorts"),
                     ("twitch", "https://twitch.tv/xqc"), ("twitch", "xqc", "clips", "1y")]:
            with self.assertRaises(ValueError, msg=str(args)):
                ytdlp.listing_url(*args)


class CommandTest(unittest.TestCase):

    def test_format_selection(self):
        self.assertEqual(ytdlp.format_args("best"), ["-f", "bv*+ba/b"])
        self.assertEqual(ytdlp.format_args("720p"),
                         ["-f", "bv*[height<=720]+ba/b[height<=720]/wv*+ba/w"])
        self.assertEqual(ytdlp.format_args("audio")[-3:], ["-x", "--audio-format", "m4a"])
        # id формата платформы передаётся как есть
        self.assertEqual(ytdlp.format_args("1080p60"), ["-f", "1080p60"])

    def test_download_command(self):
        command = ytdlp.download_command(
            "https://twitch.tv/videos/1", r"D:\x\100% {title}", "720", (60.0, 90.0),
            4, r"C:\ffmpeg\ffmpeg.exe", ["--cookies-from-browser", "firefox"])
        self.assertEqual(command[command.index("-o") + 1], r"D:\x\100%% {title}.%(ext)s")
        self.assertIn("*60.0-90.0", command)
        self.assertEqual(command[command.index("-N") + 1], "4")
        self.assertEqual(command[command.index("--ffmpeg-location") + 1],
                         r"C:\ffmpeg\ffmpeg.exe")
        # свои аргументы — в конце, ссылка — после "--": её начало с "-" не примут за ключ
        self.assertEqual(command[-4:], ["--cookies-from-browser", "firefox",
                                        "--", "https://twitch.tv/videos/1"])

    def test_parse_lines(self):
        kind, data = ytdlp.parse_progress('SVP {"status": "downloading", '
                                          '"downloaded_bytes": 50, "total_bytes": 200}')
        self.assertEqual(kind, "progress")
        self.assertEqual(ytdlp.progress_of(data)["percent"], 25.0)
        self.assertEqual(ytdlp.parse_progress("SVFILE D:\\a b.mp4"), ("file", "D:\\a b.mp4"))
        self.assertEqual(ytdlp.parse_progress("ERROR: [twitch] нет")[0], "error")
        self.assertEqual(ytdlp.parse_progress("[download] что-то")[0], "other")
        fragments = ytdlp.progress_of({"fragment_index": 3, "fragment_count": 12})
        self.assertEqual(fragments["percent"], 25.0)

    def test_sections(self):
        self.assertEqual(ytdlp.parse_section("1:00:00-1:30:00"), (3600.0, 5400.0))
        self.assertEqual(ytdlp.parse_section("62:03-3800"), (3723.0, 3800.0))
        for bad in ("1:00", "5-3", "a-b"):
            with self.assertRaises(ValueError):
                ytdlp.parse_section(bad)


class FileNameTest(unittest.TestCase):

    def test_name_and_length(self):
        directory = os.path.join("D:" + os.sep, "Streams", "downloads", "twitch", "xqc")
        base = build_download_base(directory, "xqc", "заголовок: " * 40,
                                   datetime.datetime(2026, 10, 24), "v1", " 720")
        name = os.path.basename(base)
        self.assertTrue(name.startswith("xqc - 2026-10-24 - заголовок"))
        self.assertTrue(name.endswith(" [v1] 720"))
        self.assertNotIn(":", name)
        self.assertLessEqual(len(base + ".f000.mp4.part-Frag0000.part"), MAX_PATH_LEN)


if __name__ == "__main__":
    unittest.main()
