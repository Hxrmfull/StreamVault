# -*- coding: utf-8 -*-
"""Поиск каналов по имени на всех платформах. Сеть подменяется."""
import unittest
from unittest import mock

import requests

from types import SimpleNamespace

from streamvault import search
from streamvault.sites import kick, twitch, vkvideolive, youtube


def answer(status=200, data=None):
    response = mock.Mock()
    response.status_code = status
    response.ok = 200 <= status < 300
    response.json.return_value = data
    return response


class PlatformsTest(unittest.TestCase):

    def test_twitch(self):
        data = {"data": {"searchFor": {"channels": {"items": [
            {"login": "xQc", "displayName": "xQc", "profileImageURL": "https://x/a.png",
             "followers": {"totalCount": 100}, "stream": None},
            {"login": "neon", "displayName": "Neon", "profileImageURL": "",
             "followers": {"totalCount": 5}, "stream": {"viewersCount": 3, "game": {"displayName": "Chess"}}},
        ]}}}}
        with mock.patch("streamvault.sites.common.requests.request", return_value=answer(200, data)) as request:
            found = twitch.search("xqc", 5)
        self.assertEqual(request.call_args.kwargs["json"]["variables"], {"query": "xqc"})
        self.assertEqual([item["id"] for item in found], ["twitch:xqc", "twitch:neon"])
        self.assertEqual((found[0]["followers"], found[0]["live"]), (100, False))
        self.assertEqual((found[1]["live"], found[1]["viewers"], found[1]["game"]), (True, 3, "Chess"))

    def test_kick(self):
        data = {"channels": [{"slug": "xqc", "isLive": True, "followers_count": 7,
                              "user": {"username": "xQc", "profile_pic": None}}]}
        with mock.patch("streamvault.sites.common.requests.request", return_value=answer(200, data)) as request:
            found = kick.search("xqc", 5)
        self.assertEqual(request.call_args.kwargs["params"], {"searched_word": "xqc"})
        self.assertEqual(found[0]["id"], "kick:xqc")
        self.assertEqual((found[0]["name"], found[0]["live"], found[0]["avatar"]), ("xQc", True, ""))

    def test_vkvideolive_exact_name(self):
        data = {"blogUrl": "kolya", "count": {"subscribers": 4},
                "owner": {"displayName": "Коля", "avatarUrl": "https://x/v.png", "hasAvatar": True}}
        with mock.patch("streamvault.sites.common.requests.request", return_value=answer(200, data)):
            self.assertEqual(vkvideolive.search("kolya", 5)[0]["name"], "Коля")
        with mock.patch("streamvault.sites.common.requests.request", return_value=answer(404)):
            self.assertEqual(vkvideolive.search("nobody", 5), [])
        # пробелы и прочее в имя канала VK не входят — сеть не трогаем
        with mock.patch("streamvault.sites.common.requests.request") as request:
            self.assertEqual(vkvideolive.search("два слова", 5), [])
        request.assert_not_called()

    def test_youtube_entries(self):
        entries = [
            {"uploader_id": "@xQcOW", "channel": "xQc", "channel_follower_count": 2510000,
             "thumbnails": [{"url": "//yt3.ggpht.com/small"}, {"url": "//yt3.ggpht.com/big"}]},
            {"uploader_id": "", "channel": "Без имени",
             "channel_url": "https://www.youtube.com/channel/UC123"},
            {"title": "ни имени, ни ссылки"},
        ]
        found = youtube.youtube_results(entries)
        self.assertEqual([item["id"] for item in found],
                         ["youtube:xQcOW", "youtube:https://www.youtube.com/channel/UC123/live"])
        self.assertEqual(found[0]["avatar"], "https://yt3.ggpht.com/big")

    def test_network_and_bad_answers(self):
        with mock.patch("streamvault.sites.common.requests.request",
                        side_effect=requests.exceptions.ConnectionError()):
            with self.assertRaisesRegex(search.SearchError, "нет связи"):
                kick.search("xqc", 5)
        with mock.patch("streamvault.sites.common.requests.request", return_value=answer(403)):
            with self.assertRaisesRegex(search.SearchError, "403"):
                kick.search("xqc", 5)


class ChannelSearchTest(unittest.TestCase):

    def test_cache_and_short_queries(self):
        calls = []

        def youtube(query, limit):
            calls.append(query)
            return [{"uploader_id": "@" + query, "channel": query}]

        finder = search.ChannelSearch(SimpleNamespace(youtube_channels=youtube))
        self.assertEqual(finder.search("youtube", "a"), [])          # одна буква — не ищем
        self.assertEqual(finder.search("youtube", "abc")[0]["id"], "youtube:abc")
        finder.search("youtube", "ABC ")                              # тот же запрос — из памяти
        self.assertEqual(calls, ["abc"])
        with self.assertRaises(ValueError):
            finder.search("tiktok", "abc")

    def test_unexpected_answer_is_search_error(self):
        finder = search.ChannelSearch(SimpleNamespace(youtube_channels=lambda query, limit: 42))
        with self.assertRaises(search.SearchError), self.assertLogs(level="DEBUG"):
            finder.search("youtube", "abc")


if __name__ == "__main__":
    unittest.main()
