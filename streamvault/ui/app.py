# -*- coding: utf-8 -*-
"""
Запуск окна программы: движки записи и загрузок плюс интерфейс.

Обычно это окно pywebview (внутри — тот же движок, что у Edge). В режиме
--browser те же страницы открываются в браузере: удобно, если окно
не запускается, и для проверки интерфейса.
"""

import importlib.util
import json
import logging
import mimetypes
import os
import shutil
import socket
import threading
import webbrowser

from .. import __version__, i18n, util
from ..archive import ytdlp_update
from ..archive.downloads import DownloadManager
from ..editing import Editor
from ..events import EventBus
from ..ffmpeg import FFmpegInstaller, FFmpegLocator, own_dir_for
from ..twitchdownloader import TwitchDownloaderInstaller, TwitchDownloaderLocator
from ..twitchdownloader import own_dir_for as twitchdownloader_dir
from ..update import AppUpdater, finish_previous, program_dir
from ..i18n import tr
from ..chatindex import ChatIndex
from ..integrity import IntegrityChecker, IntegrityStore, integrity_path
from ..library import Library
from ..storyboard import Storyboards
from ..live.service import LiveService
from ..live.beginning import Beginning
from ..logs import BusLogHandler, MemoryLogHandler, make_console_output_safe, setup_logging
from ..positions import Positions
from ..settings import DEFAULT_SETTINGS_PATH, Settings, SettingsError
from . import autostart
from .api import Api
from .bridge import EventPump

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


class JsBridge:
    """Единственное, что видит JavaScript в окне pywebview: метод call."""

    def __init__(self, api):
        self._api = api

    def call(self, name, args=None):
        return self._api.call(name, args)


def run(settings_path=DEFAULT_SETTINGS_PATH, browser=False, port=0, open_browser=True,
        start_hidden=False):
    """
    start_hidden — запуститься сразу в трее, не показывая окна: так программа
    стартует вместе с Windows.
    """
    make_console_output_safe()
    # до чтения настроек — язык Windows: сообщения ниже тоже должны быть понятны
    i18n.set_language(i18n.detect_system_language())
    if not browser and importlib.util.find_spec("webview") is None:
        util.notify(tr("Для окна программы нужен pywebview: python -m pip install pywebview\n"
                       "Пока можно открыть StreamVault в браузере: "
                       "python -m streamvault gui --browser"))
        return 2
    if not util.single_instance():
        # первая копия, скорее всего, свёрнута в трей — просим её показаться
        if util.ShowSignal().fire():
            return 0
        util.notify(tr("StreamVault уже запущен — окном или консольной командой."))
        return 1
    try:
        settings = Settings.load(settings_path)
    except SettingsError as error:
        util.notify(tr("Настройки не читаются: {error}", error=error))
        return 2
    i18n.init(settings)

    level = getattr(logging, settings["log_level"].upper(), logging.INFO)
    app_dir = os.path.dirname(os.path.abspath(settings.path))
    setup_logging(level, os.path.join(app_dir, "logs", "streamvault.log"),
                  settings["log_max_bytes"], settings["log_backup_count"],
                  console=util.has_console())

    bus = EventBus()
    memory = MemoryLogHandler()
    logging.getLogger().addHandler(memory)
    logging.getLogger().addHandler(BusLogHandler(bus))

    # один поиск ffmpeg на всех: установили — увидели и запись, и загрузки
    ffmpeg = FFmpegLocator(settings, own_dir_for(settings))
    installer = FFmpegInstaller(
        ffmpeg, on_change=lambda install: bus.emit("ffmpeg", install=install,
                                                   status=ffmpeg.status()))
    # yt-dlp: встроенный или обновлённый кнопкой из окна — что новее
    ytdlp_copy = ytdlp_update.YtdlpLocator(ytdlp_update.own_dir_for(settings))
    ytdlp_updater = ytdlp_update.YtdlpUpdater(
        ytdlp_copy, on_change=lambda install: bus.emit("ytdlp", install=install,
                                                       status=ytdlp_copy.status()))
    # обновление самой программы: чем кончилось прошлое, есть ли новее
    last_update = finish_previous(program_dir())
    app_updater = AppUpdater.for_this_program(
        on_change=lambda install: bus.emit("app_update", install=install))
    if settings["check_updates"]:
        # не сразу: окно и записи важнее
        app_updater.check_in_background(delay=10)

    # TwitchDownloader — для чата Twitch; скачивается кнопкой, как ffmpeg
    twitchdownloader = TwitchDownloaderLocator(twitchdownloader_dir(settings))
    def twitchdownloader_changed(install):
        bus.emit("twitchdownloader", install=install, status=twitchdownloader.status())
        if install["state"] == "done":
            # чаты эфиров, записанные без него, теперь получат HTML
            live.rescan_chats()

    twitchdownloader_installer = TwitchDownloaderInstaller(
        twitchdownloader, on_change=twitchdownloader_changed)
    live = LiveService(settings, bus=bus, ffmpeg=ffmpeg, twitchdownloader=twitchdownloader,
                       ytdlp_copy=ytdlp_copy)
    downloads = DownloadManager(settings, bus=bus, ffmpeg=ffmpeg, ytdlp_copy=ytdlp_copy,
                                twitchdownloader=twitchdownloader)
    # запись, начатая посреди эфира, добирает его начало через очередь загрузок
    beginning = Beginning(downloads)
    live.set_beginning(beginning)
    problems = []
    for problem in live.preflight() + downloads.preflight():
        if problem not in problems:
            problems.append(problem)
            logging.error("%s", problem)
    if not ffmpeg.path():
        logging.warning(tr("ffmpeg не найден: записи не будут конвертироваться в mp4, "
                           "пока его не установят или не укажут путь в настройках"))

    # без streamlink запись невозможна, но скачивать готовое можно
    if shutil.which(util.STREAMLINK[0]):
        live.start()
    downloads.start()
    tray_available = importlib.util.find_spec("pystray") is not None
    if not tray_available:
        logging.warning(tr("pystray не установлен — значка в трее не будет: "
                           "python -m pip install pystray"))
    # клипы и видео с чатом поверх — по одной правке за раз, в своём потоке
    editor = Editor(ffmpeg.path, bus)
    editor.start()
    # пропуски в записях: очередь обработки ищет их сама, окно — по просьбе
    integrity = IntegrityStore.at(integrity_path(settings))
    checker = IntegrityChecker(ffmpeg.path, integrity, bus)
    checker.start()
    library = Library(settings, downloads, os.path.join(app_dir, "cache"), ffmpeg, integrity=integrity)
    # чаты всех записей — в базе для поиска и сводок
    chat_index = ChatIndex(os.path.join(app_dir, "cache", "chats.sqlite"),
                           lambda: list(library.folders().values()))
    api = Api(settings, live, downloads, logs=memory, problems=problems,
              library=library,
              autostart=autostart if os.name == "nt" else None, tray=tray_available,
              ffmpeg=ffmpeg, installer=installer, ytdlp_copy=ytdlp_copy,
              ytdlp_updater=ytdlp_updater, twitchdownloader=twitchdownloader,
              twitchdownloader_installer=twitchdownloader_installer,
              app_updater=app_updater, last_update=last_update,
              positions=Positions(os.path.join(app_dir, "cache", "positions.json")),
              editor=editor, integrity=integrity, checker=checker,
              storyboards=Storyboards(os.path.join(app_dir, "cache"), ffmpeg.path), chat_index=chat_index)
    logging.info(tr("StreamVault {version} запущен", version=__version__))

    try:
        if browser:
            _run_in_browser(api, bus, live, downloads, port, open_browser, tray_available)
        else:
            _run_window(api, bus, live, downloads, settings, app_dir,
                        start_hidden and tray_available, tray_available)
    finally:
        logging.info(tr("закрываю: останавливаю записи и загрузки, доделываю конвертацию"))
        installer.cancel()
        ytdlp_updater.cancel()
        twitchdownloader_installer.cancel()
        app_updater.cancel()
        editor.stop()
        checker.stop()
        beginning.stop()
        downloads.shutdown()
        left = live.shutdown(grace=0)
        chat_index.close()
        if left:
            logging.info(tr("необработанных записей: {count}, подхватятся при следующем "
                            "запуске", count=left))
        logging.info(tr("завершено"))
    return 0


def free_port():
    """Свободный порт на 127.0.0.1 — его выбирает система."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def tray_status(live, downloads):
    """Что сейчас пишется и сколько загрузок идёт — для значка в трее."""
    recording = [channel["label"] for channel in live.snapshot()["channels"]
                 if channel["state"] == "recording"]
    active = sum(1 for job in downloads.snapshot()["jobs"]
                 if job["state"] in ("queued", "downloading", "processing"))
    return {"recording": recording, "downloads": active}


def _run_window(api, bus, live, downloads, settings, app_dir, start_hidden, tray_available):
    import webview

    # Страницу отдаёт http-сервер pywebview, а тип файла он берёт у mimetypes.
    # В Windows тот читает реестр, где у .js бывает text/plain, — и окно,
    # собранное из модулей (<script type="module">), не загрузилось бы.
    mimetypes.add_type("text/javascript", ".js")

    window = webview.create_window(
        "StreamVault", url=os.path.join(WEB_DIR, "index.html"), js_api=JsBridge(api),
        width=1360, height=860, min_size=(960, 600), background_color="#0e0e10",
        hidden=start_hidden)
    api.attach_window(window)

    def deliver(batch):
        payload = json.dumps(batch, ensure_ascii=False)
        window.evaluate_js("window.sv && window.sv.receive(%s)" % payload)

    pump = EventPump(bus, live, deliver)
    quitting = threading.Event()
    hidden = threading.Event()
    if start_hidden:
        hidden.set()
    told_about_tray = threading.Event()

    def show_window():
        hidden.clear()
        window.show()
        window.restore()

    def hide_window():
        hidden.set()
        window.hide()
        if not told_about_tray.is_set() and tray is not None:
            told_about_tray.set()
            tray.notify(tr("StreamVault работает в фоне"),
                        tr("Записи продолжаются. Открыть окно или выйти — через значок в трее."))

    def quit_app():
        quitting.set()
        window.destroy()

    tray = None
    notifier = None
    if tray_available:
        from .tray import Tray, TrayNotifier
        tray = Tray(status=lambda: tray_status(live, downloads), on_open=show_window,
                    on_quit=quit_app, on_check=live.check_now,
                    on_folder=api.open_folder)
        tray.start()
        notifier = TrayNotifier(bus, tray, enabled=lambda: settings["tray_notifications"],
                                hidden=hidden.is_set)

    def on_loaded():
        if not pump.is_alive() and not quitting.is_set():
            pump.start()

    def on_closing():
        # Окно прячется в трей, а не закрывается: записи идут дальше. Без
        # значка — как раньше: если что-то пишется или качается, спросить.
        # Вызовы окна — из другого потока: этот держит окно и не дождался бы их.
        if quitting.is_set():
            return True
        if tray is not None and settings["close_to_tray"]:
            threading.Thread(target=hide_window, daemon=True).start()
            return False
        if not api.busy():
            quitting.set()
            return True
        threading.Thread(target=_ask_quit, args=(window,), daemon=True).start()
        return False

    api.on_quit = quit_app
    util.ShowSignal().listen(show_window)
    window.events.loaded += on_loaded
    window.events.closing += on_closing
    try:
        # своё хранилище — профиль WebView2 в папке программы. Порт — свободный от
        # системы: без него pywebview вне приватного режима берёт всегда 42001,
        # а Windows пускает на занятый порт второй сервер молча — и окно получало
        # страницы другой программы на pywebview (или другой копии StreamVault)
        webview.start(private_mode=False, storage_path=os.path.join(app_dir, "webview"),
                      http_port=free_port())
    finally:
        pump.stop()
        if notifier is not None:
            notifier.stop()
        if tray is not None:
            tray.stop()


def _ask_quit(window):
    try:
        window.evaluate_js("window.sv && window.sv.askQuit()")
    except Exception as error:
        logging.debug("не удалось спросить о закрытии: %s", error)


def _run_in_browser(api, bus, live, downloads, port, open_browser, tray_available):
    from .server import BrowserServer

    server = BrowserServer(api, WEB_DIR, port=port)
    pump = EventPump(bus, live, server.push)
    api.on_quit = server.stop
    server.start()
    pump.start()
    # адрес с токеном — только в консоль, не в лог-файл
    print("StreamVault открыт в браузере: %s" % server.url, flush=True)
    if open_browser:
        webbrowser.open(server.url)
    # значок и здесь: открыть вкладку заново или выйти, не ища консоль
    tray = None
    if tray_available:
        from .tray import Tray
        tray = Tray(status=lambda: tray_status(live, downloads),
                    on_open=lambda: webbrowser.open(server.url), on_quit=server.stop,
                    on_check=live.check_now, on_folder=api.open_folder)
        tray.start()
    util.ShowSignal().listen(lambda: webbrowser.open(server.url))
    try:
        server.wait()
    except KeyboardInterrupt:
        logging.info(tr("остановка по Ctrl+C"))
    finally:
        pump.stop()
        server.stop()
        if tray is not None:
            tray.stop()
