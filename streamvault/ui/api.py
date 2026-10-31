# -*- coding: utf-8 -*-
"""
Что окно программы может попросить у движков.

Окно вызывает один метод — call(имя, аргументы), и сюда доходят только имена
из списка METHODS. Всё, что приходит из окна, проверяется: заголовки стримов
приходят с чужих сайтов, и если бы через них в окно пробрался скрипт, он не
должен суметь открыть произвольный файл на диске или переписать настройки.
"""

import collections
import logging
import os
import re
import shlex
import secrets
import shutil
import subprocess
import threading
import time
import webbrowser

from .. import __version__, i18n, sites, util
from ..archive import downloads as dl
from ..archive import chat, ytdlp
from ..chatreplay import ChatReplay, LiveChatReplay
from ..ffmpeg import read_version
from ..library import broadcast_parts, probe_duration
from ..live.livechat import jsonl_path, video_backlog
from ..positions import Positions
from ..i18n import tr
from ..platforms import PLATFORMS, channel_id, channel_option_value, is_url, parse_channel
from ..search import ChannelSearch, SearchError
from .media import MediaServer

# Что окно может открыть проигрывателем.
MEDIA_EXTENSIONS = (".mp4", ".ts", ".m4a", ".mkv", ".webm", ".mp3", ".chat.html")
# Сколько открытых чатов держать в памяти: этот и предыдущий.
OPEN_CHATS = 2

# Настройки, которые меняет окно. Каналы меняются отдельными вызовами.
EDITABLE = ("language", "root_path", "quality", "twitch_client_id", "twitch_client_secret",
            "refresh", "probe_timeout", "ffmpeg_path", "downloads_path",
            "download_quality", "max_downloads", "download_fragments",
            "twitch_auth_token", "ytdlp_extra_args", "log_level",
            "close_to_tray", "tray_notifications",
            "chat_mode", "chat_width", "chat_height", "chat_font_size", "check_updates",
            "live_chat", "convert_recordings", "mp4_faststart",
            "record_from_start", "join_beginning", "min_free_gb", "keep_days",
            "split_mode", "split_size_gb", "split_minutes", "split_count")

# Да/нет из окна приходят как true/false.
FLAGS = ("close_to_tray", "tray_notifications", "check_updates", "convert_recordings", "mp4_faststart",
         "record_from_start", "join_beginning")

# Секреты окну не отдаются: оно узнаёт только, заданы ли они.
SECRETS = ("twitch_client_secret", "twitch_auth_token")

# Подхватываются только при следующем запуске: на них построены опросчики и потоки.
NEED_RESTART = ("root_path", "twitch_client_id", "twitch_client_secret", "refresh",
                "probe_timeout")

# Пределы числовых настроек: (наименьшее, наибольшее).
LIMITS = {"max_downloads": (1, 10), "download_fragments": (1, 16),
          "probe_timeout": (30, 900), "min_free_gb": (0, 1000), "keep_days": (0, 3650),
          "split_minutes": (5, 1440), "split_count": (2, 100),
          "chat_width": (200, 1920), "chat_height": (200, 4320), "chat_font_size": (8, 72)}

LOG_LEVELS = ("debug", "info", "warning", "error")
# Как делить запись на куски при конвертации (live/processing.py: split_points).
SPLIT_MODES = ("off", "size", "duration", "count")

# Какие сведения о видео окно может передать вместе со ссылкой.
META_TEXT = ("id", "title", "channel", "folder", "platform", "kind", "thumbnail", "live_status")
META_NUMBERS = ("duration", "timestamp")


def split_args(text):
    """Строка аргументов yt-dlp -> список. Обратные слэши путей Windows не трогаются."""
    parts = shlex.split(text, posix=False)
    return [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in "\"'"
            else part for part in parts]


class Api:
    """Методы для окна. Ошибки для человека — ValueError с переведённым текстом."""

    METHODS = (
        "init", "live_snapshot", "resolve", "profiles", "search_channels",
        "add_channel", "remove_channel", "set_enabled", "set_quality", "check_now",
        "record_now", "stop_recording", "check_channel", "set_keep_days", "set_channel_options", "disk_status",
        "inspect", "list_channel", "download_all",
        "add_download", "pause_download", "resume_download", "cancel_download",
        "remove_download", "clear_finished", "downloads_snapshot",
        "open_file", "show_in_folder", "open_folder", "open_url", "watch", "chat_replay",
        "chat_search", "save_position", "cut_clip", "overlay_chat", "cancel_edit",
        "watch_live", "live_status", "chat_activity", "storyboard", "check_recording",
        "search_chats", "broadcast_summary",
        "library", "library_preview", "delete_file",
        "ffmpeg_status", "install_ffmpeg", "cancel_ffmpeg_install", "choose_ffmpeg",
        "check_ffmpeg", "ytdlp_status", "update_ytdlp", "cancel_ytdlp_update",
        "twitchdownloader_status", "install_twitchdownloader", "cancel_twitchdownloader_install",
        "download_chat", "convert_recording",
        "app_update_status", "check_app_update", "install_app_update", "cancel_app_update",
        "apply_app_update",
        "get_settings", "save_settings", "choose_folder", "quit",
    )

    def __init__(self, settings, live, downloads, logs=None, problems=(), library=None,
                 autostart=None, tray=False, ffmpeg=None, installer=None, ytdlp_copy=None,
                 ytdlp_updater=None, twitchdownloader=None, twitchdownloader_installer=None,
                 app_updater=None, last_update=None, positions=None, editor=None,
                 integrity=None, checker=None, storyboards=None, chat_index=None):
        """
        library — Library; autostart — модуль запуска вместе с Windows (или
        заглушка в тестах); tray — есть ли значок в трее; ffmpeg — FFmpegLocator;
        installer — FFmpegInstaller; ytdlp_copy — YtdlpLocator; ytdlp_updater —
        YtdlpUpdater; twitchdownloader — TwitchDownloaderLocator и его установщик;
        app_updater — AppUpdater; last_update — итог прошлого обновления программы;
        positions — где остановился просмотр записей (Positions); editor —
        правка записей: клип и видео с чатом поверх (editing.Editor);
        integrity — итоги проверок записей на пропуски (IntegrityStore),
        checker — проверка по просьбе окна (IntegrityChecker); storyboards —
        кадры для шкалы проигрывателя; chat_index — поиск по чатам всех
        записей и сводка эфира (ChatIndex).
        """
        self._settings = settings
        self._live = live
        self._downloads = downloads
        self._search = None      # ChannelSearch: создаётся при первом поиске
        self._profiles = None    # sites.Profiles: создаётся при первом вопросе
        self._logs = logs
        self._problems = list(problems)
        self._library = library
        self._autostart = autostart
        self._tray = tray
        self._ffmpeg = ffmpeg
        self._installer = installer
        self._ytdlp = ytdlp_copy
        self._ytdlp_updater = ytdlp_updater
        self._twitchdownloader = twitchdownloader
        self._twitchdownloader_installer = twitchdownloader_installer
        self._app_updater = app_updater
        self._last_update = last_update
        self._positions = positions or Positions()
        self._editor = editor
        self._integrity = integrity
        self._checker = checker
        self._storyboards = storyboards
        self._chat_index = chat_index
        self._window = None
        self.on_quit = None
        # проигрыватель в окне: сервер видео и картинок чата, открытые чаты
        self._media_server = MediaServer(images=self._chat_image,
                                         ffmpeg=lambda: self._ffmpeg.path() if self._ffmpeg else None)
        self._chats = collections.OrderedDict()     # id -> (путь, mtime, ChatReplay)
        self._chats_lock = threading.Lock()

    def attach_window(self, window):
        """Окно pywebview: нужно для выбора папки и файла."""
        self._window = window

    # ------------------------------------------------------------ вызов из окна

    def call(self, name, args=None):
        """Единственная точка входа из окна. Ответ — {"ok", "result"} или {"ok", "error"}."""
        try:
            return {"ok": True, "result": self.dispatch(str(name), args or [])}
        except (ValueError, KeyError, OSError, ytdlp.YtdlpError) as error:
            if isinstance(error, KeyError) and error.args:
                message = str(error.args[0])
            else:
                message = str(error)
            return {"ok": False, "error": message}
        except Exception as error:
            logging.exception(tr("вызов {name} из окна упал", name=name))
            return {"ok": False, "error": tr("внутренняя ошибка: {error}", error=error)}

    def dispatch(self, name, args):
        if name not in self.METHODS:
            raise ValueError(tr("неизвестный вызов: {name}", name=name))
        if not isinstance(args, (list, tuple)):
            raise ValueError(tr("аргументы вызова — список"))
        return getattr(self, name)(*args)

    def busy(self):
        """Идёт ли запись, загрузка или правка: тогда закрытие окна стоит подтвердить."""
        recording = any(channel["state"] == "recording"
                        for channel in self._live.snapshot()["channels"])
        downloading = any(job["state"] in (dl.DOWNLOADING, dl.PROCESSING, dl.QUEUED)
                          for job in self._downloads.snapshot()["jobs"])
        editing = any(job["state"] in ("queued", "running")
                      for job in (self._editor.snapshot() if self._editor else []))
        return recording or downloading or editing

    # ------------------------------------------------------------ общее

    def init(self):
        """Всё, что нужно для первой отрисовки окна."""
        return {
            "version": __version__,
            "language": i18n.current_language(),
            "languages": i18n.LANGUAGE_NAMES,
            "platforms": list(PLATFORMS),
            # возможности площадок: окно ничего не решает о площадках само
            "sites": sites.for_window(),
            "clip_periods": list(ytdlp.CLIP_PERIODS),
            "live": self._live.snapshot(),
            "disk": self.disk_status(),
            "edits": self._editor.snapshot() if self._editor else [],
            "downloads": self._downloads.snapshot(),
            "settings": self.get_settings(),
            "logs": self._logs.lines() if self._logs else [],
            "problems": self._problems,
            "ffmpeg": self.ffmpeg_status(),
            "ytdlp": self.ytdlp_status(),
            "twitchdownloader": self.twitchdownloader_status(),
            "app_update": self.app_update_status(),
            "last_update": self._last_update,
            "can_choose_folder": self._window is not None,
            "window": self._window is not None,
            "tray": self._tray,
        }

    def live_snapshot(self):
        return self._live.snapshot()

    def downloads_snapshot(self):
        return self._downloads.snapshot()

    def resolve(self, text):
        """
        Что ввели в строку поиска: ссылку на видео или список, или канал
        ("kick:xqc", "legendaafk", ссылка на канал).
        """
        text = str(text).strip()
        if not text:
            raise ValueError(tr("вставь ссылку или имя канала"))
        platform, channel = parse_channel(text)
        if platform is not None and not is_url(channel):
            return {"kind": "channel", "id": channel_id(platform, channel),
                    "platform": platform, "channel": channel}
        if is_url(text):
            return {"kind": "url", "url": text}
        raise ValueError(channel)

    def search_channels(self, platform, query):
        """
        Каналы платформы по имени — для выдачи под строкой поиска.
        Платформа не ответила — пустой список и причина, а не ошибка:
        выдача остальных платформ от этого не страдает.
        """
        if self._search is None:
            self._search = ChannelSearch(tools=self._downloads)
        try:
            return {"results": self._search.search(str(platform), str(query)), "error": ""}
        except SearchError as error:
            return {"results": [], "error": str(error)}

    def profiles(self, ids):
        """
        Имена и аватарки каналов всех площадок: {id: {"name", "avatar"}}.
        Twitch — пачкой (с ключами через Helix), остальные — открытыми
        запросами площадок параллельно. Найденное для отслеживаемого канала
        сохраняется с ним, как профиль из выдачи поиска.
        """
        logins, others = [], []
        for cid in ids or []:
            platform, _, channel = str(cid).partition(":")
            if not channel or platform not in sites.SITES:
                continue
            if platform == "twitch":
                if not is_url(channel):
                    logins.append(channel)
            else:
                others.append((platform, channel))
        result = {channel_id("twitch", login): profile
                  for login, profile in self._live.twitch_profiles(logins).items()} if logins else {}
        if others:
            if self._profiles is None:
                self._profiles = sites.Profiles(tools=self._downloads)
            for (platform, channel), profile in self._profiles.profiles(others).items():
                cid = channel_id(platform, channel)
                result[cid] = profile
                self._live.remember_profile(cid, profile)
        return result

    # ------------------------------------------------------------ каналы

    def add_channel(self, text, quality=None, autorecord=False, profile=None):
        """profile — имя и аватарка канала из выдачи поиска: сохраняются с каналом."""
        return self._live.add_channel(str(text), quality=quality or None, enabled=autorecord is True,
                                      profile=profile if isinstance(profile, dict) else None)

    def record_now(self, cid):
        """Записать идущий эфир сейчас; канал может быть и не из списка."""
        return self._live.record_now(str(cid))

    def stop_recording(self, cid):
        self._live.stop_recording(str(cid))

    def check_channel(self, cid):
        """В эфире ли канал не из списка — для его страницы."""
        return self._live.check_channel(str(cid))

    def remove_channel(self, cid):
        self._live.remove_channel(str(cid))

    def set_enabled(self, cid, enabled):
        self._live.set_enabled(str(cid), bool(enabled))

    def set_quality(self, cid, quality=None):
        self._live.set_quality(str(cid), quality or None)

    def check_now(self, cid=None):
        self._live.check_now(str(cid) if cid else None)

    def set_keep_days(self, cid, days=None):
        """Свой срок хранения записей канала; None — как в общих настройках."""
        if days is not None and (isinstance(days, bool) or not isinstance(days, (int, float))
                                 or not 0 <= days <= LIMITS["keep_days"][1]):
            raise ValueError(tr("{key} — от {low} до {high}", key="keep_days", low=0,
                                high=LIMITS["keep_days"][1]))
        self._live.set_keep_days(str(cid), None if days is None else int(days))

    def set_channel_options(self, cid, changes):
        """
        Свои настройки записи канала: {ключ: значение}; None — как в общих
        настройках. Ключи — platforms.CHANNEL_OPTIONS.
        """
        if not isinstance(changes, dict):
            raise ValueError(tr("настройки — объект"))
        clean = {key: None if value is None else channel_option_value(key, value)
                 for key, value in changes.items()}
        self._live.set_options(str(cid), clean)

    def disk_status(self):
        """Хватает ли места на диске с записями: уровень ok, low или critical."""
        return self._live.disk_status()

    # ------------------------------------------------------------ записи и клипы

    def inspect(self, url, limit=None, start=1):
        url = str(url).strip()
        if not is_url(url):
            raise ValueError(tr("нужна ссылка, начинающаяся с https://"))
        return self._downloads.inspect(url, limit=int(limit) if limit else None,
                                       start=max(1, int(start)))

    def list_channel(self, platform, channel, kind="vods", start=1, limit=30, period="all"):
        return self._downloads.list_channel(str(platform), str(channel), str(kind),
                                            limit=max(1, min(int(limit), 100)),
                                            start=max(1, int(start)), period=str(period))

    def download_all(self, platform, channel, kind="vods", period="all", quality=None, chat_mode=None):
        """
        Всё с вкладки канала. Уже скачанное и стоящее в очереди пропускается:
        повторное нажатие докачивает только новое.
        """
        if chat_mode is not None and chat_mode not in chat.MODES:
            raise ValueError(tr("чат: {modes}", modes=", ".join(chat.MODES)))
        return self._downloads.download_all(str(platform), str(channel), str(kind), str(period),
                                            quality=quality or None, chat_mode=chat_mode)

    def add_download(self, url, quality=None, section=None, meta=None, force=False, chat_mode=None):
        url = str(url).strip()
        if not is_url(url):
            raise ValueError(tr("нужна ссылка, начинающаяся с https://"))
        if section:
            start, end = (ytdlp.parse_time(str(value)) for value in section)
            if end <= start:
                raise ValueError(tr("конец отрезка должен быть позже начала"))
            section = (start, end)
        if chat_mode is not None and chat_mode not in chat.MODES:
            raise ValueError(tr("чат: {modes}", modes=", ".join(chat.MODES)))
        return self._downloads.add(url, quality=quality or None, section=section or None,
                                   meta=self._clean_meta(meta), force=bool(force),
                                   chat_mode=chat_mode)

    def download_chat(self, job_id, mode=None):
        """Чат к уже скачанной записи или клипу."""
        self._downloads.fetch_chat(str(job_id), mode if mode in chat.MODES else None)

    @staticmethod
    def _clean_meta(meta):
        """Из сведений о видео — только известные поля нужного типа."""
        if not isinstance(meta, dict):
            return None
        clean = {}
        for key in META_TEXT:
            if isinstance(meta.get(key), str):
                clean[key] = meta[key][:500]
        for key in META_NUMBERS:
            value = meta.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
        return clean or None

    def pause_download(self, job_id):
        self._downloads.pause(str(job_id))

    def resume_download(self, job_id):
        self._downloads.resume(str(job_id))

    def cancel_download(self, job_id):
        self._downloads.cancel(str(job_id))

    def remove_download(self, job_id):
        self._downloads.remove(str(job_id))

    def clear_finished(self):
        self._downloads.clear_finished()

    # ------------------------------------------------------------ файлы

    def _folders(self):
        root = self._settings["root_path"]
        return {
            "recordings": os.path.join(root, util.FINISHED_DIR),
            "downloads": self._downloads.downloads_dir(),
            "logs": os.path.join(os.path.dirname(os.path.abspath(self._settings.path)), "logs"),
        }

    def _media(self, path, exists=True):
        """
        Путь к записи или загрузке. Всё прочее окно открыть не может.
        exists=False — файла может уже не быть (часть идущей записи ушла
        в конвертацию), но путь всё равно обязан быть внутри папок программы.
        """
        path = os.path.abspath(str(path))
        roots = [os.path.abspath(self._settings["root_path"]),
                 os.path.abspath(self._downloads.downloads_dir())]
        inside = False
        for root in roots:
            try:
                inside = inside or os.path.commonpath([path, root]) == root
            except ValueError:
                pass   # другой диск
        if not inside or not path.lower().endswith(MEDIA_EXTENSIONS) \
                or (exists and not os.path.isfile(path)):
            raise ValueError(tr("этот файл программа не открывает: {path}", path=path))
        return path

    def open_file(self, path):
        os.startfile(self._media(path))

    # ------------------------------------------------------------ просмотр с чатом

    def watch(self, path):
        """
        Запись для проигрывателя в окне: адрес видео и чат рядом с ней.
        chat — None, если чата нет; иначе id для chat_replay, число сообщений
        и адреса картинок смайлов и значков (номер — как в сообщениях).
        remux — запись в .ts, которую ffmpeg перепаковывает на лету: место
        задаётся параметром ?start=, длительность — duration. position — где
        просмотр остановился в прошлый раз (None — с начала). parts — части
        того же эфира по порядку: [{"part", "path", "name"}].
        """
        path = self._media(path)
        if path.lower().endswith(".chat.html"):
            raise ValueError(tr("этот файл программа не открывает: {path}", path=path))
        result = {"video": self._media_server.file_url(path), "chat": None, "remux": False, "duration": None,
                  "position": self._positions.get(path), "parts": self._parts(path), "gaps": self._gaps(path)}
        if path.lower().endswith(".ts") and self._ffmpeg is not None and self._ffmpeg.path():
            # проверка знает длительность без склеенных пропусков — она точнее заголовка .ts
            checked = self._integrity.get(path) if self._integrity is not None else None
            duration = (checked or {}).get("duration") or (
                self._library.preview(path).get("duration") if self._library is not None else None)
            if duration:
                result.update(video=self._media_server.remux_url(path), remux=True, duration=duration)
        json_path = chat.chat_paths(path)["json"]
        if os.path.isfile(json_path):
            chat_id, replay = self._open_chat(json_path)
            result["chat"] = {
                "id": chat_id, "count": len(replay.messages),
                "images": [image if image.startswith("https://") else self._media_server.image_url(chat_id, index)
                           for index, image in enumerate(replay.images)],
            }
        return result

    def _gaps(self, path):
        """Пропуски в записи по прошлой проверке; None — не проверялась."""
        checked = self._integrity.get(path) if self._integrity is not None else None
        return checked["gaps"] if checked else None

    def chat_activity(self, chat_id, duration):
        """График открытого чата: сколько сообщений в каждом отрезке записи и где всплески."""
        with self._chats_lock:
            opened = self._chats.get(str(chat_id))
        if opened is None:
            raise ValueError(tr("чат закрыт — открой запись заново"))
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise ValueError(tr("длительность — число секунд"))
        return opened[2].activity(float(duration))

    def storyboard(self, path):
        """
        Кадры для шкалы: {"state": "ready", "interval", "columns", "rows",
        "sheets": [адреса листов]}; "working" — делаются, спросить позже;
        "none" — не будет (нет ffmpeg, у файла только звук).
        """
        path = self._media(path)
        if self._storyboards is None:
            return {"state": "none"}
        duration = self._library.preview(path).get("duration") if self._library is not None else None
        board = self._storyboards.get(path, duration)
        if board["state"] == "ready":
            board["sheets"] = [self._media_server.file_url(sheet) for sheet in board["sheets"]]
        return board

    def check_recording(self, path):
        """
        Проверить запись на пропуски — в своём потоке; итог придёт событием
        "integrity". Уже проверенная — сразу: {"gaps": [...]}.
        """
        path = self._media(path)
        if self._checker is None or self._ffmpeg is None or not self._ffmpeg.path():
            raise ValueError(tr("проверка записей — с ffmpeg: его можно скачать в настройках"))
        gaps = self._gaps(path)
        if gaps is not None:
            return {"gaps": gaps}
        self._checker.check(path)
        return {"gaps": None}

    def _need_chat_index(self):
        if self._chat_index is None:
            raise ValueError(tr("поиск по чатам здесь недоступен"))
        return self._chat_index

    def search_chats(self, text, by="text"):
        """
        Сообщения со словом во всех чатах библиотеки (by="name" — по нику).
        pending — сколько чатов ещё не просмотрено: окно спросит ещё раз.
        """
        if by not in ("text", "name"):
            raise ValueError(tr("искать можно в тексте или по нику"))
        return self._need_chat_index().search(str(text)[:100], by)

    def broadcast_summary(self, path):
        """
        Сводка эфира по всем его частям: чат (сообщений, зрителей, самые
        активные, смайлы, всплески — с частью и временем) и пропуски каждой части.
        """
        path = self._media(path)
        parts = [part["path"] for part in self._parts(path)]
        durations = [self._library.preview(part).get("duration") if self._library is not None else None
                     for part in parts]
        chats = [chat.chat_paths(part)["json"] for part in parts]
        result = self._need_chat_index().summary([json for json in chats if os.path.isfile(json)],
                                                 [duration for json, duration in zip(chats, durations)
                                                  if os.path.isfile(json)])
        # номер части в сводке чата — среди частей с чатом; окну нужен номер среди всех
        with_chat = [index for index, json in enumerate(chats) if os.path.isfile(json)]
        for moment in result["moments"]:
            moment["part"] = with_chat[moment["part"]]
        messages = dict(zip(with_chat, (part["messages"] for part in result["parts"])))
        result["parts"] = [{"path": part, "duration": duration, "size": os.path.getsize(part),
                            "chat": os.path.isfile(json), "messages": messages.get(index, 0),
                            "gaps": self._gaps(part)}
                           for index, (part, duration, json) in enumerate(zip(parts, durations, chats))]
        return result

    # ------------------------------------------------------------ идущая запись

    def watch_live(self, cid):
        """
        Запись канала, которая идёт прямо сейчас, — для проигрывателя: её часть
        растёт, и поток идёт за ней (ffmpeg читает файл по мере записи), чат —
        из .chat.jsonl рядом, тоже по мере записи. item — сведения для шапки
        проигрывателя, как у записи из библиотеки.
        """
        cid = str(cid)
        snapshot = self._live.channel_snapshot(cid)
        recording = snapshot.get("recording") or {}
        if snapshot.get("state") != "recording" or not recording.get("file"):
            raise ValueError(tr("канал сейчас не записывается"))
        path = self._media(recording["file"])
        if self._ffmpeg is None or not self._ffmpeg.path():
            raise ValueError(tr("смотреть идущую запись можно с ffmpeg — его можно скачать в настройках"))
        status = self.live_status(cid, path)
        stream = snapshot.get("stream") or {}
        result = {
            "video": self._media_server.remux_url(path, follow=True), "remux": True, "live": True,
            "duration": status["duration"] or 0, "position": None, "chat": None,
            "parts": [{"part": recording.get("part") or 1, "path": path, "name": os.path.basename(path)}],
            "item": {"path": path, "name": os.path.basename(path), "kind": "record", "video_kind": "stream",
                     "title": stream.get("title") or "", "channel_id": cid,
                     "channel": snapshot.get("label") or snapshot.get("channel") or "",
                     "platform": snapshot.get("platform") or "", "timestamp": recording.get("started"),
                     "part": recording.get("part") or 1, "chat": {}},
        }
        chat_file = jsonl_path(path)
        if os.path.isfile(chat_file):
            chat_id, replay = self._open_live_chat(chat_file)
            result["chat"] = {"id": chat_id, "count": replay.count, "images": [], "live": True}
        return result

    def live_status(self, cid, path):
        """
        Как идёт запись, которую смотрят: duration — сколько уже записано,
        growing — эта часть ещё пишется, next — эфир идёт дальше новой частью.
        Кончившаяся часть уходит в конвертацию, и её .ts может уже не быть.
        """
        path = self._media(path, exists=False)
        try:
            snapshot = self._live.channel_snapshot(str(cid))
        except KeyError:
            snapshot = {}
        recording = snapshot.get("recording") or {}
        current = recording.get("file") if snapshot.get("state") == "recording" else None
        same = bool(current) and os.path.normcase(os.path.abspath(current)) == os.path.normcase(path)
        duration = (probe_duration(self._ffmpeg.path() if self._ffmpeg else None, path)
                    if os.path.isfile(path) else None)
        # запись взяла эфир с начала: видео длиннее, чем идёт чат, — чат сдвигается.
        # Пока streamlink догоняет эфир, оценка растёт; берётся наибольшая
        with self._chats_lock:
            opened = [entry[2] for entry in self._chats.values() if entry[0] == jsonl_path(path)]
        for replay in opened:
            if same and replay.start is not None:
                replay.shift = max(replay.shift, video_backlog(duration, replay.start, time.time()))
        return {"duration": duration, "growing": same, "next": bool(current) and not same}

    def _open_live_chat(self, chat_file):
        """Чат идущей записи — один на файл: он дочитывается, а не открывается заново."""
        with self._chats_lock:
            for chat_id, (path, _, replay) in self._chats.items():
                if path == chat_file and isinstance(replay, LiveChatReplay):
                    self._chats.move_to_end(chat_id)
                    return chat_id, replay
        replay = LiveChatReplay(chat_file)
        chat_id = secrets.token_urlsafe(9)
        with self._chats_lock:
            self._chats[chat_id] = (chat_file, None, replay)
            while len(self._chats) > OPEN_CHATS:
                self._chats.popitem(last=False)
        return chat_id, replay

    def _parts(self, path):
        """Части эфира записи; у скачанного частей не бывает."""
        recordings = os.path.abspath(os.path.join(self._settings["root_path"], util.FINISHED_DIR))
        try:
            inside = os.path.commonpath([path, recordings]) == recordings
        except ValueError:
            inside = False
        if not inside:
            return [{"part": 1, "path": path, "name": os.path.basename(path)}]
        return [{"part": number, "path": part, "name": os.path.basename(part)}
                for number, part in broadcast_parts(path)]

    def save_position(self, path, seconds, duration=None):
        """Где остановился просмотр: окно сообщает его по ходу и при закрытии."""
        path = self._media(path)
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) \
                or (duration is not None and (not isinstance(duration, (int, float)) or isinstance(duration, bool))):
            raise ValueError(tr("позиция просмотра — число секунд"))
        self._positions.save(path, seconds, duration)

    def _need_editor(self):
        if self._editor is None:
            raise ValueError(tr("правка записей здесь недоступна"))
        return self._editor

    def cut_clip(self, path, start, end, with_chat=False):
        """Клип отрезка записи: start и end — секунды записи. Возвращает id правки."""
        path = self._media(path)
        for value in (start, end):
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(tr("начало и конец клипа — число секунд"))
        return self._need_editor().cut(path, start, end, with_chat is True)

    def overlay_chat(self, path, side="right"):
        """Видео чата поверх записи — новым файлом рядом. Возвращает id правки."""
        return self._need_editor().overlay(self._media(path), str(side))

    def cancel_edit(self, job_id):
        self._need_editor().cancel(str(job_id))

    def chat_search(self, chat_id, text):
        """Сообщения открытого чата, где есть text: для перехода к ним."""
        with self._chats_lock:
            opened = self._chats.get(str(chat_id))
        if opened is None:
            raise ValueError(tr("чат закрыт — открой запись заново"))
        return opened[2].search(str(text)[:100])

    def chat_replay(self, chat_id, start, end):
        """Сообщения открытого чата со временем в записи от start до end, секунды."""
        with self._chats_lock:
            opened = self._chats.get(str(chat_id))
        if opened is None:
            raise ValueError(tr("чат закрыт — открой запись заново"))
        return opened[2].window(float(start), float(end))

    def _open_chat(self, json_path):
        mtime = os.path.getmtime(json_path)
        with self._chats_lock:
            for chat_id, (path, known, replay) in self._chats.items():
                if path == json_path and known == mtime:
                    self._chats.move_to_end(chat_id)
                    return chat_id, replay
        try:
            replay = ChatReplay(json_path)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(tr("чат не читается: {error}", error=error))
        chat_id = secrets.token_urlsafe(9)
        with self._chats_lock:
            self._chats[chat_id] = (json_path, mtime, replay)
            while len(self._chats) > OPEN_CHATS:
                self._chats.popitem(last=False)
        return chat_id, replay

    def _chat_image(self, chat_id, index):
        with self._chats_lock:
            opened = self._chats.get(chat_id)
        return opened[2].image(index) if opened else None

    def show_in_folder(self, path):
        path = self._media(path)
        # explorer понимает выделение файла только в такой записи
        subprocess.Popen('explorer /select,"%s"' % path)

    def open_folder(self, which):
        folders = self._folders()
        if which not in folders:
            raise ValueError(tr("нет такой папки: {name}", name=which))
        os.makedirs(folders[which], exist_ok=True)
        os.startfile(folders[which])

    def open_url(self, url):
        url = str(url)
        if not re.match(r"^https?://", url, re.I):
            raise ValueError(tr("открываются только ссылки http и https"))
        webbrowser.open(url)

    # ------------------------------------------------------------ библиотека

    def _need_library(self):
        if self._library is None:
            raise ValueError(tr("библиотека недоступна"))
        return self._library

    def library(self):
        items = self._need_library().items()
        return {"items": items, "folders": self._library.folders(),
                "size": sum(item["size"] for item in items), "free": self._free_space()}

    def _free_space(self):
        """Сколько свободно на дисках записей и загрузок, байт (None — не узнать)."""
        free = {}
        for name, folder in (("recordings", self._settings["root_path"]),
                             ("downloads", self._downloads.downloads_dir())):
            # папки загрузок может ещё не быть — смотрим ближайшую существующую
            while folder and not os.path.isdir(folder) and os.path.dirname(folder) != folder:
                folder = os.path.dirname(folder)
            try:
                free[name] = shutil.disk_usage(folder).free
            except (OSError, ValueError):
                free[name] = None
        return free

    def library_preview(self, path):
        return self._need_library().preview(self._media(path))

    def convert_recording(self, path):
        """Запись эфира в .ts — в mp4 (если автоконвертация была выключена)."""
        self._live.convert_recording(self._media(path))

    def delete_file(self, path):
        self._need_library().delete(self._media(path))

    # ------------------------------------------------------------ ffmpeg

    def ffmpeg_status(self):
        """Где ffmpeg, какой версии, своя ли это копия и как идёт установка."""
        if self._ffmpeg is None:
            return {"path": None, "version": None, "own": False, "install": None,
                    "configured": self._settings["ffmpeg_path"]}
        status = self._ffmpeg.status()
        status["configured"] = self._settings["ffmpeg_path"]
        status["install"] = self._installer.snapshot() if self._installer else None
        return status

    def install_ffmpeg(self):
        if self._installer is None:
            raise ValueError(tr("установка ffmpeg здесь недоступна"))
        self._installer.start()
        return self._installer.snapshot()

    def cancel_ffmpeg_install(self):
        if self._installer is not None:
            self._installer.cancel()

    # ------------------------------------------------------------ yt-dlp

    def ytdlp_status(self):
        """Какой yt-dlp сейчас запускается и как идёт обновление."""
        status = self._ytdlp.status() if self._ytdlp else {"version": None, "own": False,
                                                            "bundled": None}
        status["install"] = self._ytdlp_updater.snapshot() if self._ytdlp_updater else None
        status["frozen"] = util.FROZEN
        return status

    def update_ytdlp(self):
        if self._ytdlp_updater is None:
            raise ValueError(tr("обновление yt-dlp здесь недоступно"))
        self._ytdlp_updater.start()
        return self._ytdlp_updater.snapshot()

    def cancel_ytdlp_update(self):
        if self._ytdlp_updater is not None:
            self._ytdlp_updater.cancel()

    # ------------------------------------------------------------ обновление программы

    def app_update_status(self):
        """Версия программы, есть ли новее и как идёт её скачивание."""
        if self._app_updater is None:
            return {"state": "idle", "current": __version__, "can_install": False}
        return self._app_updater.snapshot()

    def check_app_update(self):
        """Спросить GitHub сейчас. Ответ — снимок состояния; итог придёт событием."""
        if self._app_updater is None:
            raise ValueError(tr("обновление программы здесь недоступно"))
        self._app_updater.check_in_background()
        return self._app_updater.snapshot()

    def install_app_update(self):
        if self._app_updater is None or not self._app_updater.can_install():
            raise ValueError(tr("эту копию программы не обновить отсюда — скачай выпуск "
                                "со страницы GitHub"))
        self._app_updater.start()
        return self._app_updater.snapshot()

    def cancel_app_update(self):
        if self._app_updater is not None:
            self._app_updater.cancel()

    def apply_app_update(self):
        """Закрыться и поставить скачанную версию: её установщик запустит программу снова."""
        if self._app_updater is None or not self._app_updater.apply():
            raise ValueError(tr("новая версия ещё не скачана"))
        self.quit()

    # ------------------------------------------------------------ TwitchDownloader

    def twitchdownloader_status(self):
        """Где TwitchDownloaderCLI для чата, какой версии и как идёт установка."""
        if self._twitchdownloader is None:
            status = {"path": None, "version": None, "own": False}
        else:
            status = self._twitchdownloader.status()
        installer = self._twitchdownloader_installer
        status["install"] = installer.snapshot() if installer else None
        return status

    def install_twitchdownloader(self):
        if self._twitchdownloader_installer is None:
            raise ValueError(tr("установка TwitchDownloader здесь недоступна"))
        self._twitchdownloader_installer.start()
        return self._twitchdownloader_installer.snapshot()

    def cancel_twitchdownloader_install(self):
        if self._twitchdownloader_installer is not None:
            self._twitchdownloader_installer.cancel()

    def choose_ffmpeg(self):
        """Выбрать ffmpeg.exe на диске. Возвращает путь и версию либо None."""
        if self._window is None:
            raise ValueError(tr("в браузере файл не выбрать — впиши путь руками"))
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.OPEN,
                                                 file_types=("ffmpeg (*.exe)",))
        if not chosen:
            return None
        return self.check_ffmpeg(chosen[0])

    def check_ffmpeg(self, path):
        """Действительно ли это ffmpeg: он должен запуститься и назвать версию."""
        path = str(path).strip().strip('"')
        if not os.path.isfile(path):
            raise ValueError(tr("файла нет: {path}", path=path))
        version = read_version(path)
        if not version:
            raise ValueError(tr("это не ffmpeg или он не запускается: {path}", path=path))
        return {"path": os.path.abspath(path), "version": version}

    # ------------------------------------------------------------ настройки

    def get_settings(self):
        data = {key: self._settings[key] for key in EDITABLE}
        for key in SECRETS:
            data[key + "_set"] = bool(data.pop(key))
        data["ytdlp_extra_args"] = subprocess.list2cmdline(data["ytdlp_extra_args"])
        data["downloads_dir"] = self._downloads.downloads_dir()
        # запуск вместе с Windows живёт в реестре, а не в settings.json
        data["autostart"] = bool(self._autostart and self._autostart.is_enabled())
        return data

    def save_settings(self, changes):
        """
        Сохраняет изменённые настройки. Секрет, которого нет в changes, не меняется;
        пустая строка его стирает. Возвращает, нужен ли перезапуск.
        """
        if not isinstance(changes, dict):
            raise ValueError(tr("настройки — объект"))
        changes = dict(changes)
        autostart = changes.pop("autostart", None)
        if autostart is not None and self._autostart is None:
            raise ValueError(tr("запуск вместе с Windows здесь недоступен"))
        clean = {}
        for key, value in changes.items():
            if key not in EDITABLE:
                raise ValueError(tr("настройку {key} окно не меняет", key=key))
            clean[key] = self._checked(key, value)
        # реестр трогаем, только когда всё остальное прошло проверку
        if autostart is not None:
            self._autostart.set_enabled(autostart is True)
            logging.info(tr("запуск вместе с Windows включён") if autostart is True
                         else tr("запуск вместе с Windows выключен"))

        before = self._settings.as_dict()
        self._settings.update(**clean)
        changed = [key for key in clean if before.get(key) != self._settings[key]]
        if "language" in changed:
            i18n.set_language(self._settings["language"])
        if "log_level" in changed:
            logging.getLogger().setLevel(self._settings["log_level"].upper())
        if "ffmpeg_path" in changed and self._ffmpeg is not None:
            # путь к ffmpeg подхватывается сразу: конвертация спрашивает его каждый раз
            self._ffmpeg.refresh()
        if changed:
            logging.info(tr("настройки изменены: {keys}", keys=", ".join(sorted(changed))))
        return {"restart": any(key in NEED_RESTART for key in changed),
                "settings": self.get_settings()}

    def _checked(self, key, value):
        if key in FLAGS:
            if not isinstance(value, bool):
                raise ValueError(tr("{key} — да или нет", key=key))
            return value
        if key == "language":
            if value not in i18n.LANGUAGES:
                raise ValueError(tr("незнакомый язык: {value}", value=value))
            return value
        if key == "ffmpeg_path":
            # пусто — искать самой; иначе это обязан быть рабочий ffmpeg
            value = str(value).strip().strip('"')
            return self.check_ffmpeg(value)["path"] if value else ""
        if key == "ytdlp_extra_args":
            return split_args(value) if isinstance(value, str) else [str(v) for v in value]
        if key == "twitch_auth_token":
            return ytdlp.normalize_auth_token(str(value))
        if key in LIMITS:
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise ValueError(tr("{key} — нужно целое число", key=key))
            low, high = LIMITS[key]
            if not low <= number <= high:
                raise ValueError(tr("{key} — от {low} до {high}", key=key, low=low, high=high))
            return number
        if key == "refresh":
            if not isinstance(value, dict):
                raise ValueError(tr("refresh — интервалы по платформам"))
            result = {}
            for platform, seconds in value.items():
                if platform not in PLATFORMS:
                    raise ValueError(tr("неизвестная платформа: {name}", name=platform))
                try:
                    result[platform] = max(15, int(seconds))
                except (TypeError, ValueError):
                    raise ValueError(tr("интервал опроса — целое число секунд"))
            return result
        if key == "split_mode":
            if value not in SPLIT_MODES:
                raise ValueError(tr("деление записи: {modes}", modes=", ".join(SPLIT_MODES)))
            return value
        if key == "split_size_gb":
            try:
                number = float(value)
            except (TypeError, ValueError):
                raise ValueError(tr("{key} — нужно число", key=key))
            if isinstance(value, bool) or not 0.1 <= number <= 1000:
                raise ValueError(tr("{key} — от {low} до {high}", key=key, low=0.1, high=1000))
            return round(number, 2)
        if key in ("chat_mode", "live_chat"):
            if value not in chat.MODES:
                raise ValueError(tr("чат: {modes}", modes=", ".join(chat.MODES)))
            return value
        if key == "log_level":
            if str(value).lower() not in LOG_LEVELS:
                raise ValueError(tr("уровень лога: {levels}", levels=", ".join(LOG_LEVELS)))
            return str(value).lower()
        return str(value).strip()

    def choose_folder(self, initial=""):
        if self._window is None:
            raise ValueError(tr("в браузере папку не выбрать — впиши путь руками"))
        import webview
        chosen = self._window.create_file_dialog(webview.FileDialog.FOLDER,
                                                 directory=str(initial or ""))
        return chosen[0] if chosen else None

    def quit(self):
        if self.on_quit is not None:
            self.on_quit()
