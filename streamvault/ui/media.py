# -*- coding: utf-8 -*-
"""
Видео и картинки чата для проигрывателя в окне программы.

Окно открывает свои страницы прямо с диска, а <video> нужен адрес, по
которому можно перематывать (запросы Range), — его даёт этот сервер.
Он слушает только 127.0.0.1, каждый адрес содержит случайный токен,
а отдаётся лишь то, что программа разрешила сама: проверенные файлы
записей (Api._media) и картинки открытого чата.

Запись в .ts браузер не проигрывает: её ffmpeg на лету перепаковывает
в mp4 без перекодирования — с заданного места (remux_url, ?start=секунды).
Перемотать такой поток сам <video> не может: перемотку делает окно,
запрашивая поток с нового места. Запись, которая ещё пишется, ffmpeg
читает по мере роста файла (follow): поток идёт, пока идёт запись.
"""

import hmac
import logging
import os
import re
import secrets
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .. import util

VIDEO_TYPES = {".mp4": "video/mp4", ".m4a": "audio/mp4", ".webm": "video/webm",
               ".mkv": "video/x-matroska", ".mp3": "audio/mpeg", ".ts": "video/mp2t",
               # листы кадров для шкалы проигрывателя (storyboard.py)
               ".jpg": "image/jpeg"}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
# Сколько ждать новых данных в растущей записи, микросекунды: дольше —
# запись кончилась (или эта часть оборвалась), поток заканчивается.
FOLLOW_TIMEOUT = 60 * 1000 * 1000


def parse_range(header, size):
    """'bytes=a-b' -> (начало, конец включительно); None — весь файл; ValueError — не выполнить."""
    if not header:
        return None
    match = _RANGE.match(header.strip())
    if not match or not (match.group(1) or match.group(2)):
        raise ValueError(header)
    if not match.group(1):
        # bytes=-N: последние N байт
        start, end = max(0, size - int(match.group(2))), size - 1
    else:
        start = int(match.group(1))
        end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
    if start >= size or start > end:
        raise ValueError(header)
    return start, end


TS_PACKET = 188
# Метки времени MPEG-TS идут по кругу через 2^33 тактов 90 кГц (≈26,5 ч).
PTS_WRAP = 1 << 33
# Сколько читать за раз, ища метку времени: в таком куске точно есть начало кадра или звука.
PTS_WINDOW = 512 * 1024
# Делить пополам, пока отрезок длиннее этого.
SEEK_PRECISION = 64 * 1024
# Ключевой кадр ищется не дальше этого назад: у YouTube он раз в 1 с, у Twitch и VK — в 2 с.
KEYFRAME_BACK = 8 * 1024 * 1024


def _pes(packet):
    """Начало PES в пакете TS (с заголовком PES) или None."""
    if packet[0] != 0x47 or not packet[1] & 0x40:
        return None
    control = (packet[3] >> 4) & 3
    offset = 4 + (1 + packet[4] if control & 2 else 0)
    if not control & 1 or offset + 14 > TS_PACKET:
        return None
    pes = packet[offset:]
    return pes if pes[:3] == b"\x00\x00\x01" and pes[7] & 0x80 else None


def _pts(pes):
    p = pes[9:14]
    return ((p[0] >> 1) & 7) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | p[4] >> 1


def _has_sps(pes):
    """Кадр H.264 с заголовком потока (SPS, NAL 7): с него декодер может начать."""
    at = pes.find(b"\x00\x00\x01", 9)
    while 0 <= at < len(pes) - 3:
        if pes[at + 3] & 0x1F == 7:
            return True
        at = pes.find(b"\x00\x00\x01", at + 3)
    return False


def _first_pts(handle, position):
    """(PTS первого PES от position, его байт) или (None, None)."""
    handle.seek(position)
    data = handle.read(PTS_WINDOW)
    begin = next((i for i in range(min(len(data) - 2 * TS_PACKET, 2 * TS_PACKET))
                  if data[i] == data[i + TS_PACKET] == data[i + 2 * TS_PACKET] == 0x47), None)
    if begin is None:
        return None, None
    for i in range(begin, len(data) - TS_PACKET + 1, TS_PACKET):
        pes = _pes(data[i:i + TS_PACKET])
        if pes is not None:
            return _pts(pes), position + i
    return None, None


def ts_seek_point(path, seconds):
    """
    Откуда читать .ts, чтобы попасть в seconds от начала записи: (байт, его
    время). Байт — начало ключевого кадра с заголовком потока (SPS): с него
    ffmpeg начинает сразу; время — не позже seconds. Место ищется делением
    пополам по меткам времени — за миллисекунды и у многочасовой записи, —
    кадр — назад от него. Не нашлось — (0, 0.0), с начала записи.
    Якорь — именно SPS: таблица PAT у VK идёт 10 раз в секунду, а флага
    ключевого кадра у YouTube нет вовсе.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        base, _ = _first_pts(handle, 0)
        if base is None:
            return 0, 0.0
        low, high, at = 0, size, 0
        while high - low > SEEK_PRECISION:
            middle = (low + high) // 2 // TS_PACKET * TS_PACKET
            pts, found = _first_pts(handle, middle)
            if pts is not None and ((pts - base) % PTS_WRAP) / 90000 <= seconds:
                low, at = middle, found
            else:
                high = middle
        begin = max(0, at - KEYFRAME_BACK) // TS_PACKET * TS_PACKET
        handle.seek(begin)
        data = handle.read(at + TS_PACKET - begin)
        last = (len(data) - TS_PACKET) // TS_PACKET * TS_PACKET
        for i in range(last, -1, -TS_PACKET):
            pes = _pes(data[i:i + TS_PACKET])
            if pes is not None and 0xE0 <= pes[3] <= 0xEF and _has_sps(pes):
                moment = ((_pts(pes) - base) % PTS_WRAP) / 90000
                if moment <= seconds:
                    return begin + i, moment
    return 0, 0.0


def _boxes(handle, begin, end):
    """Коробки mp4 подряд: (тип, начало, начало содержимого, конец); недописанная — не отдаётся."""
    at = begin
    while at + 8 <= end:
        handle.seek(at)
        head = handle.read(16)
        if len(head) < 8:
            return
        size, kind = struct.unpack(">I4s", head[:8])
        body = at + 8
        if size == 1:
            if len(head) < 16:
                return
            size, body = struct.unpack(">Q", head[8:16])[0], at + 16
        if size < 8 or at + size > end:
            return                       # size 0 («до конца») или ещё пишется
        yield kind, at, body, at + size
        at += size


def _video_track(handle, moov_body, moov_end):
    """(номер дорожки видео, её тактов в секунде, флаги кадра по умолчанию из trex) из moov или None."""
    found = _video_trak(handle, moov_body, moov_end)
    if found is None:
        return None
    defaults = 0
    for kind, _, body, end in _boxes(handle, moov_body, moov_end):
        if kind == b"mvex":
            for inner, _, inner_body, _ in _boxes(handle, body, end):
                handle.seek(inner_body)
                data = handle.read(24)
                if inner == b"trex" and len(data) == 24 and struct.unpack(">I", data[4:8])[0] == found[0]:
                    defaults = struct.unpack(">I", data[20:24])[0]
    return found + (defaults,)


def _video_trak(handle, moov_body, moov_end):
    for kind, _, body, end in _boxes(handle, moov_body, moov_end):
        if kind != b"trak":
            continue
        track, scale, video = None, None, False
        for inner, _, inner_body, inner_end in _boxes(handle, body, end):
            if inner == b"tkhd":
                handle.seek(inner_body)
                data = handle.read(24)
                track = struct.unpack(">I", data[20:24] if data[0] == 1 else data[12:16])[0]
            elif inner == b"mdia":
                for part, _, part_body, part_end in _boxes(handle, inner_body, inner_end):
                    handle.seek(part_body)
                    data = handle.read(32)
                    if part == b"mdhd":
                        scale = struct.unpack(">I", data[20:24] if data[0] == 1 else data[12:16])[0]
                    elif part == b"hdlr":
                        video = data[8:12] == b"vide"
        if video and track and scale:
            return track, scale
    return None


def fmp4_seek_point(path, seconds):
    """
    Запись Twitch из кусков mp4 внутри .ts, которая ещё пишется: откуда её
    читать, чтобы попасть в seconds. (конец заголовка ftyp+moov, байт начала
    куска moof, время куска от начала записи) или None — не разобрать.
    ffmpeg такой растущий файл сам не перематывает: ждёт его конца. Время
    куска — из его tfdt (по дорожке видео). Twitch с малой задержкой режет
    эфир на куски по 0,1 с, и ключевой кадр есть не в каждом: начать можно
    только с куска, у которого первый кадр — ключевой.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        header_end, track, first, chosen = None, None, None, None
        for kind, at, body, end in _boxes(handle, 0, size):
            if kind == b"moov":
                track = _video_track(handle, body, end)
                header_end = end
            elif kind == b"moof" and track is not None:
                fragment = _fragment_start(handle, body, end, track[0], track[2])
                if fragment is None:
                    continue
                moment, key = fragment
                if first is None:
                    first = moment
                offset_seconds = (moment - first) / track[1]
                if offset_seconds > seconds and chosen is not None:
                    break
                if key or chosen is None:
                    chosen = (header_end, at, offset_seconds)
        return chosen


# Флаг кадра mp4 «не ключевой» (sample_is_non_sync_sample)
_NON_SYNC = 0x00010000


def _fragment_start(handle, moof_body, moof_end, track, defaults):
    """
    (время начала куска — baseMediaDecodeTime из tfdt, первый кадр ключевой ли)
    для дорожки track или None. Флаги первого кадра — из trun, иначе из tfhd,
    иначе из trex (defaults).
    """
    for kind, _, body, end in _boxes(handle, moof_body, moof_end):
        if kind != b"traf":
            continue
        current, decode, flags = None, None, defaults
        run_flags = None
        for inner, _, inner_body, _ in _boxes(handle, body, end):
            handle.seek(inner_body)
            data = handle.read(40)
            if inner == b"tfhd":
                box_flags = int.from_bytes(data[1:4], "big")
                current = struct.unpack(">I", data[4:8])[0]
                at = 8 + (8 if box_flags & 0x1 else 0) + sum(4 for bit in (0x2, 0x8, 0x10) if box_flags & bit)
                if box_flags & 0x20 and len(data) >= at + 4:
                    flags = struct.unpack(">I", data[at:at + 4])[0]
            elif inner == b"tfdt":
                decode = struct.unpack(">Q", data[4:12])[0] if data[0] == 1 else struct.unpack(">I", data[4:8])[0]
            elif inner == b"trun":
                box_flags = int.from_bytes(data[1:4], "big")
                at = 8 + (4 if box_flags & 0x1 else 0)
                if box_flags & 0x4:
                    run_flags = struct.unpack(">I", data[at:at + 4])[0]
                elif box_flags & 0x400:
                    # флаги каждого кадра: у первого — после его длительности и размера
                    at += sum(4 for bit in (0x100, 0x200) if box_flags & bit)
                    run_flags = struct.unpack(">I", data[at:at + 4])[0]
        if current == track and decode is not None:
            first_flags = run_flags if run_flags is not None else flags
            return decode, not first_flags & _NON_SYNC
    return None


def fmp4_follow_command(ffmpeg, trim):
    """ffmpeg для растущей записи из кусков mp4: она приходит в stdin (заголовок и куски с нужного места)."""
    return [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", *util.SKIP_CORRUPT, "-f", "mov", "-i", "pipe:0",
            *(["-ss", "%.3f" % trim] if trim > 0.05 else []),
            "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-f", "mp4",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov", "pipe:1"]


def feed_growing(path, header_end, offset, target, stop, idle=FOLLOW_TIMEOUT / 1e6, chunk=256 * 1024):
    """
    Заголовок записи и её куски с offset — в target, по мере роста файла;
    дольше idle секунд без новых данных (запись кончилась) или stop — конец.
    """
    try:
        with open(path, "rb") as source:
            target.write(source.read(header_end))
            source.seek(offset)
            quiet = time.monotonic()
            while not stop.is_set():
                data = source.read(chunk)
                if data:
                    target.write(data)
                    quiet = time.monotonic()
                elif time.monotonic() - quiet > idle:
                    break
                else:
                    time.sleep(0.25)
    except (OSError, ValueError):
        pass                              # окно перемотало: ffmpeg закрыт, писать некуда
    finally:
        try:
            target.close()
        except OSError:
            pass


def remux_command(ffmpeg, path, start, follow=False):
    """
    ffmpeg: запись с места start (секунды) — фрагментами mp4 в stdout, без
    перекодирования. follow — запись ещё пишется: читать файл по мере роста.
    """
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    trim = []
    if follow and util.mp4_inside_ts(ffmpeg, path):
        # куски mp4 внутри .ts: растущий файл ffmpeg читает, только если не считает
        # его перематываемым, — иначе ждёт конца файла и не отдаёт ни кадра. Место —
        # обрезкой на выходе (запасной путь: обычно место находит fmp4_seek_point)
        command += ["-follow", "1", "-rw_timeout", str(FOLLOW_TIMEOUT), "-seekable", "0", *util.SKIP_CORRUPT]
        return command + ["-i", path, *(["-ss", "%.3f" % start] if start > 0 else []),
                          "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-sn", "-dn", "-f", "mp4",
                          "-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov", "pipe:1"]
    if follow:
        # Растущий файл ffmpeg не перематывает сам: поиск места (-ss до -i) и оценка
        # длительности лезут в конец файла и ждут там, пока streamlink допишет
        # следующий кусок, — у YouTube первый кадр приходил через 10–15 с. А с
        # -seekable 0 он молча игнорировал -ss, и любая перемотка начинала с начала
        # записи. Поэтому место находит программа (ts_seek_point), ffmpeg
        # перепрыгивает к нему, а остаток до start обрезает уже на выходе.
        try:
            offset, moment = ts_seek_point(path, start) if start > 0 else (0, 0.0)
        except OSError:
            offset, moment = 0, 0.0     # не прочитать — ffmpeg дойдёт до места сам, по порядку
        command += ["-skip_initial_bytes", str(offset), "-follow", "1", "-rw_timeout", str(FOLLOW_TIMEOUT),
                    "-skip_estimate_duration_from_pts", "1", "-analyzeduration", "1000000"]
        if start - moment > 0.05:
            trim = ["-ss", "%.3f" % (start - moment)]
    elif start > 0:
        command += ["-ss", "%.3f" % start]
    # звук в .ts — AAC в обёртке ADTS: mp4 её не принимает. delay_moov — заголовок
    # пишется после первых кадров, когда aac_adtstoasc уже знает настоящие параметры
    # звука: у YouTube в низком качестве это HE-AAC, и с заголовком наперёд окно
    # получало неверные параметры и останавливалось с ошибкой декодирования
    return command + ["-i", path, *trim, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                      "-sn", "-dn", "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov",
                      "pipe:1"]


class MediaServer:
    """
    images(chat_id, номер) -> (байты, тип) или None: картинки открытого чата.
    ffmpeg() -> путь к ffmpeg или None: им перепаковываются записи в .ts.
    """

    CHUNK = 256 * 1024

    def __init__(self, images=None, ffmpeg=None):
        self.token = secrets.token_urlsafe(24)
        self._images = images or (lambda chat_id, index: None)
        self._ffmpeg = ffmpeg or (lambda: None)
        self._files = {}           # id -> путь
        self._ids = {}             # путь -> id
        self._follow = set()       # id записей, которые ещё пишутся
        self._lock = threading.Lock()
        self._httpd = None

    @property
    def port(self):
        return self._httpd.server_address[1]

    def _base(self):
        with self._lock:
            if self._httpd is None:
                self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
                self._httpd.daemon_threads = True
                threading.Thread(target=self._httpd.serve_forever, name="media-http", daemon=True).start()
        return "http://127.0.0.1:%d/%s/" % (self.port, self.token)

    def file_url(self, path):
        """Адрес файла, уже проверенного вызывающим. Один и тот же для одного пути."""
        path = os.path.abspath(path)
        base = self._base()
        with self._lock:
            file_id = self._ids.get(path)
            if file_id is None:
                file_id = self._ids[path] = secrets.token_urlsafe(9)
                self._files[file_id] = path
        return base + "file/" + file_id + "/" + quote(os.path.basename(path))

    def remux_url(self, path, follow=False):
        """
        Адрес перепакованной в mp4 записи; место — параметром ?start=секунды.
        follow — запись ещё пишется: поток идёт за ростом файла.
        """
        url = self.file_url(path)
        if follow:
            with self._lock:
                self._follow.add(self._ids[os.path.abspath(path)])
        return url.replace("/file/", "/remux/", 1)

    def image_url(self, chat_id, index):
        return self._base() + "img/%s/%d" % (quote(chat_id), index)

    def stop(self):
        with self._lock:
            httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "StreamVault"

            def log_message(self, format, *args):
                logging.debug("media: " + format, *args)

            def do_HEAD(self):
                self._serve(body=False)

            def do_GET(self):
                self._serve(body=True)

            def _plain(self, status):
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _serve(self, body):
                # Host — против подмены адреса через DNS, токен — против чужих страниц
                host = self.headers.get("Host", "")
                parts = urlsplit(self.path).path.split("/")
                if host not in ("127.0.0.1:%d" % owner.port, "localhost:%d" % owner.port) \
                        or len(parts) < 4 or not hmac.compare_digest(parts[1], owner.token):
                    return self._plain(403)
                try:
                    if parts[2] in ("file", "remux"):
                        with owner._lock:
                            path = owner._files.get(parts[3])
                        if not path:
                            return self._plain(404)
                        if parts[2] == "file":
                            return self._file(path, body)
                        with owner._lock:
                            follow = parts[3] in owner._follow
                        return self._remux(path, body, follow)
                    if parts[2] == "img" and len(parts) == 5:
                        image = owner._images(unquote(parts[3]), int(parts[4]))
                        return self._image(image, body) if image else self._plain(404)
                except ValueError:
                    pass
                except (ConnectionError, TimeoutError):
                    return                     # перемотали — проигрыватель бросил запрос
                self._plain(404)

            def _image(self, image, body):
                data, content_type = image
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if body:
                    self.wfile.write(data)

            def _remux(self, path, body, follow=False):
                ffmpeg = owner._ffmpeg()
                if not ffmpeg:
                    return self._plain(404)
                start = float(parse_qs(urlsplit(self.path).query).get("start", ["0"])[0])
                if not 0 <= start < 1e7:
                    raise ValueError(start)
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not body:
                    return
                # длина заранее неизвестна: ответ кончается закрытием соединения
                plan = None
                if follow and util.mp4_inside_ts(ffmpeg, path):
                    try:
                        plan = fmp4_seek_point(path, start)
                    except (OSError, struct.error):
                        plan = None
                stop = threading.Event()
                if plan is not None:
                    # растущая запись из кусков mp4: её подаёт программа — заголовок
                    # и куски с нужного места, по мере роста файла
                    header_end, offset, moment = plan
                    process = subprocess.Popen(fmp4_follow_command(ffmpeg, start - moment), stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, stdin=subprocess.PIPE,
                                               creationflags=util.subprocess_flags())
                    threading.Thread(target=feed_growing, args=(path, header_end, offset, process.stdin, stop),
                                     name="media-feed", daemon=True).start()
                else:
                    process = subprocess.Popen(remux_command(ffmpeg, path, start, follow), stdout=subprocess.PIPE,
                                               stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                               creationflags=util.subprocess_flags())
                try:
                    while True:
                        chunk = process.stdout.read1(owner.CHUNK)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                finally:
                    # окно перемотало или закрылось — ffmpeg больше не нужен
                    stop.set()
                    if process.poll() is None:
                        process.kill()
                    process.stdout.close()
                    process.wait(10)
                    self.close_connection = True

            def _file(self, path, body):
                try:
                    size = os.path.getsize(path)
                    span = parse_range(self.headers.get("Range"), size)
                except OSError:
                    return self._plain(404)
                except ValueError:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start, end = span or (0, size - 1)
                self.send_response(206 if span else 200)
                self.send_header("Content-Type", VIDEO_TYPES.get(os.path.splitext(path)[1].lower(),
                                                                 "application/octet-stream"))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(max(0, end - start + 1)))
                if span:
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not body:
                    return
                with open(path, "rb") as source:
                    source.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = source.read(min(owner.CHUNK, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)

        return Handler
