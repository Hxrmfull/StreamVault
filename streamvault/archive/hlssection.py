# -*- coding: utf-8 -*-
"""
Начало записи эфира из HLS — своими запросами, без ffmpeg.

Отрезок yt-dlp всегда отдаёт ffmpeg, а тот качает куски по одному: у записи
идущего эфира Twitch выходило 30–250 КБ/с, когда CDN отдаёт по одному куску
около 2 МБ/с, а по четыре сразу — около 6 МБ/с. Здесь куски качаются по
несколько сразу (настройка «кусков записи параллельно»), а пишутся по порядку.

Для VK есть и вторая причина: ffmpeg (сборки с GnuTLS) проверяет
сертификаты по хранилищу Windows как есть. CDN записей VK Video Live
(okcdn.ru) подписан HARICA TLS ECC Root CA 2021: этот корень Windows
докачивает только по запросу своего же TLS (браузер, .NET), и пока его нет,
ffmpeg получает «Peer certificate failed verification» на каждом куске.
Здесь плейлист и куски берутся через requests — с проверкой по набору
Mozilla (certifi), как у yt-dlp и streamlink. Отрезок от начала записи —
это просто первые куски плейлиста подряд: склеенные .ts — снова .ts.
"""

import concurrent.futures
import logging
import os
import subprocess
import threading
import time
from urllib.parse import urljoin

import requests

from .. import util
from ..i18n import tr

TIMEOUT = 30
ATTEMPTS = 3
CHUNK = 256 * 1024
# Пауза перед следующей попыткой куска, секунды: сразу после обрыва CDN часто рвёт снова.
RETRY_PAUSES = (1, 3)


class HlsSectionError(Exception):
    """Отрезок так не скачать — текст для человека."""


class PlaylistError(HlsSectionError):
    """Плейлист не взять — до кусков дело не дошло: можно качать по-старому, через ffmpeg."""


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
        if line.startswith("#EXT-X-BYTERANGE"):
            # куски — части одного файла: качать каждый целиком значило бы писать мусор
            raise HlsSectionError(tr("куски записи заданы диапазонами байт"))
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


def download(playlist_url, headers, length, target, progress=None, stopped=None, session=None, workers=1):
    """
    Первые length секунд записи по медиаплейлисту — в target (.ts, а у
    фрагментов mp4 — .mp4). progress(доля 0..1, байт), stopped() — прервать.
    workers — сколько кусков качать сразу. Возвращает путь файла;
    недокачанное удаляется.
    """
    headers = dict(headers or {})
    try:
        answer = (session or requests).get(playlist_url, headers=headers, timeout=TIMEOUT)
        answer.raise_for_status()
    except requests.RequestException as error:
        raise PlaylistError(tr("плейлист записи не скачался: {error}", error=error))
    try:
        init, segments = playlist_segments(answer.text, answer.url)
    except HlsSectionError as error:
        raise PlaylistError(str(error))
    urls = ([init] if init else []) + first_segments(segments, length)
    path = target + (".mp4" if init else ".ts")
    partial = path + ".part"
    written = 0
    # своя сессия у каждого потока: requests.Session не обещает работы из нескольких сразу
    local = threading.local()

    def fetch(url):
        if stopped and stopped():
            raise HlsSectionError(tr("остановлено"))
        if session is not None:
            return _fetch(session, url, headers)
        if not hasattr(local, "session"):
            local.session = requests.Session()
        return _fetch(local.session, url, headers)

    workers = max(1, int(workers or 1))
    try:
        with open(partial, "wb") as output, \
                concurrent.futures.ThreadPoolExecutor(workers, thread_name_prefix="hls") as pool:
            # впереди — не больше двух кусков на поток: в памяти держится немного
            pending = [pool.submit(fetch, url) for url in urls[:workers * 2]]
            ahead = len(pending)
            for index in range(len(urls)):
                try:
                    data = pending.pop(0).result()
                except BaseException:
                    for future in pending:
                        future.cancel()
                    raise
                if ahead < len(urls):
                    pending.append(pool.submit(fetch, urls[ahead]))
                    ahead += 1
                output.write(data)
                written += len(data)
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


def from_zero(ffmpeg, path):
    """
    Склеенные куски HLS идут со временем самой записи (у Twitch — с 64-й
    секунды и дальше), а не с нуля, как у отрезка от ffmpeg. Склейка начала
    с записью по такому стыку не совпадала кадр в кадр: перепаковать в mp4
    без перекодирования, со временем от нуля — как отрезок от ffmpeg.
    Возвращает путь готового файла; не вышло — исходный, как есть.
    """
    target = os.path.splitext(path)[0] + ".mp4"
    partial = target + ".zero"
    try:
        # звук в .ts — AAC в обёртке ADTS: mp4 её не принимает
        result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", path,
                                 "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                                 # и первый кадр — ровно с нуля: иначе на стыке лишний кадр
                                 "-avoid_negative_ts", "make_zero",
                                 "-movflags", "+faststart", "-f", "mp4", partial], capture_output=True,
                                timeout=3600, creationflags=util.subprocess_flags())
        if result.returncode == 0 and os.path.getsize(partial) > 0:
            os.replace(partial, target)
            if target != path:
                os.remove(path)
            return target
        logging.info(tr("начало записи осталось со своим временем: {error}",
                        error=result.stderr.decode("utf-8", "replace").strip()[-200:]))
    except (OSError, subprocess.TimeoutExpired) as error:
        logging.info(tr("начало записи осталось со своим временем: {error}", error=error))
    try:
        os.remove(partial)
    except OSError:
        pass
    return path


def _fetch(session, url, headers):
    """Кусок целиком (байты); обрыв — ещё попытка с начала куска."""
    for attempt in range(ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSES[min(attempt, len(RETRY_PAUSES)) - 1])
        try:
            with session.get(url, headers=headers, timeout=TIMEOUT, stream=True) as answer:
                # Twitch: кусок с приглушённым звуком в плейлисте «-unmuted.ts», а лежит «-muted.ts»
                if answer.status_code == 403 and url.endswith("-unmuted.ts"):
                    return _fetch(session, url[:-len("-unmuted.ts")] + "-muted.ts", headers)
                answer.raise_for_status()
                return b"".join(answer.iter_content(CHUNK))
        except requests.RequestException as error:
            if attempt == ATTEMPTS - 1:
                raise HlsSectionError(tr("кусок записи не скачался: {error}", error=error))
    return b""
