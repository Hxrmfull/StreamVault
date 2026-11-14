# -*- coding: utf-8 -*-
"""
Прокси: один адрес и площадки, которые ходят через него.

Через прокси идёт всё, что относится к выбранной площадке: проверка эфира,
запись (streamlink), загрузки и списки (yt-dlp и свои запросы), чат эфира
и чат записей, картинки смайлов. Остальные площадки — напрямую, как и
обновления программы и её инструментов с GitHub.

Адрес — http://, https://, socks5://, socks5h:// (имена узнаёт сам прокси)
или socks4://, с логином и паролем, если нужны: socks5://user:pass@host:1080.
Это настройка сети самого пользователя, а не обход защиты площадок: запросы
остаются теми же открытыми запросами зрителя без входа.

Настройка одна на программу — задаётся при запуске и при сохранении настроек
(configure); места, где программа ходит в сеть, спрашивают её по площадке.
"""

import logging
import socket
import threading
from urllib.parse import unquote, urlsplit, urlunsplit

from .i18n import tr
from .platforms import PLATFORMS

SCHEMES = ("http", "https", "socks5", "socks5h", "socks4")
# Чьи это узлы — по имени: для картинок и запросов, где площадка видна только по адресу.
DOMAINS = {
    "twitch": ("twitch.tv", "ttvnw.net", "jtvnw.net", "twitchcdn.net", "twitchsvc.net"),
    "kick": ("kick.com", "live-video.net"),
    "youtube": ("youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "ggpht.com", "youtube-nocookie.com"),
    "vkvideolive": ("vkvideo.ru", "vk.com", "vk.ru", "okcdn.ru", "vkuser.net", "userapi.com", "mycdn.me"),
}

_lock = threading.Lock()
_url = ""
_platforms = frozenset()


class ProxyError(ValueError):
    """Адрес прокси не годится — текст для человека."""


def normalize(url):
    """Адрес прокси в одном виде или ProxyError. Пусто — прокси нет."""
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text          # «host:port» — обычный http-прокси
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ProxyError(tr("адрес прокси не разобрать: нужен вид socks5://127.0.0.1:1080"))
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise ProxyError(tr("прокси бывает {schemes}", schemes=", ".join(name + "://" for name in SCHEMES)))
    if not parts.hostname or not port:
        raise ProxyError(tr("в адресе прокси нужны узел и порт: socks5://127.0.0.1:1080"))
    return urlunsplit((scheme, parts.netloc, "", "", ""))


def masked(url):
    """Адрес для журнала и окна: без пароля."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "…"
    if not parts.password:
        return url
    netloc = "%s:***@%s" % (parts.username or "", parts.netloc.rsplit("@", 1)[-1])
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def configure(url, platforms):
    """Прокси программы: адрес (пусто — нет) и площадки, которые через него ходят."""
    global _url, _platforms
    try:
        url = normalize(url)
    except ProxyError as error:
        logging.warning(tr("прокси не используется: {error}", error=error))
        url = ""
    chosen = frozenset(name for name in platforms or () if name in PLATFORMS)
    with _lock:
        changed = (url, chosen) != (_url, _platforms)
        _url, _platforms = url, chosen
    if changed and url:
        logging.info(tr("прокси {url} — для площадок: {platforms}", url=masked(url),
                        platforms=", ".join(sorted(chosen)) or tr("ни одной")))
    elif changed:
        logging.info(tr("прокси выключен"))


def for_platform(platform):
    """Адрес прокси для площадки или None — она ходит напрямую."""
    with _lock:
        return _url if _url and platform in _platforms else None


def platform_of(url):
    """Чья это ссылка или узел — по имени узла; не площадка — None."""
    host = (urlsplit(url).hostname if "://" in str(url) else str(url)) or ""
    host = host.lower().rstrip(".")
    for platform, domains in DOMAINS.items():
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return None


def for_url(url):
    """Адрес прокси для ссылки (по её площадке) или None."""
    platform = platform_of(url)
    return for_platform(platform) if platform else None


def requests_proxies(platform=None, url=None):
    """
    Значение proxies для requests: {"http": …, "https": …} — через прокси;
    None — как обычно (напрямую). Площадка — названием или по ссылке.
    """
    proxy = for_platform(platform) if platform else for_url(url) if url else None
    return {"http": proxy, "https": proxy} if proxy else None


def requests_kwargs(platform=None, url=None):
    """Именованные аргументы запроса requests: {"proxies": …} или ничего — напрямую."""
    proxies = requests_proxies(platform, url)
    return {"proxies": proxies} if proxies else {}


def session(platform, base=None):
    """Сессия requests площадки: с прокси, если площадка ходит через него."""
    import requests
    made = base or requests.Session()
    proxies = requests_proxies(platform)
    if proxies:
        made.proxies.update(proxies)
    return made


def streamlink_args(platform):
    """Аргументы streamlink: прокси площадки."""
    proxy = for_platform(platform)
    return ["--http-proxy", proxy] if proxy else []


def ytdlp_args(url=None, platform=None):
    """Аргументы yt-dlp: прокси — по площадке или по ссылке, которую он открывает."""
    proxy = for_platform(platform) if platform else for_url(url) if url else None
    return ["--proxy", proxy] if proxy else []


def child_environment(environment, platform):
    """
    Окружение помощника, который сам берёт прокси из окружения (TwitchDownloader):
    то же, с адресом прокси, если площадка ходит через него.
    """
    proxy = for_platform(platform)
    if not proxy:
        return environment
    return dict(environment, HTTPS_PROXY=proxy, HTTP_PROXY=proxy, ALL_PROXY=proxy)


def connect(host, port, timeout=15, platform=None):
    """
    Соединение с узлом (как socket.create_connection) — через прокси, если
    площадка узла ходит через него: для чата Twitch (IRC) и Kick (websocket),
    которые ходят не через requests.
    """
    proxy = for_platform(platform) if platform else for_url(host)
    if not proxy:
        return socket.create_connection((host, port), timeout=timeout)
    import socks
    parts = urlsplit(proxy)
    kind = {"http": socks.HTTP, "https": socks.HTTP, "socks4": socks.SOCKS4}.get(parts.scheme, socks.SOCKS5)
    return socks.create_connection(
        (host, port), timeout=timeout, proxy_type=kind, proxy_addr=parts.hostname, proxy_port=parts.port,
        # socks5h — имя узла узнаёт прокси; у http-прокси так всегда
        proxy_rdns=parts.scheme != "socks5",
        proxy_username=unquote(parts.username) if parts.username else None,
        proxy_password=unquote(parts.password) if parts.password else None)
