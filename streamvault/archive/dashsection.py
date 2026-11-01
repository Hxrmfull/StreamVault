# -*- coding: utf-8 -*-
"""
Начало записи YouTube — своими запросами, с докачкой после обрыва.

Отрезок yt-dlp отдаёт ffmpeg, а тот держит к YouTube долгие соединения, и
YouTube их рвёт (WSAECONNRESET): у пользователя загрузка начала эфира падала
раз за разом через полминуты-три, переподключение ffmpeg не помогало.

Видео и звук YouTube — отдельные mp4 с оглавлением sidx в начале: оно
говорит, сколько байт занимает каждый кусок времени. Значит, первые N секунд —
это первые байты файла. Они качаются частями по 10 МБ (так YouTube не
тормозит отдачу), обрыв — повтор с того же байта. Потом ffmpeg без сети
собирает видео со звуком и обрезает по N секунд.
"""

import logging
import os
import struct
import subprocess

import requests

from .. import util
from ..i18n import tr

TIMEOUT = 30
CHUNK = 10 * 1024 * 1024
ATTEMPTS = 5
HEADER = 256 * 1024


class DashSectionError(Exception):
    """Отрезок так не скачать — текст для человека."""


class NoIndex(DashSectionError):
    """У файла нет оглавления sidx: отрезок так не взять — качать обычным путём, через ffmpeg."""


def section_end(data, length):
    """
    Начало mp4 (ftyp, moov, sidx) -> сколько байт с начала файла покрывают
    первые length секунд. Нет оглавления — DashSectionError.
    """
    position = 0
    while position + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[position:position + 8])
        if size < 8:
            break
        if kind == b"sidx":
            if position + size > len(data):
                raise NoIndex(tr("оглавление записи не уместилось в начало файла"))
            return _walk_sidx(data[position + 8:position + size], position + size, length)
        position += size
    raise NoIndex(tr("у записи нет оглавления (sidx)"))


def _walk_sidx(body, after, length):
    version = body[0]
    timescale = struct.unpack(">I", body[8:12])[0]
    if version == 0:
        first_offset = struct.unpack(">I", body[16:20])[0]
        position = 20
    else:
        first_offset = struct.unpack(">Q", body[20:28])[0]
        position = 28
    count = struct.unpack(">H", body[position + 2:position + 4])[0]
    position += 4
    end, covered = after + first_offset, 0.0
    for index in range(count):
        reference, duration = struct.unpack(">II", body[position + 12 * index:position + 12 * index + 8])
        end += reference & 0x7FFFFFFF
        covered += duration / float(timescale or 1)
        if covered >= length:
            break
    return end


def download(streams, length, target, ffmpeg, progress=None, stopped=None, session=None):
    """
    streams — [(ссылка, заголовки)] видео и звука YouTube. Первые length
    секунд — в target.mp4. progress(доля 0..1, байт), stopped() — прервать.
    Возвращает путь файла; промежуточное удаляется.
    """
    session = session or requests.Session()
    path, parts = target + ".mp4", []
    try:
        plans = []
        for index, (url, headers) in enumerate(streams):
            head = _range(session, url, headers, 0, HEADER - 1)
            plans.append((url, headers, section_end(head, length)))
        total, done = sum(end for _, _, end in plans), 0
        for index, (url, headers, end) in enumerate(plans):
            part = "%s.f%d.part" % (target, index)
            parts.append(part)
            with open(part, "wb") as output:
                while output.tell() < end:
                    if stopped and stopped():
                        raise DashSectionError(tr("остановлено"))
                    start = output.tell()
                    output.write(_range(session, url, headers, start, min(end, start + CHUNK) - 1))
                    if progress:
                        progress((done + output.tell()) / float(total), done + output.tell())
            done += end
        _mux(ffmpeg, parts, length, path)
    except BaseException:
        util.remove_quietly(path)
        raise
    finally:
        for part in parts:
            util.remove_quietly(part)
    logging.debug("начало записи YouTube: %d байт -> %s", total, path)
    return path


def _range(session, url, headers, start, last):
    """Байты start..last; обрыв или ответ не тот — ещё попытка."""
    for attempt in range(ATTEMPTS):
        try:
            answer = session.get(url, headers=dict(headers or {}, Range="bytes=%d-%d" % (start, last)),
                                 timeout=TIMEOUT)
            answer.raise_for_status()
            if answer.status_code == 206 and answer.content:
                return answer.content
            error = tr("площадка ответила {status}", status=answer.status_code)
        except requests.RequestException as problem:
            error = str(problem)
        logging.debug("часть записи YouTube (%d-%d), попытка %d: %s", start, last, attempt + 1, error)
    raise DashSectionError(tr("часть записи не скачалась: {error}", error=error))


def _mux(ffmpeg, parts, length, path):
    """Видео со звуком — в один mp4, ровно length секунд. Без сети: файлы уже на диске."""
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
    for part in parts:
        command += ["-i", part]
    for index in range(len(parts)):
        command += ["-map", "%d:0" % index]
    command += ["-c", "copy", "-t", "%.3f" % length, "-movflags", "+faststart", "-f", "mp4", path]
    result = subprocess.run(command, capture_output=True, creationflags=util.subprocess_flags())
    if result.returncode != 0 or not os.path.isfile(path):
        raise DashSectionError(tr("ffmpeg не собрал начало записи: {error}",
                                  error=result.stderr.decode("utf-8", "replace").strip()[-300:]))
