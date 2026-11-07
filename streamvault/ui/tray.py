# -*- coding: utf-8 -*-
"""Значок в трее: программа работает в фоне, пока окно закрыто."""

import logging
import os
import threading

import pystray
from PIL import Image, ImageDraw

from ..i18n import tr
from ..live.storage import human_size


# Цвета логотипа (logo.svg): серебро — градиент по диагонали, фон — почти чёрный.
SILVER = ((0.0, (0xFF, 0xFF, 0xFF)), (0.25, (0xA7, 0xB0, 0xBD)), (0.5, (0xF0, 0xF3, 0xF7)),
          (0.75, (0x7D, 0x87, 0x96)), (1.0, (0xDC, 0xE1, 0xE8)))
BACK_TOP, BACK_BOTTOM = (0x1C, 0x1C, 0x1E), (0x05, 0x05, 0x06)
HUB, LIGHT, RED = "#0a0a0b", "#ffffff", "#eb0400"
# рисуется крупнее и уменьшается: у мелких значков (16 px) сглаженные края
SUPERSAMPLE = 8
# Логотип нарисован в квадрате 200×200; штурвал в нём увеличен в 1,15 раза от центра.
WHEEL_SCALE = 1.15
SPOKES = ((115.59, 109, 146.77, 127), (100, 118, 100, 154), (84.41, 109, 53.23, 127),
          (84.41, 91, 53.23, 73), (100, 82, 100, 46), (115.59, 91, 146.77, 73))
KNOBS = ((153.69, 131), (100, 162), (46.31, 131), (46.31, 69), (100, 38), (153.69, 69))


def _mix(stops, t):
    for (left, a), (right, b) in zip(stops, stops[1:]):
        if t <= right:
            share = (t - left) / (right - left)
            return tuple(round(x + (y - x) * share) for x, y in zip(a, b))
    return stops[-1][1]


def _diagonal(size, stops, start, end):
    """Градиент по диагонали: start..end — доли стороны, где он начинается и кончается."""
    step = 64
    small = Image.new("RGB", (step, step))
    small.putdata([_mix(stops, min(1.0, max(0.0, ((x + y) / 2 / (step - 1) - start) / (end - start))))
                   for y in range(step) for x in range(step)])
    return small.resize((size, size), Image.BICUBIC)


def draw_icon(recording=False, size=64, dot=None):
    """
    Значок программы, нарисованный кодом, — тот же, что logo.svg: серебряный
    штурвал с плеем в середине на чёрном. Красная точка в углу — в трее во
    время записи: видно, не открывая окно. dot — нарисовать её в любом случае.
    """
    dot = recording if dot is None else dot
    big = size * SUPERSAMPLE
    u = big / 200

    def at(value):
        """Координата штурвала в логотипе -> пиксель большого значка."""
        return (100 + (value - 100) * WHEEL_SCALE) * u

    def disc(draw, cx, cy, r, **style):
        draw.ellipse([at(cx) - r * WHEEL_SCALE * u, at(cy) - r * WHEEL_SCALE * u,
                      at(cx) + r * WHEEL_SCALE * u, at(cy) + r * WHEEL_SCALE * u], **style)

    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    shape = Image.new("L", (big, big), 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, big - 1, big - 1], radius=46 * u, fill=255)
    back = _diagonal(big, ((0.0, BACK_TOP), (1.0, BACK_BOTTOM)), 0.0, 1.0)
    image.paste(back, (0, 0), shape)

    # всё серебряное — одной маской, потом заливка градиентом
    silver = Image.new("L", (big, big), 0)
    pen = ImageDraw.Draw(silver)
    width = 8 * WHEEL_SCALE * u
    for x1, y1, x2, y2 in SPOKES:
        pen.line([(at(x1), at(y1)), (at(x2), at(y2))], fill=255, width=max(1, round(width)))
        for x, y in ((x1, y1), (x2, y2)):                       # круглые концы спиц
            disc(pen, x, y, 4, fill=255)
    for x, y in KNOBS:
        disc(pen, x, y, 8, fill=255)
    disc(pen, 100, 100, 44 + 5, outline=255, width=max(1, round(10 * WHEEL_SCALE * u)))   # обод
    disc(pen, 100, 100, 27, fill=255)                           # ступица
    # градиент логотипа — от (36, 36) до (164, 164) в координатах штурвала
    start, end = at(36) / big, at(164) / big
    image.paste(_diagonal(big, SILVER, start, end), (0, 0), silver)

    draw = ImageDraw.Draw(image)
    disc(draw, 100, 100, 21, fill=HUB)
    draw.polygon([(at(92), at(87)), (at(92), at(113)), (at(114), at(100))], fill=LIGHT)   # плей
    if dot:
        # точка записи — в правом верхнем углу, с чёрной обводкой
        draw.ellipse([(26.6 - 4.4) * big / 32, (5.4 - 4.4) * big / 32, (26.6 + 4.4) * big / 32,
                      (5.4 + 4.4) * big / 32], fill=HUB)
        draw.ellipse([(26.6 - 3.0) * big / 32, (5.4 - 3.0) * big / 32, (26.6 + 3.0) * big / 32,
                      (5.4 + 3.0) * big / 32], fill=RED)
    return image.resize((size, size), Image.LANCZOS)


def status_text(status):
    """Строка состояния для подсказки и меню."""
    names = status.get("recording") or []
    if names:
        text = tr("Пишется: {names}", names=", ".join(names))
    else:
        text = tr("Сейчас ничего не пишется")
    if status.get("downloads"):
        text += tr(" · загрузок: {count}", count=status["downloads"])
    return text


class Tray:
    """
    Значок с меню. У pystray свой цикл сообщений Windows, поэтому значок
    живёт в своём потоке: главный занят окном программы.
    """

    UPDATE_INTERVAL = 3

    def __init__(self, status, on_open, on_quit, on_check, on_folder, factory=None):
        """status() -> {"recording": [имена каналов], "downloads": число}."""
        self._status = status
        self._on_open = on_open
        self._on_quit = on_quit
        self._on_check = on_check
        self._on_folder = on_folder
        self._factory = factory or pystray.Icon
        self._images = {False: draw_icon(False), True: draw_icon(True)}
        self._recording = None
        self._text = "StreamVault"
        self._stop = threading.Event()
        self._icon = None

    def _menu(self):
        # подписи — функциями: меню перечитывает их, и смена языка видна сразу
        item = pystray.MenuItem
        return pystray.Menu(
            item(lambda _: tr("Открыть StreamVault"), self._call(self._on_open), default=True),
            pystray.Menu.SEPARATOR,
            item(lambda _: self._text, self._call(lambda: None), enabled=False),
            item(lambda _: tr("Проверить каналы сейчас"), self._call(self._on_check)),
            item(lambda _: tr("Папка записей"), self._call(lambda: self._on_folder("recordings"))),
            item(lambda _: tr("Папка загрузок"), self._call(lambda: self._on_folder("downloads"))),
            pystray.Menu.SEPARATOR,
            item(lambda _: tr("Выход"), self._call(self._on_quit)))

    @staticmethod
    def _call(action):
        # ошибка в обработчике не должна ронять цикл сообщений значка
        def run():
            try:
                action()
            except Exception:
                logging.exception(tr("действие из меню значка не удалось"))
        return run

    def start(self):
        self._icon = self._factory("StreamVault", self._images[False], "StreamVault",
                                   menu=self._menu())
        threading.Thread(target=self._run, name="tray", daemon=True).start()
        threading.Thread(target=self._update_loop, name="tray-status", daemon=True).start()

    def _run(self):
        try:
            self._icon.run()
        except Exception:
            logging.exception(tr("значок в трее не запустился"))

    def _update_loop(self):
        self.refresh()
        while not self._stop.wait(self.UPDATE_INTERVAL):
            self.refresh()

    def refresh(self):
        try:
            status = self._status()
        except Exception as error:
            logging.debug("состояние для значка не получено: %s", error)
            return
        self._text = status_text(status)
        recording = bool(status.get("recording"))
        try:
            # у подсказки Windows предел в 127 символов
            self._icon.title = ("StreamVault — " + self._text)[:127]
            if recording != self._recording:
                self._icon.icon = self._images[recording]
                self._recording = recording
            self._icon.update_menu()
        except Exception as error:
            logging.debug("значок не обновился: %s", error)

    def notify(self, title, message):
        try:
            self._icon.notify(message, title)
        except Exception as error:
            logging.debug("уведомление не показано: %s", error)

    def stop(self):
        self._stop.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass


class TrayNotifier:
    """
    Уведомления в трее: началась запись, скачалось. Только пока окно
    скрыто — с открытым окном всё и так видно.
    """

    def __init__(self, bus, tray, enabled, hidden):
        self._tray = tray
        self._enabled = enabled
        self._hidden = hidden
        self._channels = {}
        self._jobs = {}
        self._unsubscribe = bus.subscribe(self._on_event)

    def _on_event(self, event):
        if event["type"] == "channel":
            channel = event["channel"]
            before = self._channels.get(channel["id"])
            self._channels[channel["id"]] = channel["state"]
            stream = channel.get("stream") or {}
            if channel["state"] == "recording" and before != "recording":
                self._show(tr("Запись началась: {name}", name=channel.get("label", channel["id"])),
                           stream.get("title") or tr("Эфир пишется в папку записей"))
            elif channel["state"] == "live" and before == "offline":
                # автозапись выключена — о начале эфира стоит сказать
                self._show(tr("{name} в эфире", name=channel.get("label", channel["id"])),
                           stream.get("title") or tr("Записать можно в окне StreamVault"))
        elif event["type"] == "disk" and event["level"] in ("low", "critical"):
            # о месте на диске — и при открытом окне: запись вот-вот остановится
            message = (tr("Осталось {free}. Запись остановлена, пока место не освободится.",
                          free=human_size(event["free"])) if event["level"] == "critical"
                       else tr("Осталось {free}. Освободи место, иначе запись остановится.",
                               free=human_size(event["free"])))
            if self._enabled():
                self._tray.notify(tr("Мало места на диске с записями"), message)
        elif event["type"] == "joining" and event.get("state") in ("done", "failed"):
            # склейка идёт минутами — окно к её концу обычно свёрнуто
            name = os.path.basename(event.get("main") or "")
            if event["state"] == "done":
                self._show(tr("Начало эфира склеено с записью"), name)
            else:
                self._show(tr("Начало эфира не склеено с записью"), name + " — " + (event.get("reason") or ""))
        elif event["type"] == "download":
            job = event["job"]
            before = self._jobs.get(job["id"])
            self._jobs[job["id"]] = job["state"]
            if before is not None and before != job["state"]:
                if job["state"] == "done":
                    self._show(tr("Скачано"), job.get("title") or job.get("url", ""))
                elif job["state"] == "failed":
                    self._show(tr("Не скачалось"), (job.get("title") or job.get("url", ""))
                               + " — " + (job.get("error") or ""))

    def _show(self, title, message):
        if self._enabled() and self._hidden():
            self._tray.notify(title, message[:200])

    def stop(self):
        self._unsubscribe()
