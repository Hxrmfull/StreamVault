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
"""

import argparse
import datetime
import logging
import os
import sys
import time

from . import __version__, sites, util
from .archive import chat
from .archive import downloads as dl
from .archive import ytdlp
from .live.recorder import ChannelRecorder
from .live.service import LiveService
from .logs import make_console_output_safe, setup_logging
from .platforms import PLATFORMS, is_url, parse_channel
from .settings import DEFAULT_SETTINGS_PATH, Settings, SettingsError, import_legacy_config

# Как часто консольная загрузка пишет прогресс, секунды.
PROGRESS_REPORT_INTERVAL = 5


def build_parser():
    parser = argparse.ArgumentParser(
        prog="streamvault",
        description="StreamVault %s: запись эфиров и скачивание записей и клипов "
                    "Twitch, Kick, YouTube, VK Video Live" % __version__)
    parser.add_argument("--settings", metavar="ФАЙЛ", default=DEFAULT_SETTINGS_PATH,
                        help="файл настроек (по умолчанию settings.json рядом с программой)")
    commands = parser.add_subparsers(dest="command", metavar="КОМАНДА")

    gui = commands.add_parser("gui", help="открыть окно программы (так же без команды)")
    gui.add_argument("--browser", action="store_true",
                     help="открыть в браузере, а не в отдельном окне")
    gui.add_argument("--port", type=int, default=0, help="порт для режима браузера")
    gui.add_argument("--no-open", action="store_true",
                     help="не открывать браузер самому, только напечатать адрес")
    gui.add_argument("--tray", action="store_true",
                     help="запуститься свёрнутым в трей, без окна")

    record = commands.add_parser("record", help="записывать эфиры, пока не нажат Ctrl+C")
    record.add_argument("-u", "--channel", dest="channel", action="append",
                        metavar="[ПЛАТФОРМА:]КАНАЛ",
                        help="например kick:xqc или legendaafk; можно повторять "
                             "и перечислять через запятую. Без префикса — twitch. "
                             "С этим ключом список каналов в настройках не меняется")
    record.add_argument("-q", "--quality", metavar="КАЧЕСТВО",
                        help="качество записи, например best или 720p")
    record.add_argument("-l", "--log", dest="log", default=None,
                        metavar="УРОВЕНЬ", help="debug, info, warning, error")
    record.add_argument("--disable-ffmpeg", action="store_true",
                        help="не конвертировать, просто переносить записи")

    download = commands.add_parser("download", help="скачать записи и клипы по ссылкам")
    download.add_argument("urls", nargs="+", metavar="ССЫЛКА")
    download.add_argument("-q", "--quality", metavar="КАЧЕСТВО",
                          help="best, worst, audio, высота кадра (1080, 720...) "
                               "или формат из команды info")
    download.add_argument("--section", metavar="НАЧАЛО-КОНЕЦ",
                          help="скачать только отрезок, например 1:00:00-1:30:00")
    download.add_argument("--force", action="store_true",
                          help="скачать заново, даже если уже скачано")
    download.add_argument("--chat", choices=chat.MODES, default=None,
                          help="чат записей и клипов Twitch через TwitchDownloader: none, "
                               "text (HTML и JSON) или render (ещё и видео чата); "
                               "по умолчанию — как в настройках")
    download.add_argument("-l", "--log", dest="log", default=None,
                          metavar="УРОВЕНЬ", help="debug, info, warning, error")

    info = commands.add_parser(
        "info", help="что лежит по ссылке или какие записи есть у канала")
    info.add_argument("target", metavar="ССЫЛКА|ПЛАТФОРМА:КАНАЛ")
    info.add_argument("--kind", default="vods",
                      help="vods, clips, highlights, uploads (Twitch), "
                           "videos, shorts (YouTube); по умолчанию vods")
    info.add_argument("--period", default="all", choices=ytdlp.CLIP_PERIODS,
                      help="за какой срок клипы Twitch: 24hr, 7d, 30d, all")
    info.add_argument("--limit", type=int, default=20, help="сколько записей показать")

    commands.add_parser("platforms", help="показать поддерживаемые платформы")

    importer = commands.add_parser(
        "import-config", help="перенести config.py старого рекордера в настройки")
    importer.add_argument("path", metavar="CONFIG_PY")
    importer.add_argument("--force", action="store_true",
                          help="перезаписать уже существующий файл настроек")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    make_console_output_safe()

    if args.command == "platforms":
        for name, spec in PLATFORMS.items():
            print("  %-12s %-38s опрос раз в %s с" % (name, spec["url"], spec["refresh"]))
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
        return True
    print("ошибка: StreamVault уже запущен — окном или другой консольной командой. "
          "Закрой его или добавь каналы и ссылки в нём", file=sys.stderr)
    return False


def load_settings_and_log(args, parser):
    """Настройки и лог для долгих команд. None — настройки не читаются."""
    try:
        settings = Settings.load(args.settings)
    except SettingsError as error:
        print("ошибка: %s" % error, file=sys.stderr)
        return None

    level_name = (args.log or settings["log_level"]).upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        parser.error("неизвестный уровень логирования: %s" % level_name)

    log_dir = os.path.join(os.path.dirname(os.path.abspath(settings.path)), "logs")
    setup_logging(level, os.path.join(log_dir, "streamvault.log"),
                  settings["log_max_bytes"], settings["log_backup_count"])
    return settings


def import_config(args):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if os.path.exists(args.settings) and not args.force:
        logging.error("%s уже есть. Чтобы перезаписать, добавь --force", args.settings)
        return 2
    try:
        settings = import_legacy_config(args.path, args.settings)
    except (OSError, SyntaxError) as error:
        logging.error("не удалось прочитать %s: %s", args.path, error)
        return 2
    channels = settings["channels"]
    logging.info("настройки сохранены в %s", settings.path)
    logging.info("каналов: %s%s", len(channels),
                 " (%s)" % ", ".join("%s:%s" % (c["platform"], c["channel"])
                                     for c in channels) if channels else "")
    if settings["twitch_client_id"]:
        logging.info("ключи Twitch перенесены")
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
        logging.critical("не задан ни один канал: добавь каналы в %s (channels) "
                         "или передай -u", settings.path)
        return 2
    problems = service.preflight()
    if problems:
        for problem in problems:
            logging.critical("%s", problem)
        return 2

    service.start()
    try:
        while service.is_running():
            time.sleep(0.5)
    except KeyboardInterrupt:
        logging.info("остановка по Ctrl+C")

    try:
        # Ctrl+C дошёл и до streamlink: даём ему закрыть файл самому
        left = service.shutdown(grace=ChannelRecorder.STOP_GRACE)
    except KeyboardInterrupt:
        # второй Ctrl+C, пока доделывается обработка: выходим без трассировки.
        # Недописанный mp4 останется с расширением .part и будет убран сам.
        logging.warning("повторный Ctrl+C — выхожу, не дожидаясь обработки. "
                        "Необработанные записи подхватятся при следующем запуске")
        return 130

    if left:
        logging.info("осталось необработанных записей: %s. "
                     "Они подхватятся при следующем запуске", left)
    logging.info("завершено")
    return 0


def format_size(value):
    if not value:
        return "?"
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return ("%.0f %s" if unit == "Б" else "%.1f %s") % (value, unit)
        value /= 1024.0


def format_duration(seconds):
    if seconds is None:
        return ""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return ("%d:%02d:%02d" % (hours, rest // 60, rest % 60) if hours
            else "%d:%02d" % (rest // 60, rest % 60))


def describe_progress(job):
    stage = job.get("chat_progress")
    if job["state"] == dl.PROCESSING and stage:
        # видео уже скачано — идёт чат
        percent = stage.get("percent")
        return "чат" + (" %d%%" % percent if percent is not None else "")
    progress = job.get("progress") or {}
    parts = []
    if progress.get("percent") is not None:
        parts.append("%.0f%%" % progress["percent"])
    if progress.get("downloaded"):
        parts.append("%s из %s" % (format_size(progress["downloaded"]),
                                   format_size(progress.get("total"))))
    if progress.get("speed"):
        parts.append("%s/с" % format_size(progress["speed"]))
    if progress.get("eta") is not None:
        parts.append("осталось %s" % format_duration(progress["eta"]))
    if job["state"] == dl.PROCESSING:
        parts.append("склейка")
    return ", ".join(parts) or "начинаю"


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
            logging.info("уже скачано: %s (заново — с --force)", job["file"])
        else:
            wanted.append(job_id)
    if not wanted:
        return 0

    manager.start()
    finished = (dl.DONE, dl.FAILED, dl.CANCELLED, dl.PAUSED)
    reported = 0.0
    try:
        while True:
            jobs = [manager.job(job_id) for job_id in wanted]
            if all(job["state"] in finished for job in jobs):
                break
            if time.monotonic() - reported >= PROGRESS_REPORT_INTERVAL:
                reported = time.monotonic()
                for job in jobs:
                    if job["state"] in dl.ACTIVE:
                        logging.info("%s: %s", job["title"] or job["url"],
                                     describe_progress(job))
            time.sleep(0.5)
    except KeyboardInterrupt:
        manager.shutdown()
        logging.info("остановлено. Скачанное сохранено: та же команда продолжит "
                     "загрузку с того же места")
        return 130
    manager.shutdown()

    # о скачанном уже сказал сам менеджер, здесь — только итог
    jobs = [manager.job(job_id) for job_id in wanted]
    failed = [job for job in jobs if job["state"] != dl.DONE]
    logging.info("скачано %s из %s", len(jobs) - len(failed), len(jobs))
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
        print("  канал:        %s" % result["channel"])
        if result["timestamp"]:
            print("  дата:         %s" % datetime.datetime.fromtimestamp(
                result["timestamp"]).strftime("%Y-%m-%d %H:%M"))
        if result["duration"]:
            print("  длительность: %s" % format_duration(result["duration"]))
        print("  качество:     %s" % ", ".join(
            "%s (-q %s)" % (option["label"], option["quality"])
            for option in result["qualities"]))
        return 0

    print(result["title"])
    if not result["entries"]:
        print("  пусто")
    for index, entry in enumerate(result["entries"], 1):
        date = (datetime.datetime.fromtimestamp(entry["timestamp"]).strftime("%Y-%m-%d")
                if entry["timestamp"] else "")
        print("%3d. %-10s %8s  %s" % (index, date, format_duration(entry["duration"]),
                                       entry["title"]))
        print("     %s" % entry["url"])
    return 0
