# -*- coding: utf-8 -*-
"""Реестр площадок: равенство возможностей, кадры эфира, профили. Сеть подменяется."""
import os
import re
import unittest
from types import SimpleNamespace
from unittest import mock

from streamvault import i18n, sites
from streamvault.platforms import PLATFORMS
from streamvault.sites import kick, vkvideolive, youtube
from streamvault.sites.common import Memo, SiteError
from tests.test_search import answer

README = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
REQUEST = "streamvault.sites.common.requests.request"


def fresh_memos():
    """Память ответов модулей — своя у каждого теста."""
    return mock.patch.multiple(kick, _info=Memo(kick.INFO_SECONDS), _live=Memo(kick.INFO_SECONDS),
                               _videos=Memo(kick.VIDEOS_SECONDS)), \
        mock.patch.multiple(vkvideolive, _streams=Memo(vkvideolive.PREVIEW_SECONDS))


class EqualityTest(unittest.TestCase):
    """Правило 1: возможность либо есть у площадки, либо записана причина, почему её нет."""

    def test_every_platform_has_a_module(self):
        self.assertEqual(set(sites.SITES), set(PLATFORMS))

    def test_every_capability_is_done_or_explained(self):
        for platform in sites.SITES:
            for capability, value in sites.capabilities(platform).items():
                with self.subTest(platform=platform, capability=capability):
                    self.assertTrue(value is True or (isinstance(value, str) and value.strip()),
                                    "у %s нет «%s», и причина не записана" % (platform, capability))

    def test_unsupported_names_known_capabilities_only(self):
        for platform in sites.SITES:
            for capability in sites.unsupported(platform):
                self.assertIn(capability, sites.CAPABILITIES, platform)
                # записанное «нет» у реализованной возможности — устаревшая запись
                self.assertFalse(sites.implements(sites.site(platform), capability),
                                 "%s: «%s» есть, а записано, что нет" % (platform, capability))

    def test_youtube_has_no_channel_clips(self):
        self.assertIn("Shorts", sites.capabilities("youtube")["clips"])

    def test_reasons_are_translated(self):
        for platform in sites.SITES:
            texts = list(sites.unsupported(platform).values()) + [sites.search_note(platform)]
            for text in filter(None, texts):
                self.assertIn(text, i18n.STRINGS, text)


class ReadmeTableTest(unittest.TestCase):
    """Таблица «Что умеет каждая площадка» в README совпадает с реестром."""

    def table(self):
        with open(README, encoding="utf-8") as source:
            text = source.read()
        section = text.split("## Что умеет каждая площадка", 1)[1].split("\n## ", 1)[0]
        rows = [line for line in section.splitlines() if line.startswith("|")]
        header = [cell.strip() for cell in rows[0].strip("|").split("|")]
        body = {}
        for line in rows[2:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            body[cells[0]] = dict(zip(header[1:], cells[1:]))
        return header[1:], body

    def test_rows_and_columns_match_registry(self):
        columns, body = self.table()
        self.assertEqual(columns, [sites.SITES[platform].NAME for platform in sites.SITES])
        self.assertEqual(list(body), list(sites.CAPABILITIES.values()))
        for platform, module in sites.SITES.items():
            for capability, title in sites.CAPABILITIES.items():
                cell = body[title][module.NAME]
                with self.subTest(platform=platform, capability=capability):
                    if sites.capabilities(platform)[capability] is True:
                        self.assertTrue(cell.startswith("да"), cell)
                    else:
                        self.assertTrue(re.match(r"нет\b", cell), cell)


class PreviewTest(unittest.TestCase):

    def setUp(self):
        for patcher in fresh_memos():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_twitch_and_youtube_need_no_network(self):
        with mock.patch(REQUEST) as request:
            self.assertIn("live_user_xqc-640x360", sites.live_preview("twitch", "xQc"))
            self.assertEqual(sites.live_preview("youtube", "c", {"id": "abcdefghijk"}),
                             "https://i.ytimg.com/vi/abcdefghijk/hqdefault_live.jpg")
            self.assertEqual(sites.live_preview("youtube", "c", {}), "")
        request.assert_not_called()

    def test_kick_frame_while_live_and_remembered(self):
        live = {"slug": "xqc", "livestream": {"thumbnail": {"url": "https://images.kick.com/t.webp"}}}
        with mock.patch(REQUEST, return_value=answer(200, live)) as request:
            self.assertEqual(sites.live_preview("kick", "xqc"), "https://images.kick.com/t.webp")
            self.assertEqual(sites.live_preview("kick", "XQC"), "https://images.kick.com/t.webp")
        self.assertEqual(request.call_count, 1)        # опрос частый — ответ помнится
        self.assertEqual(request.call_args.args[1], "https://kick.com/api/v2/channels/xqc")

    def test_kick_offline_and_failures(self):
        with mock.patch(REQUEST, return_value=answer(200, {"slug": "a", "livestream": None})):
            self.assertEqual(sites.live_preview("kick", "a"), "")
        # нет связи — пустая рамка, а не упавшая запись
        with mock.patch(REQUEST, return_value=answer(500)):
            self.assertEqual(sites.live_preview("kick", "b"), "")
        with mock.patch(REQUEST) as request:
            self.assertEqual(sites.live_preview("kick", "https://kick.com/a?clip=1"), "")
        request.assert_not_called()

    def test_kick_empty_frame_asked_from_stream_itself(self):
        # в сведениях о канале кадр бывает пустым, а у самого эфира он есть
        def respond(method, url, **kwargs):
            if url.endswith("/livestream"):
                return answer(200, {"data": {"thumbnail": {"src": "https://images.kick.com/720.webp"}}})
            return answer(200, {"slug": "sus", "livestream": {"is_live": True, "thumbnail": None}})

        with mock.patch(REQUEST, side_effect=respond) as request:
            self.assertEqual(sites.live_preview("kick", "sus"), "https://images.kick.com/720.webp")
        self.assertEqual(request.call_args.args[1], "https://kick.com/api/v2/channels/sus/livestream")
        # не в эфире — второй раз не спрашиваем
        with mock.patch(REQUEST, return_value=answer(200, {"slug": "off", "livestream": None})) as request:
            self.assertEqual(sites.live_preview("kick", "off"), "")
        self.assertEqual(request.call_count, 1)

    def test_vk_frame(self):
        stream = {"isOnline": True, "previewUrl": "https://images.live.vkvideo.ru/p.jpg"}
        with mock.patch(REQUEST, return_value=answer(200, stream)) as request:
            self.assertEqual(sites.live_preview("vkvideolive", "kolya"), stream["previewUrl"])
        self.assertEqual(request.call_args.args[1],
                         "https://api.live.vkvideo.ru/v1/blog/kolya/public_video_stream")
        with mock.patch(REQUEST, return_value=answer(200, {"isOnline": False, "previewUrl": "https://x/p"})):
            self.assertEqual(sites.live_preview("vkvideolive", "other"), "")

    def test_describe_stream_takes_frame_from_site(self):
        from streamvault.live.probes import describe_stream
        live = {"slug": "c", "livestream": {"thumbnail": {"url": "https://images.kick.com/c.webp"}}}
        with mock.patch(REQUEST, return_value=answer(200, live)):
            self.assertEqual(describe_stream({"title": "т"}, "kick", "c")["thumbnail"],
                             "https://images.kick.com/c.webp")


class ProfilesTest(unittest.TestCase):

    def setUp(self):
        for patcher in fresh_memos():
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_kick_and_vk(self):
        kick_data = {"slug": "xqc", "user": {"username": "xQc", "profile_pic": "https://k/x.png"}}
        with mock.patch(REQUEST, return_value=answer(200, kick_data)):
            self.assertEqual(kick.profile("xqc"), {"name": "xQc", "avatar": "https://k/x.png"})
        vk_data = {"blogUrl": "kolya", "owner": {"displayName": "Коля", "avatarUrl": "https://v/a.png",
                                                 "hasAvatar": False}}
        with mock.patch(REQUEST, return_value=answer(200, vk_data)) as request:
            self.assertEqual(vkvideolive.profile("kolya"), {"name": "Коля", "avatar": ""})
        self.assertEqual(request.call_args.args[1], "https://api.live.vkvideo.ru/v1/blog/kolya")
        with mock.patch(REQUEST, return_value=answer(404)):
            self.assertIsNone(vkvideolive.profile("nobody"))

    def test_youtube_channel_link_and_video_link(self):
        tools = SimpleNamespace(ytdlp_json=mock.Mock(return_value={"channel": "К", "thumbnails": []}))
        youtube.profile("https://www.youtube.com/channel/UC1/live", tools)
        self.assertEqual(tools.ytdlp_json.call_args.args[0][-1], "https://www.youtube.com/channel/UC1")
        tools.ytdlp_json.reset_mock()
        self.assertIsNone(youtube.profile("https://www.youtube.com/watch?v=abc", tools))
        tools.ytdlp_json.assert_not_called()

    def test_book_is_parallel_safe_and_remembers(self):
        calls = []
        tools = SimpleNamespace(ytdlp_json=lambda args, timeout: calls.append(args) or {"channel": "Y"})
        book = sites.Profiles(tools)
        with mock.patch(REQUEST, return_value=answer(200, {"slug": "k", "user": {"username": "K"}})):
            found = book.profiles([("kick", "k"), ("youtube", "y"), ("youtube", "y"), ("tiktok", "t")])
        self.assertEqual(found, {("kick", "k"): {"name": "K", "avatar": ""},
                                 ("youtube", "y"): {"name": "Y", "avatar": ""}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(book.profiles([("youtube", "y")]), {("youtube", "y"): {"name": "Y", "avatar": ""}})
        self.assertEqual(len(calls), 1)

    def test_failure_is_retried_later_not_at_once(self):
        book = sites.Profiles()
        with mock.patch(REQUEST, return_value=answer(500)) as request:
            self.assertEqual(book.profiles([("vkvideolive", "kolya")]), {})
            self.assertEqual(book.profiles([("vkvideolive", "kolya")]), {})
        self.assertEqual(request.call_count, 1)


class RecordingTest(unittest.TestCase):

    def test_recording_chat(self):
        self.assertTrue(sites.recording_chat("twitch", "clip"))
        self.assertFalse(sites.recording_chat("twitch", "video"))
        self.assertTrue(sites.recording_chat("youtube", "video"))
        self.assertFalse(sites.recording_chat("youtube", "video", "not_live"))
        self.assertFalse(sites.recording_chat("other", "vod"))

    def test_listings_and_own_lister(self):
        self.assertEqual(list(sites.listings("youtube")), ["vods", "videos", "shorts"])
        self.assertIsNone(sites.own_lister("twitch"))
        self.assertIsNotNone(sites.own_lister("vkvideolive"))

    def test_window_info(self):
        info = sites.for_window()
        self.assertEqual(list(info), list(PLATFORMS))
        self.assertEqual(info["vkvideolive"]["search_note"], "нет канала с точно таким именем")
        self.assertEqual(info["twitch"]["unsupported"], {})

    def test_search_errors_are_site_errors(self):
        with mock.patch(REQUEST, return_value=answer(503)):
            with self.assertRaises(SiteError):
                sites.search("kick", "abc", 5)


if __name__ == "__main__":
    unittest.main()


class KickListingTest(unittest.TestCase):
    """Превью записей Kick: yt-dlp их не отдаёт — подставляются из списка записей сайта."""

    def setUp(self):
        for patcher in fresh_memos():
            patcher.start()
            self.addCleanup(patcher.stop)

    def respond(self, method, url, **kwargs):
        if "web.kick.com" in url:
            self.assertEqual(kwargs["headers"]["x-app-platform"], "web")
            return answer(200, {"data": [{"id": "v1", "thumbnail": {"src": "https://web.kick.com/t1.jpeg"}},
                                         {"id": "v2", "thumbnail": {"src": ""}}]})
        return answer(200, {"slug": "sus", "id": 77})

    def test_thumbnails_filled(self):
        result = {"entries": [{"id": "v1", "thumbnail": ""}, {"id": "v2", "thumbnail": ""},
                              {"id": "v3", "thumbnail": "https://own/3.jpg"}, {"id": "old", "thumbnail": ""}]}
        with mock.patch(REQUEST, side_effect=self.respond):
            sites.enrich_listing("kick", "sus", "vods", result)
        self.assertEqual([entry["thumbnail"] for entry in result["entries"]],
                         ["https://web.kick.com/t1.jpeg", "", "https://own/3.jpg", ""])

    def test_only_vods_and_failures_keep_list(self):
        clips = {"entries": [{"id": "c1", "thumbnail": ""}]}
        with mock.patch(REQUEST) as request:
            sites.enrich_listing("kick", "sus", "clips", clips)
            sites.enrich_listing("twitch", "x", "vods", {"entries": [{"id": "1", "thumbnail": ""}]})
        request.assert_not_called()
        vods = {"entries": [{"id": "v1", "thumbnail": ""}]}
        with mock.patch(REQUEST, return_value=answer(500)):
            sites.enrich_listing("kick", "sus", "vods", vods)     # площадка не ответила — список как был
        self.assertEqual(vods["entries"][0]["thumbnail"], "")
