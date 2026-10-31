# -*- coding: utf-8 -*-
"""Значок в трее: программа работает в фоне, пока окно закрыто."""

import logging
import threading

import pystray
from PIL import Image, ImageDraw

from ..i18n import tr
from ..live.storage import human_size


def draw_icon(recording=False, size=64):
    """
    Значок программы, нарисованный кодом, — тот же, что logo.svg. Во время
    записи в углу горит красная точка: видно, не открывая окно.
    """
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    unit = size / 32
    draw.rounded_rectangle([2 * unit, 4 * unit, 30 * unit, 28 * unit], radius=6 * unit, fill="#9147ff")
    draw.rounded_rectangle([6 * unit, 8 * unit, 26 * unit, 24 * unit], radius=3 * unit, fill="#0e0e10")
    draw.polygon([(13 * unit, 11 * unit), (13 * unit, 21 * unit), (21 * unit, 16 * unit)], fill="#efeff1")
    if recording:
        draw.ellipse([19 * unit, 0, 32 * unit, 13 * unit], fill="#eb0400",
                     outline="#0e0e10", width=max(1, int(2 * unit)))
    return image


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
