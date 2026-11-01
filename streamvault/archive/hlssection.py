# -*- coding: utf-8 -*-
"""
Начало записи эфира из HLS — своими запросами, без ffmpeg.

Отрезок yt-dlp всегда отдаёт ffmpeg, а ffmpeg (сборки с GnuTLS) проверяет
сертификаты по хранилищу Windows как есть. CDN записей VK Video Live
(okcdn.ru) подписан HARICA TLS ECC Root CA 2021: этот корень Windows
докачивает только по запросу своего же TLS (браузер, .NET), и пока его нет,
ffmpeg получает «Peer certificate failed verification» на каждом куске.
Здесь плейлист и куски берутся через requests — с проверкой по набору
Mozilla (certifi), как у yt-dlp и streamlink. Отрезок от начала записи —
это просто первые куски плейлиста подряд: склеенные .ts — снова .ts.
"""

import logging
import os
from urllib.parse import urljoin

import requests

from ..i18n import tr

TIMEOUT = 30
ATTEMPTS = 3
CHUNK = 256 * 1024


class HlsSectionError(Exception):
    """Отрезок так не скачать — текст для человека."""


def playlist_segments(text, base_url):
    """
    Медиаплейлист HLS -> (init или None, [(длительность, ссылка)]).
    Зашифрованное и мастер-плейлист — HlsSectionError: это не наш случай.
    """
    init, segments, duration = None, [], None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            raise HlsSectionError(tr("это список качеств, а не плейлист записи"))
        if line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line:
            raise HlsSectionError(tr("запись зашифрована"))
        if line.startswith("#EXT-X-MAP"):
            uri = line.split('URI="', 1)[-1].split('"', 1)[0]
            init = urljoin(base_url, uri)
        elif line.startswith("#EXTINF:"):
            duration = float(line[len("#EXTINF:"):].split(",", 1)[0] or 0)
        elif line and not line.startswith("#") and duration is not None:
            segments.append((duration, urljoin(base_url, line)))
            duration = None
    if not segments:
        raise HlsSectionError(tr("в плейлисте записи нет кусков"))
    return init, segments


def first_segments(segments, length):
    """Куски с начала, покрывающие первые length секунд."""
    chosen, covered = [], 0.0
    for duration, url in segments:
        if covered >= length:
            break
        chosen.append(url)
        covered += duration
    return chosen


def download(playlist_url, headers, length, target, progress=None, stopped=None, session=None):
    """
    Первые length секунд записи по медиаплейлисту — в target (.ts, а у
    фрагментов mp4 — .mp4). progress(доля 0..1, байт), stopped() — прервать.
    Возвращает путь файла; недокачанное удаляется.
    """
    session = session or requests.Session()
    headers = dict(headers or {})
    try:
        answer = session.get(playlist_url, headers=headers, timeout=TIMEOUT)
        answer.raise_for_status()
    except requests.RequestException as error:
        raise HlsSectionError(tr("плейлист записи не скачался: {error}", error=error))
    init, segments = playlist_segments(answer.text, answer.url)
    urls = ([init] if init else []) + first_segments(segments, length)
    path = target + (".mp4" if init else ".ts")
    partial = path + ".part"
    written = 0
    try:
        with open(partial, "wb") as output:
            for index, url in enumerate(urls):
                if stopped and stopped():
                    raise HlsSectionError(tr("остановлено"))
                written += _fetch(session, url, headers, output)
                if progress:
                    progress((index + 1) / len(urls), written)
        os.replace(partial, path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    logging.debug("начало записи из HLS: %d кусков, %d байт -> %s", len(urls), written, path)
    return path


def _fetch(session, url, headers, output):
    """Кусок целиком в output; обрыв — ещё попытка с начала куска."""
    start = output.tell()
    for attempt in range(ATTEMPTS):
        output.seek(start)
        output.truncate()
        try:
            with session.get(url, headers=headers, timeout=TIMEOUT, stream=True) as answer:
                answer.raise_for_status()
                for chunk in answer.iter_content(CHUNK):
                    output.write(chunk)
            return output.tell() - start
        except requests.RequestException as error:
            if attempt == ATTEMPTS - 1:
                raise HlsSectionError(tr("кусок записи не скачался: {error}", error=error))
    return 0
