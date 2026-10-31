# -*- coding: utf-8 -*-
"""
Окно программы в обычном браузере.

Те же страницы, что в pywebview, только вызовы и события идут по HTTP.
Сервер слушает лишь 127.0.0.1, и каждый запрос к /api и /events обязан нести
случайный токен в заголовке: иначе любой сайт, открытый в браузере, мог бы
постучаться на localhost и, скажем, удалить загрузки.
"""

import collections
import hmac
import json
import logging
import mimetypes
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

TOKEN_HEADER = "X-StreamVault-Token"

# Политика безопасности страницы: скрипты и стили только свои, картинки —
# ещё и с https (превью и аватарки с сайтов платформ); видео и картинки чата
# в проигрывателе — с локального сервера записей (media.py).
CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' https: data: http://127.0.0.1:*; "
                           "media-src http://127.0.0.1:*; "
                           "style-src 'self'; script-src 'self'; connect-src 'self'")

_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
          ".ico": "image/x-icon"}


class BrowserServer:
    """HTTP-сервер для окна в браузере: страницы, вызовы /api и события /events."""

    # Сколько секунд держать запрос /events, если событий нет.
    EVENT_WAIT = 20
    # Сколько пачек событий помнить для отставших запросов.
    KEEP_BATCHES = 200
    # Больше этого тело вызова быть не может.
    MAX_BODY = 1_000_000

    def __init__(self, api, web_dir, port=0):
        self._api = api
        self._web_dir = os.path.abspath(web_dir)
        self.token = secrets.token_urlsafe(24)
        self._cond = threading.Condition()
        self._batches = collections.deque(maxlen=self.KEEP_BATCHES)
        self._seq = 0
        self._stopped = threading.Event()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = None

    @property
    def port(self):
        return self._httpd.server_address[1]

    @property
    def url(self):
        return "http://127.0.0.1:%d/?token=%s" % (self.port, self.token)

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="ui-http", daemon=True)
        self._thread.start()

    def stop(self):
        if self._stopped.is_set():
            return
        self._stopped.set()
        with self._cond:
            self._cond.notify_all()
        self._httpd.shutdown()
        self._httpd.server_close()

    def wait(self):
        """Ждёт остановки. Короткие ожидания пропускают Ctrl+C."""
        while not self._stopped.wait(0.5):
            pass

    def push(self, batch):
        """Пачка событий от EventPump."""
        with self._cond:
            self._seq += 1
            self._batches.append((self._seq, batch))
            self._cond.notify_all()

    def events_after(self, after, timeout):
        """
        Пачки событий новее номера after. after < 0 — только текущий номер,
        сразу: страница узнаёт его до первого снимка состояния и потом не
        получает второй раз то, что уже было в снимке (строки журнала).
        """
        with self._cond:
            if after < 0:
                return self._seq, []
            self._cond.wait_for(lambda: self._seq > after or self._stopped.is_set(), timeout)
            if after > self._seq:
                after = 0   # программу перезапустили, а страница помнит старый номер
            return self._seq, [batch for seq, batch in self._batches if seq > after]

    def _handler_class(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "StreamVault"

            def log_message(self, format, *args):
                logging.debug("http: " + format, *args)

            def _authorized(self):
                # Host проверяется против подмены адреса через DNS
                host = self.headers.get("Host", "")
                allowed = ("127.0.0.1:%d" % owner.port, "localhost:%d" % owner.port)
                token = self.headers.get(TOKEN_HEADER, "")
                return host in allowed and hmac.compare_digest(token, owner.token)

            def _send(self, status, body, content_type):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data, status=200):
                self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == "/events":
                    if not self._authorized():
                        return self._json({"ok": False, "error": "нет доступа"}, 403)
                    try:
                        after = int(parse_qs(parts.query).get("after", ["0"])[0])
                    except ValueError:
                        after = 0
                    seq, batches = owner.events_after(after, owner.EVENT_WAIT)
                    return self._json({"seq": seq, "batches": batches})
                self._static(parts.path)

            def do_POST(self):
                if urlsplit(self.path).path != "/api":
                    return self._json({"ok": False, "error": "нет такого адреса"}, 404)
                if not self._authorized():
                    return self._json({"ok": False, "error": "нет доступа"}, 403)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 <= length <= owner.MAX_BODY:
                    return self._json({"ok": False, "error": "слишком большой запрос"}, 413)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json({"ok": False, "error": "непонятный запрос"}, 400)
                if not isinstance(body, dict):
                    return self._json({"ok": False, "error": "непонятный запрос"}, 400)
                self._json(owner._api.call(body.get("name"), body.get("args")))

            def _static(self, path):
                if path in ("", "/"):
                    path = "/index.html"
                target = os.path.normpath(os.path.join(owner._web_dir, path.lstrip("/")))
                try:
                    inside = os.path.commonpath([target, owner._web_dir]) == owner._web_dir
                except ValueError:
                    inside = False
                if not inside or not os.path.isfile(target):
                    return self._send(404, b"not found", "text/plain; charset=utf-8")
                extension = os.path.splitext(target)[1].lower()
                content_type = (_TYPES.get(extension) or mimetypes.guess_type(target)[0]
                                or "application/octet-stream")
                with open(target, "rb") as source:
                    self._send(200, source.read(), content_type)

        return Handler
