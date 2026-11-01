# -*- coding: utf-8 -*-
"""
yt-dlp как внешняя программа: что лежит по ссылке и как это скачать.

yt-dlp запускается отдельным процессом, как streamlink: загрузку можно
погасить в любой момент (пауза, отмена), а сбой внутри него не роняет
программу. Обновляется он сам по себе: python -m pip install -U yt-dlp.
"""

import datetime
import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlencode, urlsplit

from .. import util
from ..platforms import is_url
from ..i18n import tr

# Метки строк, которые yt-dlp печатает по нашим шаблонам.
PROGRESS_MARK = "SVP "
POSTPROCESS_MARK = "SVPP "
FILE_MARK = "SVFILE "

# Сколько ждать ответа на вопрос "что по этой ссылке".
INSPECT_TIMEOUT = 120

# Приставка качества: сначала те же кодеки, что у записи эфира (format_args).
H264 = "h264:"

# ffmpeg переподключается к тому же месту, если площадка оборвала соединение.
FFMPEG_RECONNECT = "-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 -reconnect_delay_max 10"

# Вкладки страницы канала (ссылки на списки записей) — в модулях площадок:
# sites.listings(). В клипах Twitch {period} — за какой срок брать самые
# просматриваемые.
CLIP_PERIODS = ("24hr", "7d", "30d", "all")

# Начало имени экстрактора yt-dlp -> платформа программы.
_EXTRACTOR_PLATFORMS = (("twitch", "twitch"), ("kick", "kick"),
                        ("youtube", "youtube"), ("vkplay", "vkvideolive"))

# Экстрактор -> что это за видео.
_EXTRACTOR_KINDS = {"twitchvod": "vod", "kickvod": "vod", "vkplay": "vod", "vkplayclip": "clip",
                    "twitchclips": "clip", "kickclip": "clip"}

# Постобработка, которая ничего не перекодирует и мгновенна.
_TRIVIAL_POSTPROCESSORS = ("MoveFiles",)


class YtdlpError(Exception):
    """yt-dlp не справился. Текст — для человека."""


def platform_of(extractor_key):
    key = (extractor_key or "").lower()
    for prefix, platform in _EXTRACTOR_PLATFORMS:
        if key.startswith(prefix):
            return platform
    return key or "other"


def kind_of(extractor_key):
    return _EXTRACTOR_KINDS.get((extractor_key or "").lower(), "video")


def listing_url(platform, channel, kind="vods", period="all"):
    """Ссылка на список записей канала. Неподдерживаемое сочетание — ValueError."""
    from .. import sites
    kinds = sites.listings(platform)
    if not kinds:
        raise ValueError(tr("для {platform} список записей канала не поддерживается — "
                            "вставь ссылку на саму запись", platform=platform))
    if kind not in kinds:
        raise ValueError(tr("у {platform} нет списка «{kind}», есть: {kinds}",
                            platform=platform, kind=kind, kinds=", ".join(kinds)))
    if is_url(channel):
        raise ValueError(tr("нужно имя канала, а не ссылка: {link}", link=channel))
    if period not in CLIP_PERIODS:
        raise ValueError(tr("срок для клипов: {periods}", periods=", ".join(CLIP_PERIODS)))
    return kinds[kind].format(channel=channel.lstrip("@"), period=period)


def _clean_url(url):
    """Служебный хвост #__youtubedl_smuggle, который yt-dlp добавляет в списках."""
    return (url or "").split("#__youtubedl_smuggle", 1)[0]


# Превью записи Twitch: .../cf_vods/<узел>/<хеш>_<канал>_<номер эфира>_<начало, unix>/.../thumb0-320x180.jpg
_TWITCH_VOD_THUMB = re.compile(r"static-cdn\.jtvnw\.net/cf_vods/[^/]+/[0-9a-f]+_[^/]+_\d+_(\d{9,11})/")
_TWITCH_THUMB_SIZE = re.compile(r"-320x180\.(jpg|png)$")


def _thumbnail(info):
    url = info.get("thumbnail")
    if not url:
        thumbnails = [t for t in info.get("thumbnails") or [] if t.get("url")]
        # yt-dlp сортирует превью от худшего к лучшему
        url = thumbnails[-1]["url"] if thumbnails else ""
    # Twitch в списке записей отдаёт 320x180, а карточка шире — и на экранах
    # с увеличением кадр выходил мыльным. Тот же кадр бывает любого размера.
    if "static-cdn.jtvnw.net/" in url:
        url = _TWITCH_THUMB_SIZE.sub(r"-640x360.\1", url)
    return url


def _timestamp(info):
    for key in ("timestamp", "release_timestamp"):
        if isinstance(info.get(key), (int, float)):
            return info[key]
    # в списке записей Twitch даты нет, но начало эфира записано в адресе превью
    match = _TWITCH_VOD_THUMB.search(info.get("thumbnail") or "")
    if match:
        return int(match.group(1))
    date = info.get("upload_date")
    if date:
        try:
            moment = datetime.datetime.strptime(date, "%Y%m%d")
            return moment.replace(tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            pass
    return None


# в адресе записи или клипа имя канала идёт первым: kick.com/<канал>/videos/…,
# live.vkvideo.ru/<канал>/record/…, twitch.tv/<канал>/clip/…
_CHANNEL_PATH_KINDS = ("videos", "video", "clips", "clip", "record")


def channel_folder(info, platform, url):
    """
    Папка канала для скачанного — тот же идентификатор, что у записей его
    эфиров (логин, @имя без @, имя в адресе), а не отображаемое имя: иначе
    записи и скачанное одного канала лежали бы в разных папках («LofiGirl»
    и «Lofi Girl»). Не понять — пусто: тогда папка по имени канала.
    """
    path = [part for part in urlsplit(url or "").path.split("/") if part]
    if len(path) >= 3 and path[1].lower() in _CHANNEL_PATH_KINDS:
        return path[0]
    uploader_id = str(info.get("uploader_id") or "")
    if platform == "youtube" and uploader_id.startswith("@"):
        return uploader_id[1:]
    # у записи эфира Twitch uploader_id — логин канала; у клипа — автор клипа
    if platform == "twitch" and (info.get("ie_key") or info.get("extractor_key")) == "TwitchVod":
        return uploader_id
    return ""


def describe_entry(info):
    """Одно видео в виде, удобном окну программы. Годится и для записей списка."""
    extractor = info.get("ie_key") or info.get("extractor_key")
    live_status = info.get("live_status")
    url = _clean_url(info.get("webpage_url") or info.get("url"))
    return {
        "id": str(info.get("id") or ""),
        "url": url,
        "title": info.get("title") or "",
        "platform": platform_of(extractor),
        "kind": kind_of(extractor),
        # у клипа Twitch uploader — автор клипа, а канал — в channel
        "channel": info.get("channel") or info.get("uploader") or info.get("uploader_id") or "",
        "folder": channel_folder(info, platform_of(extractor), url),
        "duration": info.get("duration"),
        "thumbnail": _thumbnail(info),
        "timestamp": _timestamp(info),
        "view_count": info.get("view_count"),
        "is_live": bool(info.get("is_live")) or live_status == "is_live",
        # was_live — прошедший эфир: у него бывает чат
        "live_status": live_status or "",
    }


def qualities(info):
    """
    Варианты качества для выбора: по одному на высоту кадра, от лучшего.
    Видео и звук YouTube приходят отдельно — склеит их yt-dlp.
    """
    heights = {}
    has_audio_only = False
    for fmt in info.get("formats") or []:
        if fmt.get("ext") == "mhtml":
            continue   # раскадровка для перемотки, а не видео
        height = fmt.get("height")
        if fmt.get("vcodec") == "none":
            has_audio_only = has_audio_only or fmt.get("acodec") not in (None, "none")
            continue
        if not height:
            continue
        fps = int(round(fmt.get("fps") or 0))
        heights[height] = max(heights.get(height, 0), fps)

    options = [{"quality": "best", "label": tr("лучшее")}]
    for height in sorted(heights, reverse=True):
        fps = heights[height]
        label = "%dp%s" % (height, fps if fps > 30 else "")
        options.append({"quality": str(height), "label": label})
    if has_audio_only:
        options.append({"quality": "audio", "label": tr("только звук")})
    return options


def describe(data):
    """Ответ yt-dlp -J: одно видео (с вариантами качества) или список."""
    if data.get("_type") in ("playlist", "multi_video"):
        entries = [describe_entry(entry) for entry in data.get("entries") or [] if entry]
        return {
            "type": "playlist",
            "title": data.get("title") or "",
            "url": data.get("webpage_url") or data.get("original_url") or "",
            "platform": platform_of(data.get("extractor_key")),
            "entries": entries,
        }
    result = describe_entry(data)
    result["type"] = "video"
    result["qualities"] = qualities(data)
    return result


def _last_error(output):
    """Последняя строка "ERROR: ..." из вывода yt-dlp, без служебного префикса."""
    errors = [line[len("ERROR:"):].strip() for line in output.splitlines()
              if line.startswith("ERROR:")]
    return errors[-1] if errors else ""


def run_json(args, timeout=INSPECT_TIMEOUT, env=None):
    """
    Запускает yt-dlp и разбирает JSON из его вывода. Вывод идёт во временные
    файлы: ответ про большой плейлист YouTube весит мегабайты. env — окружение
    (с обновлённым yt-dlp, если он есть).
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen([*util.YTDLP, *args], stdout=out, stderr=err,
                                       stdin=subprocess.DEVNULL,
                                       env=env or util.child_environment(),
                                       creationflags=util.subprocess_flags())
        except OSError as error:
            raise YtdlpError(tr("не удалось запустить yt-dlp: {error}", error=error))
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            util.kill_process_tree(process)
            process.wait(10)
            raise YtdlpError(tr("yt-dlp не ответил за {seconds} с", seconds=timeout))
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")

    if process.returncode != 0:
        if re.search(r"No module named '?yt_dlp", stderr):
            raise YtdlpError(tr("yt-dlp не установлен: python -m pip install yt-dlp"))
        raise YtdlpError(_last_error(stderr) or stderr.strip()[-300:]
                         or tr("yt-dlp завершился с кодом {code}", code=process.returncode))
    try:
        return json.loads(stdout)
    except ValueError:
        raise YtdlpError(tr("yt-dlp ответил непонятно"))


def inspect(url, limit=None, start=1, timeout=INSPECT_TIMEOUT, extra_args=None, env=None):
    """
    Что лежит по ссылке. Для списка (канал, плейлист) — записи без подробностей,
    limit штук начиная со start: так страница канала открывается за секунды.
    """
    args = ["-J", "--flat-playlist", "--no-playlist", "--no-warnings",
            "--color", "never"]
    if limit:
        args += ["-I", "%d:%d" % (start, start + limit - 1)]
    args += list(extra_args or [])
    return describe(run_json(args + ["--", url], timeout, env))


def youtube_channel_search(query, limit=5, timeout=30, env=None):
    """
    Каналы YouTube по названию — страница поиска с отбором «Каналы».
    Сырые записи yt-dlp: в них @имя, число подписчиков и аватарка.
    """
    url = "https://www.youtube.com/results?" + urlencode({"search_query": query, "sp": "EgIQAg=="})
    data = run_json(["-J", "--flat-playlist", "--no-warnings", "--color", "never",
                     "-I", "1:%d" % limit, "--", url], timeout, env)
    return data.get("entries") or []


_TOKEN_PREFIXES = ("auth-token=", "oauth:", "oauth ", "bearer ")


def normalize_auth_token(text):
    """
    Токен Twitch в том виде, как его скопировали: с "auth-token=", "OAuth "
    или без. Пусто — токена нет. Чужие символы — ValueError: в файле кук
    табуляция или перевод строки сломали бы его разбор.
    """
    token = (text or "").strip()
    lowered = token.lower()
    for prefix in _TOKEN_PREFIXES:
        if lowered.startswith(prefix):
            token = token[len(prefix):].strip()
            break
    if token and not re.fullmatch(r"[A-Za-z0-9]+", token):
        raise ValueError(tr("токен Twitch состоит только из латинских букв и цифр — "
                            "скопируй значение куки auth-token целиком"))
    return token


def write_cookie_file(token):
    """
    Файл кук для yt-dlp с одной кукой auth-token: по ней Twitch пускает
    к записям только для подписчиков. Файл свой у каждого запуска: yt-dlp
    дописывает в него куки при выходе, и параллельные загрузки портили бы общий.
    """
    handle, path = tempfile.mkstemp(prefix="streamvault-cookies-", suffix=".txt")
    expires = int(time.time()) + 365 * 86400
    with os.fdopen(handle, "w", encoding="utf-8") as target:
        target.write("# Netscape HTTP Cookie File\n")
        target.write(".twitch.tv\tTRUE\t/\tTRUE\t%d\tauth-token\t%s\n" % (expires, token))
    return path


def format_args(quality):
    """
    Качество -> выбор формата yt-dlp: best, worst, audio, высота (720 или 720p)
    или прямо id формата, как его называет платформа ("1080p60" у Twitch).
    Приставка "h264:" ("h264:1080", "h264:best") — сначала H.264 со звуком AAC,
    как у записи эфира: начало эфира склеивается с ней только при тех же
    кодеках, а YouTube по «лучшему» отдаёт VP9 или AV1. Нет такого — как без неё.
    """
    value = (quality or "best").strip()
    lowered = value.lower()
    if lowered.startswith(H264):
        rest = value[len(H264):]
        if rest.lower() == "best":
            limit = ""
        elif re.fullmatch(r"\d{3,4}p?", rest.lower()):
            limit = "[height<=%d]" % int(rest.lower().rstrip("p"))
        else:
            return format_args(rest)
        same = "bv*[vcodec^=avc1]{0}+ba[acodec^=mp4a]/b[vcodec^=avc1]{0}/".format(limit)
        return ["-f", same + format_args(rest)[1]]
    if lowered == "best":
        return ["-f", "bv*+ba/b"]
    if lowered == "worst":
        return ["-f", "wv*+wa/w"]
    if lowered == "audio":
        return ["-f", "ba/b", "-x", "--audio-format", "m4a"]
    if re.fullmatch(r"\d{3,4}p?", lowered):
        height = int(lowered.rstrip("p"))
        # нет ничего не выше заданного — берём самое скромное, а не падаем
        return ["-f", "bv*[height<={0}]+ba/b[height<={0}]/wv*+ba/w".format(height)]
    return ["-f", value]


def download_command(url, base, quality, section, fragments, ffmpeg_path, extra_args):
    """
    Команда загрузки. base — путь без расширения: его подберёт yt-dlp.
    section — (начало, конец) в секундах, чтобы скачать только отрезок.
    ffmpeg_path — папка с ffmpeg или None: тогда yt-dlp ищет его сам.
    """
    args = [*util.YTDLP,
            "--newline", "--progress", "--color", "never", "--no-playlist", "--no-mtime",
            "--progress-template", "download:" + PROGRESS_MARK + "%(progress)j",
            "--progress-template", "postprocess:" + POSTPROCESS_MARK + "%(progress)j",
            "--print", "after_move:" + FILE_MARK + "%(filepath)s",
            # при равной высоте — mp4 и m4a: их понимает любой проигрыватель
            "-S", "res,fps,ext:mp4:m4a", "--merge-output-format", "mp4",
            "-N", str(max(1, int(fragments))),
            # % в пути yt-dlp понял бы как начало шаблона
            "-o", base.replace("%", "%%") + ".%(ext)s",
            *format_args(quality)]
    if section:
        # отрезок качает ffmpeg, а YouTube рвёт долгие соединения (WSAECONNRESET, код
        # 4294957242): без переподключения загрузка падала через полминуты
        # ffmpeg и о ходе молчит — -progress пишет в общий вывод, докуда он дошёл
        args += ["--download-sections", "*%s-%s" % (section[0], section[1]),
                 "--downloader-args", "ffmpeg_i:" + FFMPEG_RECONNECT,
                 "--downloader-args", "ffmpeg_o:-progress pipe:1 -stats_period 1"]
    # папка, а не сам ffmpeg.exe: в ней yt-dlp найдёт и ffprobe
    if ffmpeg_path:
        args += ["--ffmpeg-location", ffmpeg_path]
    args += list(extra_args or [])
    args += ["--", url]
    return args


def parse_progress(line):
    """
    Строка вывода загрузки -> (вид, данные). Виды: progress, postprocess,
    file, error, ffmpeg (ход ffmpeg, качающего отрезок: {"time": с} или
    {"size": байт}), other.
    """
    key, separator, value = line.partition("=")
    if separator and value.isdigit():
        if key == "out_time_us":
            return "ffmpeg", {"time": int(value) / 1e6}
        if key == "total_size":
            return "ffmpeg", {"size": int(value)}
    for mark, kind in ((PROGRESS_MARK, "progress"), (POSTPROCESS_MARK, "postprocess")):
        if line.startswith(mark):
            try:
                return kind, json.loads(line[len(mark):])
            except ValueError:
                return "other", line
    if line.startswith(FILE_MARK):
        return "file", line[len(FILE_MARK):].strip()
    if line.startswith("ERROR:"):
        return "error", line[len("ERROR:"):].strip()
    return "other", line


def progress_of(data):
    """Прогресс yt-dlp -> доля, байты, скорость, сколько осталось."""
    downloaded = data.get("downloaded_bytes")
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    fragment, fragments = data.get("fragment_index"), data.get("fragment_count")
    percent = None
    if downloaded is not None and total:
        percent = min(100.0, downloaded * 100.0 / total)
    elif fragment and fragments:
        percent = min(100.0, fragment * 100.0 / fragments)
    if data.get("status") == "finished":
        percent = 100.0
    return {
        "percent": percent,
        "downloaded": downloaded,
        "total": total,
        "speed": data.get("speed"),
        "eta": data.get("eta"),
    }


def is_heavy_postprocessor(data):
    """Постобработка, которую стоит показать как отдельный этап: склейка, перепаковка."""
    return data.get("postprocessor") not in _TRIVIAL_POSTPROCESSORS


_TIME = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_time(text):
    """'1:02:03', '62:03', '3723' -> секунды."""
    match = _TIME.match(text.strip())
    if not match:
        raise ValueError(tr("не понимаю время: «{text}» (нужно Ч:ММ:СС, ММ:СС или секунды)",
                            text=text))
    parts = [part for part in match.groups() if part is not None]
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def parse_section(text):
    """'1:00:00-1:30:00' -> (3600, 5400)."""
    start, separator, end = text.partition("-")
    if not separator:
        raise ValueError(tr("отрезок пишется как НАЧАЛО-КОНЕЦ, например 1:00:00-1:30:00"))
    section = (parse_time(start), parse_time(end))
    if section[1] <= section[0]:
        raise ValueError(tr("конец отрезка должен быть позже начала"))
    return section
