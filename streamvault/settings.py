# -*- coding: utf-8 -*-
"""
Настройки программы: settings.json рядом с ней.

В отличие от config.py старого рекордера, их меняет и сама программа:
добавленный канал или выключенная автозапись сохраняются сразу.
"""

import copy
import json
import logging
import os
import runpy
import sys
import tempfile
import threading

from .platforms import PLATFORMS, channel_label, normalize_channels
from .i18n import tr


def _writable(folder):
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def app_dir():
    """
    Где лежат settings.json, журналы, кэш и свой ffmpeg. Из исходников —
    папка проекта. У exe — его папка: программа переносная, всё своё
    носит с собой. Если туда писать нельзя (положили в Program Files),
    а настроек там ещё нет, — %APPDATA%\\StreamVault.
    """
    if not getattr(sys, "frozen", False):
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    folder = os.path.dirname(os.path.abspath(sys.executable))
    if os.path.exists(os.path.join(folder, "settings.json")) or _writable(folder):
        return folder
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "StreamVault")


APP_DIR = app_dir()
DEFAULT_SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    # Язык окна: ru, en или zh. Пусто — ещё не выбран, при первом запуске
    # берётся язык Windows.
    "language": "",
    # Куда складывать записи. Внутри создаются recorded/<платформа>/<канал>
    # (сырые .ts) и processed/<платформа>/<канал> (готовые .mp4).
    "root_path": os.path.join(os.path.expanduser("~"), "Videos", "StreamVault"),
    # Качество по умолчанию: best, 1080p60, 720p, worst... У канала может быть своё.
    "quality": "best",
    # [{"platform": "twitch", "channel": "имя", "enabled": true, "quality": "720p"}]
    # У канала бывают и свои настройки записи — "options": {"record_from_start":
    # false, "split_mode": "size", ...} (platforms.CHANNEL_OPTIONS).
    # Вместо объекта можно написать строку "kick:xqc" или ссылку.
    "channels": [],
    # Приложение с https://dev.twitch.tv/console/apps. Нужно только ради
    # дешёвого опроса Twitch раз в 15 секунд; без него Twitch спрашивается
    # у streamlink, как остальные платформы.
    "twitch_client_id": "",
    "twitch_client_secret": "",
    # Интервал опроса по платформам, секунды.
    "refresh": {name: spec["refresh"] for name, spec in PLATFORMS.items()},
    # Сколько ждать ответа streamlink при опросе статуса. Kick решает
    # JS-проверку через браузер, это небыстро.
    "probe_timeout": 180,
    # Путь к ffmpeg. Пусто — найти самой: своя копия, PATH, обычные места установки.
    "ffmpeg_path": "",
    # Конвертировать записи эфиров из .ts в mp4. Выключено — запись остаётся
    # в .ts (диск пишется один раз), сконвертировать можно потом из библиотеки.
    "convert_recordings": True,
    # Оглавление mp4 в начало файла (+faststart): видео сразу играет по сети,
    # но готовый файл переписывается ещё раз. Для просмотра на компьютере не нужно.
    "mp4_faststart": True,
    # Делить запись на куски при конвертации в mp4 — без перекодирования, по
    # ключевым кадрам: off — не делить, size — куски не больше split_size_gb ГБ,
    # duration — по split_minutes минут, count — на split_count равных частей.
    # Части эфира после обрывов делятся каждая отдельно.
    "split_mode": "off",
    "split_size_gb": 2.0,
    "split_minutes": 60,
    "split_count": 2,
    # Запись, начатая посреди эфира, берёт его с начала — насколько площадка
    # даёт его запись: кусок до начала записи скачивается из записи эфира на
    # площадке первой частью (live/beginning.py). Только у первой части:
    # после обрыва новая часть продолжает с места, а не повторяет весь эфир.
    "record_from_start": False,
    # Скачанное начало эфира и запись — в один файл, когда обе части готовы:
    # стык по кадрам, без перекодирования, части удаляются (live/joining.py).
    # Выключено — части остаются рядом и играют в окне подряд.
    "join_beginning": True,
    # Сколько гигабайт оставлять свободными на диске с записями. Меньше —
    # предупреждение, меньше пятой части — запись останавливается. 0 — не следить.
    "min_free_gb": 5,
    # Сколько дней хранить записи эфиров; старые уходят в корзину вместе
    # с чатом. 0 — хранить всегда. У канала может быть свой срок (keep_days).
    "keep_days": 0,
    # Сколько раз повторять конвертацию упавшего файла и пауза между попытками.
    "ffmpeg_attempts": 3,
    "ffmpeg_retry_delay": 120,
    # Сколько времени после остановки дать очереди на доработку, секунды.
    "drain_grace": 900,
    # Куда скачивать готовые записи и клипы. Пусто — папка downloads в root_path.
    "downloads_path": "",
    # Качество скачивания по умолчанию: best, worst, audio, 1080, 720...
    "download_quality": "best",
    # Сколько загрузок идёт одновременно.
    "max_downloads": 2,
    # Сколько кусков записи эфира качать параллельно: так в разы быстрее.
    "download_fragments": 4,
    # Кука auth-token с twitch.tv: с ней yt-dlp скачивает записи, доступные
    # только подписчикам. Даёт полный доступ к аккаунту — как пароль.
    "twitch_auth_token": "",
    # Дополнительные аргументы yt-dlp, например ["--cookies-from-browser", "firefox"].
    "ytdlp_extra_args": [],
    # Чат записей и клипов Twitch по умолчанию (через TwitchDownloader):
    # none — без чата, text — чат для браузера (HTML) и JSON, render — ещё и видео чата.
    "chat_mode": "none",
    # Чат прямых эфиров Twitch при записи: none — не писать, text — сохранить
    # (.chat.json и .chat.html рядом с записью), render — ещё и видео чата.
    "live_chat": "text",
    # Видео чата: ширина и высота в пикселях, размер шрифта.
    "chat_width": 422,
    "chat_height": 1080,
    "chat_font_size": 18,
    # Закрытие окна сворачивает программу в трей, записи продолжаются.
    "close_to_tray": True,
    # Уведомления в трее: началась запись, скачалось — пока окно свёрнуто.
    "tray_notifications": True,
    # Спрашивать GitHub о новой версии программы при запуске.
    "check_updates": True,
    "log_level": "info",
    "log_max_bytes": 5 * 1024 * 1024,
    "log_backup_count": 5,
}

# Как назывались параметры в config.py старого рекордера.
_LEGACY_NAMES = {
    "client_id": "twitch_client_id",
    "client_secret": "twitch_client_secret",
}


class SettingsError(Exception):
    """settings.json не читается или испорчен."""


class Settings:
    """Настройки в памяти и на диске. Потокобезопасны: их меняет и окно, и движок."""

    def __init__(self, path=DEFAULT_SETTINGS_PATH, data=None):
        self.path = path
        self._lock = threading.RLock()
        self._data = _validated(data or {})

    @classmethod
    def load(cls, path=DEFAULT_SETTINGS_PATH):
        """Читает settings.json. Нет файла — настройки по умолчанию."""
        if not os.path.exists(path):
            return cls(path)
        try:
            # utf-8-sig: Блокнот иногда сохраняет файл с BOM
            with open(path, encoding="utf-8-sig") as source:
                data = json.load(source)
        except ValueError as error:
            raise SettingsError(tr("{path} испорчен: {error}", path=path, error=error))
        except OSError as error:
            raise SettingsError(tr("{path} не читается: {error}", path=path,
                                   error=error.strerror or error))
        if not isinstance(data, dict):
            raise SettingsError(tr("{path}: ожидался объект в фигурных скобках", path=path))
        return cls(path, data)

    def __getitem__(self, key):
        with self._lock:
            return copy.deepcopy(self._data[key])

    def get(self, key, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def update(self, **changes):
        """Меняет параметры и сразу сохраняет файл."""
        with self._lock:
            self._data.update(_validated(changes, base=self._data))
            self.save()

    def as_dict(self):
        with self._lock:
            return copy.deepcopy(self._data)

    def save(self):
        """
        Пишет во временный файл и подменяет им настоящий: если программу
        прервут посреди записи, settings.json останется целым.
        """
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            temporary = self.path + ".tmp"
            with open(temporary, "w", encoding="utf-8") as target:
                json.dump(self._data, target, ensure_ascii=False, indent=2)
                target.write("\n")
            os.replace(temporary, self.path)


def _validated(data, base=None):
    """
    Проверяет типы. Негодное значение не роняет программу: в лог уходит
    предупреждение, а параметр остаётся прежним. Незнакомые параметры
    сохраняются как есть, чтобы не потерять написанное руками.
    """
    result = copy.deepcopy(base if base is not None else DEFAULTS)
    for key, value in data.items():
        key = _LEGACY_NAMES.get(key, key)
        if key not in DEFAULTS:
            logging.warning(tr("настройки: незнакомый параметр {key}, оставляю как есть",
                               key=key))
            result[key] = value
            continue

        default = DEFAULTS[key]
        if key == "refresh":
            # в первой версии рекордера refresh — одно число на все платформы
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                value = {name: value for name in PLATFORMS}
            if isinstance(value, dict):
                merged = copy.deepcopy(default)
                merged.update(value)
                result[key] = merged
                continue
        elif key == "channels":
            if isinstance(value, (list, str, dict)):
                result[key] = normalize_channels(value)
                continue
        elif isinstance(default, bool):
            # проверяется раньше int: True для Python — тоже целое число
            if isinstance(value, bool):
                result[key] = value
                continue
        elif isinstance(default, int):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = int(value)
                continue
        elif isinstance(default, float):
            # в settings.json дробное число бывает записано целым: 2, а не 2.0
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = float(value)
                continue
        elif isinstance(value, type(default)):
            result[key] = value
            continue

        logging.warning(tr("настройки: {key} задан непонятно ({value}), оставляю {kept}",
                           key=key, value=repr(value), kept=repr(result[key])))
    return result


def channel_setting(settings, key, platform, label):
    """
    Настройка записи для канала: своя у канала (options в его записи
    в списке) или общая. Канал узнаётся по площадке и имени папки записей:
    так её находят и очередь конвертации, и чат, у которых есть только путь.
    """
    if platform and label:
        for entry in settings["channels"]:
            if entry["platform"] == platform and channel_label(entry["channel"]).lower() == label.lower():
                options = entry.get("options") or {}
                if key in options:
                    return options[key]
                break
    return settings[key]


def legacy_channels(namespace):
    """Каналы из config.py старого рекордера: словарь, список, строка или usernames."""
    configured = namespace.get("channels")
    raw = []
    if isinstance(configured, dict):
        for platform, names in configured.items():
            if isinstance(names, str):
                names = [names]
            for entry in names or []:
                raw.extend("%s:%s" % (platform, name.strip())
                           for name in str(entry).split(",") if name.strip())
    elif configured:
        raw = configured
    else:
        raw = namespace.get("usernames") or []
    # старый рекордер записывал каждый эфир своих каналов — так и остаётся
    return normalize_channels(raw, enabled=True)


def import_legacy_config(config_path, settings_path=DEFAULT_SETTINGS_PATH):
    """Переносит config.py старого рекордера в settings.json."""
    namespace = runpy.run_path(config_path)
    data = {}
    for key in DEFAULTS:
        if key in namespace and key != "channels":
            data[key] = namespace[key]
    for old, new in _LEGACY_NAMES.items():
        if namespace.get(old):
            data[new] = namespace[old]
    data["channels"] = legacy_channels(namespace)

    settings = Settings(settings_path, data)
    settings.save()
    return settings
