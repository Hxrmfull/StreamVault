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
import subprocess
import threading
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


def remux_command(ffmpeg, path, start, follow=False):
    """
    ffmpeg: запись с места start (секунды) — фрагментами mp4 в stdout, без
    перекодирования. follow — запись ещё пишется: читать файл по мере роста.
    """
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start > 0:
        command += ["-ss", "%.3f" % start]
    if follow:
        # -seekable 0: иначе ffmpeg прыгает в конец растущего файла (длительность,
        # поиск start) и у конца ждёт, пока streamlink допишет следующий кусок, —
        # у YouTube с кусками по 5 с первый кадр приходил через 10–15 с, и окно
        # сдавалось раньше. Без прыжков start ищется чтением по порядку, а разбор
        # потока хватает и секунды: в .ts эфира всегда H.264 и AAC.
        command += ["-follow", "1", "-rw_timeout", str(FOLLOW_TIMEOUT), "-seekable", "0",
                    "-analyzeduration", "1000000"]
    # звук в .ts — AAC в обёртке ADTS: mp4 её не принимает. delay_moov — заголовок
    # пишется после первых кадров, когда aac_adtstoasc уже знает настоящие параметры
    # звука: у YouTube в низком качестве это HE-AAC, и с заголовком наперёд окно
    # получало неверные параметры и останавливалось с ошибкой декодирования
    return command + ["-i", path, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
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
