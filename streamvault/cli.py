# -*- coding: utf-8 -*-
"""
Командная строка StreamVault.

  python -m streamvault                 открыть окно программы
  python -m streamvault gui --browser   то же окно в браузере
  python -m streamvault record          записывать эфиры каналов из настроек
  python -m streamvault record -u kick:xqc
  python -m streamvault info twitch:xqc --kind clips
  python -m streamvault download ССЫЛКА -q 720 --section 1:00:00-1:30:00
  python -m streamvault platforms       поддерживаемые платформы
  python -m streamvault import-config ПУТЬ\\config.py

Язык — как у окна: из настроек, а пока его там нет — язык Windows.
"""

import argparse
import datetime
import logging
import os
import sys
import time

from . import __version__, i18n, sites, util
from .archive import chat
from .archive import downloads as dl
from .archive import ytdlp
from .console import StatusLine, download_status, format_duration, format_size, recording_status
from .i18n import tr
from .live.recorder import ChannelRecorder
from .live.service import LiveService
from .logs import make_console_output_safe, setup_logging
from .platforms import PLATFORMS, is_url, parse_channel
from .settings import DEFAULT_SETTINGS_PATH, Settings, SettingsError, import_legacy_config

# Как часто консольная загрузка пишет прогресс, секунды.
PROGRESS_REPORT_INTERVAL = 5

# Свои заголовки и ошибки argparse пишет через gettext, по-английски. Ключ —
# его английская строка (в tr() наоборот — русская), поэтому таблица своя.
ARGPARSE_RUSSIAN = {
    "usage: ": "использование: ",
    "positional arguments": "аргументы",
    "options": "ключи",
    "show this help message and exit": "показать эту справку и выйти",
    " (default: %(default)s)": " (по умолчанию: %(default)s)",
    "%(prog)s: error: %(message)s\n": "%(prog)s: ошибка: %(message)s\n",
    "the following arguments are required: %s": "не хватает аргументов: %s",
    "unrecognized arguments: %s": "непонятные аргументы: %s",
    "argument %(argument_name)s: %(message)s": "аргумент %(argument_name)s: %(message)s",
    "invalid choice: %(value)r (choose from %(choices)s)": "нет такого варианта: %(value)r (есть: %(choices)s)",
    "invalid %(type)s value: %(value)r": "не подходит как %(type)s: %(value)r",
    "expected one argument": "нужно одно значение",
    "ambiguous option: %(option)s could match %(matches)s": "неоднозначный ключ: %(option)s — это %(matches)s?",
}
_ARGPARSE_GETTEXT = argparse._


def choose_language(argv):
    """
    Язык консоли — как у окна: из настроек, а пока его там нет — язык
    Windows. Настройки при этом не меняются. Выбирается до разбора
    аргументов: на этом языке и подсказки --help.
    """
    i18n.set_language(i18n.detect_system_language())
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--settings", default=DEFAULT_SETTINGS_PATH)
    known, _ = early.parse_known_args(argv)
    try:
        i18n.init(Settings.load(known.settings), persist=False)
    except SettingsError:
        pass      # о битых настройках скажет сама команда — на языке Windows
    # argparse берёт переводы у своей глобальной _ в момент вывода: подменяем её
    if i18n.current_language() == i18n.RUSSIAN:
        argparse._ = lambda text: ARGPARSE_RUSSIAN.get(text, _ARGPARSE_GETTEXT(text))
    else:
        argparse._ = _ARGPARSE_GETTEXT


def build_parser():
    parser = argparse.ArgumentParser(
        prog="streamvault",
        description=tr("StreamVault {version}: запись эфиров и скачивание записей и клипов "
                       "Twitch, Kick, YouTube, VK Video Live", version=__version__))
    parser.add_argument("--settings", metavar=tr("ФАЙЛ"), default=DEFAULT_SETTINGS_PATH,
                        help=tr("файл настроек (по умолчанию settings.json рядом с программой)"))
    commands = parser.add_subparsers(dest="command", metavar=tr("КОМАНДА"))
    level = tr("УРОВЕНЬ")

    gui = commands.add_parser("gui", help=tr("открыть окно программы (так же без команды)"))
    gui.add_argument("--browser", action="store_true",
                     help=tr("открыть в браузере, а не в отдельном окне"))
    gui.add_argument("--port", type=int, default=0, help=tr("порт для режима браузера"))
    gui.add_argument("--no-open", action="store_true",
                     help=tr("не открывать браузер самому, только напечатать адрес"))
    gui.add_argument("--tray", action="store_true",
                     help=tr("запуститься свёрнутым в трей, без окна"))

    record = commands.add_parser("record", help=tr("записывать эфиры, пока не нажат Ctrl+C"))
    record.add_argument("-u", "--channel", dest="channel", action="append",
                        metavar=tr("[ПЛАТФОРМА:]КАНАЛ"),
                        help=tr("например kick:xqc или legendaafk; можно повторять "
                                "и перечислять через запятую. Без префикса — twitch. "
                                "С этим ключом список каналов в настройках не меняется"))
    record.add_argument("-q", "--quality", metavar=tr("КАЧЕСТВО"),
                        help=tr("качество записи, например best или 720p"))
    record.add_argument("-l", "--log", dest="log", default=None,
                        metavar=level, help="debug, info, warning, error")
    record.add_argument("--disable-ffmpeg", action="store_true",
                        help=tr("не конвертировать, просто переносить записи"))

    download = commands.add_parser("download", help=tr("скачать записи и клипы по ссылкам"))
    download.add_argument("urls", nargs="+", metavar=tr("ССЫЛКА"))
    download.add_argument("-q", "--quality", metavar=tr("КАЧЕСТВО"),
                          help=tr("best, worst, audio, высота кадра (1080, 720...) "
                                  "или формат из команды info"))
    download.add_argument("--section", metavar=tr("НАЧАЛО-КОНЕЦ"),
                          help=tr("скачать только отрезок, например 1:00:00-1:30:00"))
    download.add_argument("--force", action="store_true",
                          help=tr("скачать заново, даже если уже скачано"))
    download.add_argument("--chat", choices=chat.MODES, default=None,
                          help=tr("чат записей и клипов Twitch через TwitchDownloader: none, "
                                  "text (HTML и JSON) или render (ещё и видео чата); "
                                  "по умолчанию — как в настройках"))
    download.add_argument("-l", "--log", dest="log", default=None,
                          metavar=level, help="debug, info, warning, error")

    info = commands.add_parser(
        "info", help=tr("что лежит по ссылке или какие записи есть у канала"))
    info.add_argument("target", metavar=tr("ССЫЛКА|ПЛАТФОРМА:КАНАЛ"))
    info.add_argument("--kind", default="vods",
                      help=tr("vods, clips, highlights, uploads (Twitch), "
                              "videos, shorts (YouTube); по умолчанию vods"))
    info.add_argument("--period", default="all", choices=ytdlp.CLIP_PERIODS,
                      help=tr("за какой срок клипы Twitch: 24hr, 7d, 30d, all"))
    info.add_argument("--limit", type=int, default=20, help=tr("сколько записей показать"))

    commands.add_parser("platforms", help=tr("показать поддерживаемые платформы"))

    importer = commands.add_parser(
        "import-config", help=tr("перенести config.py старого рекордера в настройки"))
    importer.add_argument("path", metavar="CONFIG_PY")
    importer.add_argument("--force", action="store_true",
                          help=tr("перезаписать уже существующий файл настроек"))
    return parser


def main(argv=None):
    choose_language(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    make_console_output_safe()

    if args.command == "platforms":
        for name, spec in PLATFORMS.items():
            print("  %-12s %-38s %s" % (name, spec["url"],
                                        tr("опрос раз в {seconds} с", seconds=spec["refresh"])))
        return 0
    if args.command == "import-config":
        return import_config(args)
    if args.command == "record":
        return record(args, parser)
    if args.command == "download":
        return download(args, parser)
    if args.command == "info":
        return show_info(args)
    # без команды — окно программы
    from .ui.app import run
    if args.command == "gui":
        return run(args.settings, browser=args.browser, port=args.port,
                   open_browser=not args.no_open, start_hidden=args.tray)
    return run(args.settings)


def claim_instance():
    """Вторая копия записывала бы те же каналы и затирала бы очередь загрузок."""
    if util.single_instance():
        # streamlink и ffmpeg закрываются вместе с консольной записью, как бы её ни закрыли
        util.bind_children()
        return True
    print(tr("ошибка: StreamVault уже запущен — окном или другой консольной командой. "
             "Закрой его или добавь каналы и ссылки в нём"), file=sys.stderr)
    return False


def load_settings_and_log(args, parser):
    """Настройки и лог для долгих команд. None — настройки не читаются."""
    try:
        settings = Settings.load(args.settings)
    except SettingsError as error:
        print(tr("ошибка: {error}", error=error), file=sys.stderr)
        return None

    level_name = (args.log or settings["log_level"]).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        parser.error(tr("неизвестный уровень логирования: {level}", level=level_name))

    log_dir = os.path.join(os.path.dirname(os.path.abspath(settings.path)), "logs")
    setup_logging(level, os.path.join(log_dir, "streamvault.log"),
                  settings["log_max_bytes"], settings["log_backup_count"])
    return settings


def import_config(args):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if os.path.exists(args.settings) and not args.force:
        logging.error(tr("{path} уже есть. Чтобы перезаписать, добавь --force", path=args.settings))
        return 2
    try:
        settings = import_legacy_config(args.path, args.settings)
    except (OSError, SyntaxError) as error:
        logging.error(tr("не удалось прочитать {path}: {error}", path=args.path, error=error))
        return 2
    channels = settings["channels"]
    logging.info(tr("настройки сохранены в {path}", path=settings.path))
    names = " (%s)" % ", ".join("%s:%s" % (c["platform"], c["channel"]) for c in channels) if channels else ""
    logging.info(tr("каналов: {count}{names}", count=len(channels), names=names))
    if settings["twitch_client_id"]:
        logging.info(tr("ключи Twitch перенесены"))
    return 0


def record(args, parser):
    if not claim_instance():
        return 2
    settings = load_settings_and_log(args, parser)
    if settings is None:
        return 2

    service = LiveService(settings, channels=args.channel, quality=args.quality, watch_all=False,
                          disable_ffmpeg=args.disable_ffmpeg)
    if not service.has_enabled_channels():
        logging.critical(tr("не задан ни один канал: добавь каналы в {path} (channels) "
                            "или передай -u", path=settings.path))
        return 2
    problems = service.preflight()
    if problems:
        for problem in problems:
            logging.critical("%s", problem)
        return 2

    service.start()
    # живая строка горит и после Ctrl+C: видно, что ещё обрабатывается
    status = StatusLine(recording_status(service)).start()
    try:
        try:
            while service.is_running():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logging.info(tr("остановка по Ctrl+C"))

        try:
            # Ctrl+C дошёл и до streamlink: даём ему закрыть файл самому
            left = service.shutdown(grace=ChannelRecorder.STOP_GRACE)
        except KeyboardInterrupt:
            # второй Ctrl+C, пока доделывается обработка: выходим без трассировки.
            # Недописанный mp4 останется с расширением .part и будет убран сам.
            logging.warning(tr("повторный Ctrl+C — выхожу, не дожидаясь обработки. "
                               "Необработанные записи подхватятся при следующем запуске"))
            return 130
    finally:
        status.stop()

    if left:
        logging.info(tr("осталось необработанных записей: {count}. "
                        "Они подхватятся при следующем запуске", count=left))
    logging.info(tr("завершено"))
    return 0


def describe_progress(job):
    stage = job.get("chat_progress")
    if job["state"] == dl.PROCESSING and stage:
        # видео уже скачано — идёт чат
        percent = stage.get("percent")
        return tr("чат") + (" %d%%" % percent if percent is not None else "")
    progress = job.get("progress") or {}
    parts = []
    if progress.get("percent") is not None:
        parts.append("%.0f%%" % progress["percent"])
    if progress.get("downloaded"):
        parts.append(tr("{done} из {total}", done=format_size(progress["downloaded"]),
                        total=format_size(progress.get("total"))))
    if progress.get("speed"):
        parts.append(tr("{size}/с", size=format_size(progress["speed"])))
    if progress.get("eta") is not None:
        parts.append(tr("осталось {time}", time=format_duration(progress["eta"])))
    if job["state"] == dl.PROCESSING:
        parts.append(tr("склейка"))
    return ", ".join(parts) or tr("начинаю")


def download(args, parser):
    try:
        section = ytdlp.parse_section(args.section) if args.section else None
    except ValueError as error:
        parser.error(str(error))
    if not claim_instance():
        return 2
    settings = load_settings_and_log(args, parser)
    if settings is None:
        return 2

    manager = dl.DownloadManager(settings, resume=False)
    problems = manager.preflight()
    if problems:
        for problem in problems:
            logging.critical("%s", problem)
        return 2

    wanted = []
    for url in args.urls:
        job_id = manager.add(url, quality=args.quality, section=section, force=args.force,
                             chat_mode=args.chat)
        job = manager.job(job_id)
        if job["state"] == dl.DONE:
            logging.info(tr("уже скачано: {path} (заново — с --force)", path=job["file"]))
        else:
            wanted.append(job_id)
    if not wanted:
        return 0

    manager.start()
    status = StatusLine(download_status(manager, wanted, describe_progress)).start()
    finished = (dl.DONE, dl.FAILED, dl.CANCELLED, dl.PAUSED)
    reported = 0.0
    try:
        while True:
            jobs = [manager.job(job_id) for job_id in wanted]
            if all(job["state"] in finished for job in jobs):
                break
            # в консоли прогресс рисует живая строка; в файл или другой программе — строки раз в 5 с
            if not status.enabled and time.monotonic() - reported >= PROGRESS_REPORT_INTERVAL:
                reported = time.monotonic()
                for job in jobs:
                    if job["state"] in dl.ACTIVE:
                        logging.info("%s: %s", job["title"] or job["url"],
                                     describe_progress(job))
            time.sleep(0.5)
    except KeyboardInterrupt:
        manager.shutdown()
        logging.info(tr("остановлено. Скачанное сохранено: та же команда продолжит "
                        "загрузку с того же места"))
        return 130
    finally:
        status.stop()
    manager.shutdown()

    # о скачанном уже сказал сам менеджер, здесь — только итог
    jobs = [manager.job(job_id) for job_id in wanted]
    failed = [job for job in jobs if job["state"] != dl.DONE]
    logging.info(tr("скачано {done} из {total}", done=len(jobs) - len(failed), total=len(jobs)))
    return 1 if failed else 0


def show_info(args):
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    target = args.target.strip()
    try:
        if is_url(target):
            result = ytdlp.inspect(target, limit=args.limit)
        else:
            platform, channel = parse_channel(target)
            if platform is None:
                logging.error("%s", channel)
                return 2
            url = ytdlp.listing_url(platform, channel, args.kind, args.period)
            # списки, которых yt-dlp не знает (VK Video Live), площадка берёт сама
            lister = sites.own_lister(platform)
            if lister is not None:
                result = lister(channel, args.kind, args.limit or 30, 1, args.period)
            else:
                result = ytdlp.inspect(url, limit=args.limit)
    except (ValueError, ytdlp.YtdlpError) as error:
        logging.error("%s", error)
        return 1

    if result["type"] == "video":
        print(result["title"])
        rows = [(tr("канал"), result["channel"])]
        if result["timestamp"]:
            rows.append((tr("дата"), datetime.datetime.fromtimestamp(
                result["timestamp"]).strftime("%Y-%m-%d %H:%M")))
        if result["duration"]:
            rows.append((tr("длительность"), format_duration(result["duration"])))
        rows.append((tr("качество"), ", ".join("%s (-q %s)" % (option["label"], option["quality"])
                                               for option in result["qualities"])))
        # подписи в столбик — по самой длинной на текущем языке
        width = max(len(label) for label, _ in rows) + 1
        for label, value in rows:
            print("  %-*s %s" % (width, label + ":", value))
        return 0

    print(result["title"])
    if not result["entries"]:
        print("  " + tr("пусто"))
    for index, entry in enumerate(result["entries"], 1):
        date = (datetime.datetime.fromtimestamp(entry["timestamp"]).strftime("%Y-%m-%d")
                if entry["timestamp"] else "")
        print("%3d. %-10s %8s  %s" % (index, date, format_duration(entry["duration"]),
                                       entry["title"]))
        print("     %s" % entry["url"])
    return 0
