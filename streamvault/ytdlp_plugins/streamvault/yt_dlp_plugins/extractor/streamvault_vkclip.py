# -*- coding: utf-8 -*-
"""
Расширение yt-dlp от StreamVault: клипы VK Video Live.

Записи VK yt-dlp понимает сам (VKPlayIE), а адреса клипов
live.vkvideo.ru/<канал>/clip/<id> — нет. Клип отдаётся тем же открытым
запросом сайта и в том же виде, что запись, поэтому форматы берутся
готовым разбором yt-dlp. Программа подключает расширение ключом
--plugin-dirs.
"""

from yt_dlp.extractor.vk import VKPlayBaseIE
from yt_dlp.utils import int_or_none, str_or_none, traverse_obj


class VKPlayClipIE(VKPlayBaseIE):
    IE_NAME = 'vkplay:clip'
    _VALID_URL = (r'https?://(?:www\.)?(?:live\.vkvideo\.ru|live\.vkplay\.ru|vkplay\.live)/'
                  r'(?P<username>[^/#?]+)/clip/(?P<id>[\da-f-]{36})')

    def _real_extract(self, url):
        username, video_id = self._match_valid_url(url).group('username', 'id')
        clip = traverse_obj(self._download_json(
            f'https://api.live.vkvideo.ru/v1/channel/{username}/clip/{video_id}', video_id),
            ('data', 'clip', {dict})) or {}
        return {
            'id': video_id,
            'title': clip.get('title') or video_id,
            'duration': int_or_none(clip.get('duration')),
            'timestamp': int_or_none(clip.get('createdAt')),
            'thumbnail': clip.get('previewUrl'),
            'uploader': traverse_obj(clip, ('blog', 'owner', 'displayName', {str})),
            'uploader_id': str_or_none(traverse_obj(clip, ('blog', 'owner', 'id'))),
            'channel': traverse_obj(clip, ('blog', 'owner', 'displayName', {str})) or username,
            'view_count': traverse_obj(clip, ('count', 'views', {int_or_none})),
            'formats': self._extract_formats(clip, video_id),
        }
