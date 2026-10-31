# -*- coding: utf-8 -*-
"""
Чат записей YouTube, Kick и VK Video Live — к скачанному видео, как у Twitch.

- YouTube: чат прошедшего эфира отдаёт yt-dlp (субтитры live_chat) —
  весь, с временем каждого сообщения в записи.
- Kick: история чата канала открыта — web.kick.com отдаёт сообщения
  до заданного момента по 25 штук. Запись проходится от конца к началу.
- VK Video Live: чат записи открыт, как на её странице, — сообщения около
  заданного места записи (time_code), со временем каждого в записи.
  Запись проходится от начала к концу.

Сообщения разбираются так же, как чат идущего эфира (live/chatreaders.py),
пишутся во временный .chat.jsonl и переводятся в .chat.json формата
TwitchDownloader со смайлами и значками внутри (livechat.convert). Дальше —
как у Twitch: HTML и видео чата делает TwitchDownloader.
"""

import datetime
import glob
import json
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from urllib.parse import quote

import requests

from .. import util
from ..i18n import tr
from ..live import chatreaders, livechat
from . import chat

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) StreamVault",
           "Accept": "application/json", "x-app-platform": "web"}
REQUEST_TIMEOUT = 20
KICK_VIDEO = "https://kick.com/api/v1/video/{uuid}"
KICK_HISTORY = "https://web.kick.com/api/v1/chat/{channel_id}/history"
KICK_VIDEOS = "https://web.kick.com/api/v1/channels/{channel_id}/videos"
KICK_SLUG = re.compile(r"kick\.com/([\w-]+)/videos/", re.I)
KICK_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
# пауза между страницами истории чата: сайт не должен принять нас за нападение
PAGE_PAUSE = 0.15
RETRIES = (2, 5, 15)
VK_RECORD_CHAT = "https://api.live.vkvideo.ru/v1/channel/{blog}/record/{record}/chat/"
KICK_CLIP = re.compile(r"(clip_[0-9A-Za-z]+)")
KICK_CLIP_INFO = "https://kick.com/api/v2/clips/{clip}"
VK_CLIP = re.compile(r"(?:live\.vkvideo\.ru|live\.vkplay\.ru|vkplay\.live)/([^/?#]+)/clip/([0-9a-f-]{36})", re.I)
VK_CLIP_INFO = "https://api.live.vkvideo.ru/v1/channel/{blog}/clip/{clip}"
VK_RECORD = re.compile(r"(?:live\.vkvideo\.ru|live\.vkplay\.ru|vkplay\.live)/([^/?#]+)/record/([0-9a-f-]{36})", re.I)
VK_PAGE = 200


def download(platform, url, target, title="", channel="", section=None, duration=None,
             progress=None, stopped=lambda: False, on_process=None, env=None, session=None,
             command=None, fetch=None):
    """
    Чат записи -> target (.chat.json). Возвращает число сообщений (0 — чата нет,
    файл не создаётся). progress(проценты или None); stopped() — остановили:
    тогда chat.ChatStopped; не вышло — chat.ChatError с текстом для человека.
    on_process(процесс yt-dlp) — чтобы его можно было погасить.
    """
    # не значением по умолчанию: при импорте livechat может быть загружен не до конца
    fetch = fetch or livechat.fetch_image
    folder = tempfile.mkdtemp(prefix="sv-vodchat-")
    jsonl = os.path.join(folder, "chat.chat.jsonl")
    try:
        if platform == "youtube":
            records = _youtube(url, folder, section, duration, progress, stopped, on_process, env,
                               command or util.YTDLP)
        elif platform == "kick":
            records = _kick(url, section, progress, stopped, session or requests.Session())
        elif platform == "vkvideolive":
            records = _vk(url, section, duration, progress, stopped, session or requests.Session())
        else:
            raise chat.ChatError(tr("у этой площадки чата записей нет"))
        with open(jsonl, "w", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
        if stopped():
            raise chat.ChatStopped()
        try:
            return livechat.convert(jsonl, target, title=title, channel=channel, fetch=fetch)
        except (OSError, ValueError) as error:
            raise chat.ChatError(tr("чат не сохранился: {error}", error=error))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _described(records, kind, items, described):
    for item_id, name, url_ in items:
        if url_ and item_id not in described:
            described.add(item_id)
            records.append({"type": kind, "id": item_id, "name": name, "url": url_})


# ------------------------------------------------------------ YouTube

def youtube_records(lines, section=None):
    """
    Строки live_chat.json от yt-dlp -> записи .chat.jsonl. Время сообщения
    в записи — videoOffsetTimeMsec (у отрезка — от его начала); время
    отправки сдвигается к нему одной поправкой на всю запись.
    """
    begin = section[0] if section else 0.0
    end = section[1] if section else None
    actions, gaps = [], []
    for line in lines:
        try:
            data = json.loads(line)
            replay = data["replayChatItemAction"]
            offset = int(replay.get("videoOffsetTimeMsec") or 0) / 1000
        except (ValueError, KeyError, TypeError):
            continue
        for action in replay.get("actions") or ():
            parsed = chatreaders.parse_youtube_action(action)
            if parsed is None:
                continue
            actions.append((offset, parsed))
            if parsed[0] == "msg":
                gaps.append(parsed[1]["ts"] - offset)
    # момент начала записи по часам отправки; без сообщений — неважно
    base = statistics.median(gaps) if gaps else time.time()
    records = [{"type": "start", "ts": base + begin, "platform": "youtube"}]
    described = set()
    for offset, parsed in actions:
        if parsed[0] == "delete":
            records.append({"type": "delete", "id": parsed[1]})
        elif parsed[0] == "delete_user":
            records.append({"type": "delete_user", "user_id": parsed[1]})
        elif offset >= begin and (end is None or offset <= end):
            record, emotes, badges = parsed[1:]
            record["ts"] = base + offset
            _described(records, "emote", emotes, described)
            _described(records, "badge", badges, described)
            records.append(record)
    return records


def _youtube(url, folder, section, duration, progress, stopped, on_process, env, command):
    log_path = os.path.join(folder, "yt-dlp.log")
    with open(log_path, "wb") as log:
        try:
            process = subprocess.Popen(
                [*command, "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                 "--no-part", "--no-warnings", "--color", "never", "--no-progress",
                 "-o", os.path.join(folder, "video"), "--", url],
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env or util.child_environment(), creationflags=util.subprocess_flags())
        except OSError as error:
            raise chat.ChatError(tr("не удалось запустить yt-dlp: {error}", error=error))
    if on_process:
        on_process(process)
    try:
        # прогресс — по времени последнего скачанного сообщения в записи
        while process.poll() is None:
            if stopped():
                util.kill_process_tree(process)
                process.wait(10)
                raise chat.ChatStopped()
            if progress and duration:
                reached = _last_offset(folder)
                if reached is not None:
                    progress(max(0, min(99, int(reached * 100 / duration))))
            time.sleep(1)
    finally:
        if process.poll() is None:
            util.kill_process_tree(process)
            process.wait(10)
    if stopped():
        raise chat.ChatStopped()
    source = _chat_file(folder)
    if process.returncode != 0 or source is None:
        with open(log_path, encoding="utf-8", errors="replace") as log:
            output = log.read()
        if source is None and process.returncode == 0 or "no subtitles" in output.lower():
            # обычное видео или чат выключен: не ошибка, просто чата нет
            return []
        errors = [line[len("ERROR:"):].strip() for line in output.splitlines() if line.startswith("ERROR:")]
        raise chat.ChatError(errors[-1] if errors else tr("yt-dlp завершился с кодом {code}",
                                                          code=process.returncode))
    with open(source, encoding="utf-8", errors="replace") as lines:
        return youtube_records(lines, section)


def _chat_file(folder):
    return next(iter(glob.glob(os.path.join(glob.escape(folder), "*.live_chat.json"))), None)


def _last_offset(folder):
    """Время в записи последнего скачанного сообщения: хвост растущего файла."""
    source = _chat_file(folder)
    if source is None:
        return None
    try:
        with open(source, "rb") as chat_file:
            chat_file.seek(0, os.SEEK_END)
            chat_file.seek(max(0, chat_file.tell() - 65536))
            tail = chat_file.read().decode("utf-8", "replace")
    except OSError:
        return None
    found = re.findall(r'"videoOffsetTimeMsec":\s*"(-?\d+)"', tail)
    return int(found[-1]) / 1000 if found else None


# ------------------------------------------------------------ Kick

def _get_json(session, url, stopped, **params):
    for attempt, pause in enumerate(RETRIES + (None,)):
        if stopped():
            raise chat.ChatStopped()
        try:
            response = session.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                raise chat.ChatError(tr("запись не найдена — её удалили или она закрыта"))
            if response.status_code < 400:
                return response.json()
            problem = "HTTP %d" % response.status_code
        except (requests.RequestException, ValueError) as error:
            problem = error.__class__.__name__
        if pause is None:
            raise chat.ChatError(tr("площадка не отдала чат: {error}", error=problem))
        time.sleep(pause)


def _kick_time(text):
    return datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00").replace(" ", "T")).timestamp()


def _kick_video(url, session, stopped):
    """
    Запись Kick по ссылке -> (id канала, начало, длительность в секундах,
    значки подписчиков). Ссылки yt-dlp и списка записей программы — с vod_id:
    он есть в списке записей канала. Старые ссылки — с uuid видео.
    """
    match = KICK_UUID.search(url)
    if not match:
        raise chat.ChatError(tr("не понял, какая это запись Kick: {url}", url=url))
    video_id = match.group(0).lower()
    slug = KICK_SLUG.search(url)
    if slug:
        channel = _get_json(session, chatreaders.KICK_CHANNEL.format(slug=slug.group(1).lower()), stopped) or {}
        badges = chatreaders.kick_subscriber_badges(channel)
        if channel.get("id"):
            videos = (_get_json(session, KICK_VIDEOS.format(channel_id=channel["id"]), stopped) or {}).get("data")
            found = next((video for video in videos or () if str(video.get("id")).lower() == video_id), None)
            if found:
                try:
                    return (channel["id"], _kick_time(found["start_time"]),
                            float(found.get("duration") or 0), badges)
                except (KeyError, TypeError, ValueError):
                    pass
    livestream = (_get_json(session, KICK_VIDEO.format(uuid=video_id), stopped) or {}).get("livestream") or {}
    try:
        return (livestream["channel_id"], _kick_time(livestream["start_time"]),
                float(livestream.get("duration") or 0) / 1000, [])
    except (KeyError, TypeError, ValueError):
        raise chat.ChatError(tr("Kick не сказал, когда шла эта запись"))


def _kick_clip(clip_id, session, stopped):
    """Клип Kick -> (id канала, начало, длительность, значки подписчиков)."""
    clip = _get_json(session, KICK_CLIP_INFO.format(clip=clip_id), stopped) or {}
    clip = clip.get("clip") or clip
    try:
        channel_id = clip["channel_id"]
        start = _kick_time(clip["started_at"])
        length = float(clip.get("duration") or 0)
    except (KeyError, TypeError, ValueError):
        raise chat.ChatError(tr("Kick не сказал, когда шла эта запись"))
    badges = []
    slug = (clip.get("channel") or {}).get("slug")
    if slug:
        try:
            badges = chatreaders.kick_subscriber_badges(
                _get_json(session, chatreaders.KICK_CHANNEL.format(slug=slug), stopped) or {})
        except chat.ChatError:
            pass
    return channel_id, start, length, badges


def _kick(url, section, progress, stopped, session):
    clip = KICK_CLIP.search(url)
    if clip:
        channel_id, start, length, subscriber_badges = _kick_clip(clip.group(1), session, stopped)
    else:
        channel_id, start, length, subscriber_badges = _kick_video(url, session, stopped)
    begin = start + (section[0] if section else 0)
    end = start + (section[1] if section else (length or time.time() - start))

    messages, cursor = [], int(end * 1e6)
    while True:
        page = (_get_json(session, KICK_HISTORY.format(channel_id=channel_id), stopped,
                           cursor=cursor) or {}).get("data") or {}
        batch = page.get("messages") or []
        oldest = None
        for message in batch:
            try:
                sent = _kick_time(message["created_at"])
            except (KeyError, ValueError):
                continue
            oldest = sent if oldest is None else min(oldest, sent)
            if begin <= sent <= end:
                messages.append(message)
        try:
            following = int(page.get("cursor") or 0)
        except ValueError:
            following = 0
        if not batch or oldest is None or oldest < begin or not following or following >= cursor:
            break
        cursor = following
        if progress and end > begin:
            progress(max(0, min(99, int((end - oldest) * 100 / (end - begin)))))
        time.sleep(PAGE_PAUSE)

    records = [{"type": "start", "ts": begin, "platform": "kick", "room_id": str(channel_id)}]
    described = set()
    seen = set()
    parsed = []
    for message in messages:
        if message.get("id") not in seen:
            seen.add(message.get("id"))
            parsed.append(chatreaders.parse_kick_message(message, subscriber_badges))
    # страницы идут от конца к началу — по порядку отправки
    for record, emotes, badges in sorted(parsed, key=lambda item: item[0]["ts"]):
        _described(records, "emote", emotes, described)
        _described(records, "badge", badges, described)
        records.append(record)
    return records


# ------------------------------------------------------------ VK Video Live

def _vk(url, section, duration, progress, stopped, session):
    clip = VK_CLIP.search(url)
    match = VK_RECORD.search(url)
    if clip:
        blog = clip.group(1)
        info = ((_get_json(session, VK_CLIP_INFO.format(blog=quote(blog), clip=clip.group(2).lower()),
                           stopped) or {}).get("data") or {}).get("clip") or {}
        try:
            record, offset = str(info["recordId"]), float(info["offsetStart"])
            length = float(info.get("offsetEnd") or 0) - offset or float(info.get("duration") or 0)
        except (KeyError, TypeError, ValueError):
            raise chat.ChatError(tr("VK Video Live не сказал, из какой записи этот клип"))
        begin = offset + (section[0] if section else 0)
        end = offset + (section[1] if section else length)
        duration = end - begin
    elif match:
        blog, record = match.group(1), match.group(2)
        begin = section[0] if section else 0.0
        end = section[1] if section else None
    else:
        raise chat.ChatError(tr("не понял, какая это запись VK Video Live: {url}", url=url))
    address = VK_RECORD_CHAT.format(blog=quote(blog), record=record.lower())

    # ответ — VK_PAGE сообщений до места и столько же после: идём вперёд
    # по «после», пока их не меньше полной страницы
    messages, seen, position = [], set(), int(begin)
    while True:
        batch = (_get_json(session, address, stopped, limit=VK_PAGE, time_code=position) or {}).get("data") or []
        after = sorted((message for message in batch if isinstance(message.get("timeCode"), (int, float))
                        and message["timeCode"] >= position), key=lambda message: message["timeCode"])
        for message in after:
            if message.get("id") not in seen and not message.get("isDeleted") and not message.get("isPrivate"):
                seen.add(message.get("id"))
                messages.append(message)
        if len(after) < VK_PAGE:
            break
        following = int(after[-1]["timeCode"])
        # больше страницы сообщений в одну секунду — шагнуть за неё
        position = following if following > position else position + 1
        if end is not None and position > end:
            break
        if progress and duration:
            progress(max(0, min(99, int((position - begin) * 100 / max(1, (end or duration) - begin)))))
        time.sleep(PAGE_PAUSE)

    messages = [message for message in messages
                if message["timeCode"] >= begin and (end is None or message["timeCode"] <= end)]
    gaps = [float(message["createdAt"]) - message["timeCode"] for message in messages if message.get("createdAt")]
    base = statistics.median(gaps) if gaps else time.time()
    records = [{"type": "start", "ts": base + begin, "platform": "vkvideolive", "channel": blog}]
    described = set()
    for message in messages:
        record, emotes, badges = chatreaders.parse_vk_message(message)
        record["ts"] = base + message["timeCode"]
        _described(records, "emote", emotes, described)
        _described(records, "badge", badges, described)
        records.append(record)
    return records
