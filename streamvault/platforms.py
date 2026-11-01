# -*- coding: utf-8 -*-
"""
Платформы и разбор каналов: из "kick:xqc", "legendaafk" или ссылки
получается пара (платформа, канал), а из неё — ссылка для streamlink
и имя папки для записей.
"""

import logging
import re

from .util import sanitize_filename
from .i18n import tr

# Шаблон ссылки и разумный интервал опроса для каждой платформы.
# Добавить новую — дописать строку: streamlink покроет её своим плагином.
PLATFORMS = {
    "twitch": {
        "url": "https://twitch.tv/{channel}",
        "hosts": ("twitch.tv",),
        "refresh": 15,
    },
    "kick": {
        "url": "https://kick.com/{channel}",
        "hosts": ("kick.com",),
        "refresh": 60,
    },
    "youtube": {
        "url": "https://www.youtube.com/@{channel}/live",
        "hosts": ("youtube.com", "youtu.be"),
        "refresh": 60,
    },
    "vkvideolive": {
        "url": "https://live.vkvideo.ru/{channel}",
        # плагин vkvideolive понимает три разных домена
        "hosts": ("vkvideo.ru", "vkplay.ru", "vkplay.live"),
        "refresh": 60,
    },
}

DEFAULT_PLATFORM = "twitch"

# Twitch просит не опрашивать Helix чаще, чем раз в 15 секунд. Опрос через
# streamlink куда дороже — там поднимается плагин, а для Kick ещё и браузер.
MIN_REFRESH_API = 15
MIN_REFRESH_STREAMLINK = 30


def is_url(text):
    return text.lower().startswith(("http://", "https://"))


def _url_parts(url):
    """Хост, непустые сегменты пути и строка запроса."""
    rest = url.split("//", 1)[-1].partition("#")[0]
    rest, _, query = rest.partition("?")
    host, _, path = rest.partition("/")
    return host.lower(), [part for part in path.split("/") if part], query


# Платформы, у которых ссылка на канал — это домен плюс ровно один сегмент имени.
_NAME_IN_PATH_PLATFORMS = ("twitch", "kick", "vkvideolive")

# Логины Twitch и Kick не различают регистр; приводим, чтобы не плодить дубли.
_CASE_INSENSITIVE_PLATFORMS = ("twitch", "kick")


def canonical_channel(platform, channel):
    """
    Сводит ссылку к имени канала, когда это возможно: twitch.tv/name -> name.

    Без этого Twitch получал бы в API ссылку вместо логина и отвечал 400,
    а один канал, записанный то ником, то ссылкой, писался бы дважды.
    Ссылки, которые к имени не сводятся (запись, клип, watch?v=), остаются как есть.
    """
    if not is_url(channel):
        name = channel.strip().lstrip("@")
        return name.lower() if platform in _CASE_INSENSITIVE_PLATFORMS else name

    _, parts, query = _url_parts(channel)
    if platform == "vkvideolive" and vk_video_url(channel):
        return channel      # трансляция обычного VK Видео, а не канал (sites.vkvideolive.same_stream)
    if platform in _NAME_IN_PATH_PLATFORMS and len(parts) == 1:
        # kick.com/name?clip=... — это клип, а не трансляция
        if not (platform == "kick" and "clip=" in query):
            return canonical_channel(platform, parts[0])
    if (platform == "youtube" and parts and parts[0].startswith("@")
            and (len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == "live"))):
        return canonical_channel(platform, parts[0])
    return channel


# Видео или трансляция обычного VK Видео: vkvideo.ru/live-197477005_456243748,
# vk.com/video-1_2, vk.com/…?z=video-1_2%2F… — владелец (минус — сообщество) и номер.
_VK_VIDEO_HOSTS = ("vk.com", "vk.ru", "vkvideo.ru")
_VK_VIDEO = re.compile(r"(?:^|[/=])(?:live|video)(-?\d+)_(\d+)")


def vk_video_url(text):
    """Ссылка на видео обычного VK Видео — в виде, понятном yt-dlp и streamlink, иначе ""."""
    if not is_url(str(text)):
        return ""
    host, parts, query = _url_parts(str(text))
    if not any(host == known or host.endswith("." + known) for known in _VK_VIDEO_HOSTS):
        return ""
    # у VK Video Live свой поддомен: live.vkvideo.ru/<канал>
    if host.startswith("live."):
        return ""
    match = _VK_VIDEO.search("/".join(parts) + "&" + query.replace("%2F", "/").replace("%2f", "/"))
    return "https://vk.com/video%s_%s" % match.groups() if match else ""


def stream_url(platform, channel):
    """Ссылка для streamlink. Готовый URL в настройках принимается как есть."""
    if is_url(channel):
        return channel
    # собаку шаблоны подставляют сами, иначе получится @@channel
    return PLATFORMS[platform]["url"].format(channel=channel.lstrip("@"))


# Хвосты пути, которые сами по себе не называют канал.
_MEANINGLESS_PATH_PARTS = ("live", "watch", "video", "videos", "embed")

# Имена устройств Windows: папку nul создать нельзя, остальные капризны.
_RESERVED_NAMES = ({"con", "prn", "aux", "nul"}
                   | {"com%d" % i for i in range(1, 10)}
                   | {"lpt%d" % i for i in range(1, 10)})


def channel_label(channel):
    """Имя папки и префикс файла. Из полной ссылки берём осмысленный кусок."""
    if is_url(channel):
        _, parts, query = _url_parts(channel)
        # youtube.com/watch?v=ID — название лежит в параметре, а не в пути
        for param in query.split("&"):
            key, _, value = param.partition("=")
            if key == "v" and value:
                channel = value
                break
        else:
            # отбрасываем бессмысленные хвосты вроде /live
            while parts and parts[-1].lower() in _MEANINGLESS_PATH_PARTS:
                parts.pop()
            channel = parts[-1] if parts else "stream"
    label = sanitize_filename(channel.lstrip("@")) or "stream"
    if label.lower() in _RESERVED_NAMES:
        label = "_" + label
    return label


def channel_id(platform, channel):
    """Ключ канала в программе: по нему окно программы обращается к каналу."""
    return "%s:%s" % (platform, channel)


def parse_channel(item, default_platform=DEFAULT_PLATFORM):
    """
    Разбирает запись вида "kick:xqc", "legendaafk" или полную ссылку.
    Возвращает (платформа, канал) либо (None, причина отказа).
    """
    item = str(item).strip()
    if not item:
        return None, tr("пустая строка")

    if is_url(item):
        host, _, _ = _url_parts(item)
        for name, spec in PLATFORMS.items():
            for known in spec["hosts"]:
                # сам домен либо любой его поддомен: m.twitch.tv, www.youtube.com
                if host == known or host.endswith("." + known):
                    return name, canonical_channel(name, item)
        return None, tr("не понимаю, какой платформе принадлежит ссылка: {link}", link=item)

    platform, separator, channel = item.partition(":")
    if not separator:
        return default_platform, canonical_channel(default_platform, item)
    platform = platform.strip().lower()
    channel = channel.strip()
    if platform not in PLATFORMS:
        return None, tr("неизвестная платформа «{platform}» в «{text}»",
                        platform=platform, text=item)
    if not channel:
        return None, tr("не указан канал в «{text}»", text=item)
    return platform, canonical_channel(platform, channel)


def clean_profile(profile):
    """
    Имя и аватарка канала, найденные поиском: у Kick, YouTube и VK Video Live
    их больше неоткуда взять. Только строки; аватарка — только https.
    """
    if not isinstance(profile, dict):
        return None
    name = str(profile.get("name") or "").strip()[:100]
    avatar = str(profile.get("avatar") or "").strip()
    if not avatar.lower().startswith("https://") or len(avatar) > 1000:
        avatar = ""
    return {"name": name, "avatar": avatar} if name or avatar else None


def keep_days_value(value):
    """Срок хранения записей канала: целое от 0 (хранить всегда) или None — как в общих настройках."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, min(int(value), 3650))
    except (TypeError, ValueError):
        return None


# Свои настройки записи у канала (entry["options"]) поверх общих: те же
# ключи, что в settings.json. Чего у канала нет — берётся общее.
CHANNEL_FLAGS = ("record_from_start", "join_beginning", "convert_recordings", "mp4_faststart")
CHANNEL_CHOICES = {"live_chat": ("none", "text", "render"), "split_mode": ("off", "size", "duration", "count")}
CHANNEL_NUMBERS = {"split_size_gb": (0.1, 1000, float), "split_minutes": (5, 1440, int), "split_count": (2, 100, int)}
CHANNEL_OPTIONS = CHANNEL_FLAGS + tuple(CHANNEL_CHOICES) + tuple(CHANNEL_NUMBERS)


def channel_option_value(key, value):
    """Значение своей настройки канала или ValueError: окно и settings.json проверяются одинаково."""
    if key in CHANNEL_FLAGS:
        if isinstance(value, bool):
            return value
    elif key in CHANNEL_CHOICES:
        if value in CHANNEL_CHOICES[key]:
            return value
    elif key in CHANNEL_NUMBERS:
        low, high, kind = CHANNEL_NUMBERS[key]
        if isinstance(value, (int, float)) and not isinstance(value, bool) and low <= value <= high:
            return round(float(value), 2) if kind is float else int(value)
        raise ValueError(tr("{key} — от {low} до {high}", key=key, low=low, high=high))
    else:
        raise ValueError(tr("настройку {key} у канала не задать", key=key))
    raise ValueError(tr("{key} задан непонятно: {value}", key=key, value=repr(value)))


def clean_channel_options(options):
    """Свои настройки канала из settings.json: негодные пропускаются с записью в лог."""
    clean = {}
    for key, value in (options or {}).items() if isinstance(options, dict) else ():
        try:
            clean[key] = channel_option_value(key, value)
        except ValueError as error:
            logging.warning(tr("настройки канала: {error}", error=error))
    return clean


def normalize_channels(raw, enabled=False):
    """
    Каналы из настроек или командной строки: строки "kick:xqc", ссылки,
    перечисления через запятую или объекты {"platform", "channel", "enabled",
    "quality", "keep_days", "options"}. Возвращает объекты без дублей; негодные записи уходят в лог.
    enabled — автозапись у канала, где она не указана явно: в окне по умолчанию
    выключена, у каналов консольной записи (-u) и из старого рекордера — включена.
    """
    if isinstance(raw, (str, dict)):
        raw = [raw]

    result = []
    seen = set()
    for item in raw or []:
        options = item if isinstance(item, dict) else {}
        if options:
            text = str(options.get("channel", "")).strip()
            platform = str(options.get("platform", "")).strip()
            if platform and text and not is_url(text):
                text = "%s:%s" % (platform, text)
            candidates = [text]
        else:
            candidates = str(item).split(",")

        for text in candidates:
            if not text.strip():
                continue
            platform, channel = parse_channel(text)
            if platform is None:
                logging.error("%s", channel)
                continue
            # один канал, записанный в разном регистре, — всё равно один канал
            key = (platform, channel.lower())
            if key in seen:
                logging.info(tr("{platform}:{channel} указан дважды, второй раз пропускаю",
                                platform=platform, channel=channel))
                continue
            seen.add(key)
            entry = {"platform": platform, "channel": channel,
                     "enabled": bool(options.get("enabled", enabled))}
            if options.get("quality"):
                entry["quality"] = str(options["quality"]).strip()
            keep_days = keep_days_value(options.get("keep_days"))
            if keep_days is not None:
                entry["keep_days"] = keep_days
            channel_options = clean_channel_options(options.get("options"))
            if channel_options:
                entry["options"] = channel_options
            profile = clean_profile(options.get("profile"))
            if profile:
                entry["profile"] = profile
            result.append(entry)
    return result


def refresh_for(platform, overrides, floor):
    """Интервал опроса платформы из настроек, с запасным значением и нижней границей."""
    default = PLATFORMS[platform]["refresh"]
    value = (overrides or {}).get(platform, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        logging.warning(tr("интервал опроса для {platform} задан непонятно ({value}), "
                           "беру {default} с", platform=platform, value=repr(value),
                           default=default))
        value = default
    return max(floor, value)
