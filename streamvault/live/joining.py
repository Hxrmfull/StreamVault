# -*- coding: utf-8 -*-
"""
Склейка начала эфира с записью.

Запись, начатая посреди эфира, добирает его начало из записи на площадке
(live/beginning.py): рядом с записью ложится «… - part00». Когда готовы
обе части — начало скачано вместе с чатом, запись обработана и её чат
доделан, — они склеиваются в один файл без перекодирования, а части
удаляются. Выключено (настройка join_beginning, общая или у канала) —
части остаются рядом и играют в окне подряд.

Стык ищется по кадрам, а не по часам: первый кадр записи находится
в последних секундах начала (уменьшенные серые кадры, наименьшая разница),
и начало обрезается ровно перед ним — без повтора и без дыры. Начало
скачано с запасом в 15 секунд как раз для этого.

Не сошлось — размер кадра разный, стык не найден, места на диске мало —
части остаются как есть, а в журнале сказано почему.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading

from .. import util
from ..archive import chat as archive_chat
from ..i18n import tr
from ..settings import channel_setting
from .beginning import OVERLAP, clock

PART00 = " - part00"
SCAN_INTERVAL = 60          # как часто искать готовые пары, секунды
# Стык ищется в последних SEARCH_BEFORE секундах начала перед тем местом, где
# по оценке начинается запись (начало скачано на OVERLAP секунд дальше).
SEARCH_BEFORE = 30
THUMB = (32, 18)            # размер кадра для сравнения
FRAMES_TO_MATCH = 3         # столько первых кадров записи ищется подряд
MATCH_LIMIT = 10.0          # средняя разница яркости (0–255), выше — «не то место»
VIDEO_EXTENSIONS = (".mp4", ".ts", ".mkv", ".webm")


# ------------------------------------------------------------ кадры и стык

def _thumbs_command(ffmpeg, path, start=None, length=None, count=None):
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info"]
    if start:
        command += ["-ss", "%.3f" % start]
    command += ["-copyts", "-i", path, "-map", "0:v:0"]
    if length:
        command += ["-t", "%.3f" % length]
    if count:
        command += ["-frames:v", str(count)]
    return command + ["-vf", "scale=%d:%d:flags=area,format=gray,showinfo" % THUMB, "-f", "rawvideo", "pipe:1"]


def thumbnails(ffmpeg, path, start=None, length=None, count=None):
    """[(время кадра, байты уменьшенного серого кадра)] — время от начала файла."""
    result = subprocess.run(_thumbs_command(ffmpeg, path, start, length, count), capture_output=True,
                            timeout=600, creationflags=util.subprocess_flags())
    size = THUMB[0] * THUMB[1]
    times = []
    for line in result.stderr.decode("utf-8", "replace").splitlines():
        if "Parsed_showinfo" in line and "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except (IndexError, ValueError):
                continue
    data = result.stdout
    frames = [data[index * size:(index + 1) * size] for index in range(len(data) // size)]
    return list(zip(times, frames))


def _difference(left, right):
    return sum(abs(a - b) for a, b in zip(left, right)) / max(1, len(left))


def find_seam(ffmpeg, head, tail, estimate):
    """
    Где в начале (head) стоит первый кадр записи (tail): секунды от начала
    head или None. estimate — где он примерно (конец начала минус запас).
    """
    wanted = [frame for _, frame in thumbnails(ffmpeg, tail, count=FRAMES_TO_MATCH)]
    if len(wanted) < 1:
        return None
    # -copyts: время кадров — как в самом файле; так же его понимает outpoint склейки
    window = thumbnails(ffmpeg, head, start=max(0.0, estimate - SEARCH_BEFORE))
    if len(window) < len(wanted):
        return None
    best, best_at = None, None
    for index in range(len(window) - len(wanted) + 1):
        score = sum(_difference(window[index + k][1], wanted[k]) for k in range(len(wanted))) / len(wanted)
        if best is None or score < best:
            best, best_at = score, window[index][0]
    if best is None or best > MATCH_LIMIT:
        logging.info(tr("стык начала и записи не найден (разница {score})", score=round(best or -1, 1)))
        return None
    return best_at


# ------------------------------------------------------------ склейка

def _concat_line(path):
    # полный путь: относительный concat искал бы рядом со списком, во временной папке
    return "file '%s'" % os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")


def join_command(ffmpeg, listing, output, maps=("0:v:0?", "0:a:0?")):
    container = "mpegts" if output.lower().endswith(".ts") else "mp4"
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", listing]
    for stream in maps:
        command += ["-map", stream]
    command += ["-c", "copy"]
    if container == "mp4":
        command += ["-movflags", "+faststart"]
    return command + ["-f", container, output]


def streams(ffmpeg, path):
    """
    Видео и звук файла в том порядке, в каком они в нём лежат, с кодеками:
    [("audio", "aac"), ("video", "h264")].
    """
    result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True, timeout=120,
                            creationflags=util.subprocess_flags())
    found = []
    for line in result.stderr.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("Stream #0:"):
            continue
        for kind, word in (("video", ": Video: "), ("audio", ": Audio: ")):
            if word in line and kind not in dict(found):
                found.append((kind, line.split(word, 1)[1].split()[0].strip(",")))
    return found


def stream_kinds(ffmpeg, path):
    """Только порядок дорожек: ["audio", "video"]."""
    return [kind for kind, _ in streams(ffmpeg, path)]


def _run(command):
    result = subprocess.run(command, capture_output=True, timeout=6 * 3600, creationflags=util.subprocess_flags())
    if result.returncode != 0:
        lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OSError(lines[-1] if lines else "ffmpeg: %d" % result.returncode)


def _ffprobe(ffmpeg):
    """ffprobe лежит рядом с ffmpeg (сборки gyan.dev, своя копия программы) или в PATH."""
    folder = os.path.dirname(ffmpeg)
    for name in ("ffprobe.exe", "ffprobe"):
        if folder and os.path.isfile(os.path.join(folder, name)):
            return os.path.join(folder, name)
    return shutil.which("ffprobe")


def decode_time(ffmpeg, path, at):
    """
    Время декодирования кадра, который показывается в секунду at. concat режет
    по нему: у кадра за стыком оно раньше, чем время показа (из-за B-кадров),
    и outpoint по времени показа оставил бы в начале кадры уже из записи.
    Не узнать — at.
    """
    ffprobe = _ffprobe(ffmpeg)
    if not ffprobe:
        return at
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals",
                             "%.3f%%%.3f" % (max(0.0, at - 3), at + 3), "-show_entries", "packet=pts_time,dts_time",
                             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=120,
                            creationflags=util.subprocess_flags())
    best = None
    for line in result.stdout.splitlines():
        try:
            pts, dts = (float(value) for value in line.strip().strip(",").split(",")[:2])
        except ValueError:
            continue
        if best is None or abs(pts - at) < abs(best[0] - at):
            best = (pts, dts)
    return best[1] if best and abs(best[0] - at) < 0.02 else at


def join_video(ffmpeg, head, cut, tail, output):
    """
    head до секунды cut, затем tail целиком — в output, без перекодирования.

    Сначала начало обрезается по стыку и перепаковывается в контейнер записи
    с тем же порядком дорожек: concat сопоставляет дорожки по номеру, а не по
    типу (у записи Twitch первой идёт звук, у скачанного начала — видео), и не
    согласует шаг времени mp4 и .ts. outpoint — время декодирования кадра
    стыка (decode_time), duration — сам стык: с него начинается tail.
    """
    order = stream_kinds(ffmpeg, tail) or ["video", "audio"]
    maps = ["0:%s:0" % kind[0] for kind in order]
    trim = "outpoint %.6f\nduration %.6f\n" % (decode_time(ffmpeg, head, cut), cut)
    cut_head = output + ".head" + os.path.splitext(tail)[1]
    listings = []
    try:
        listings.append(_listing("%s\n%s" % (_concat_line(head), trim)))
        _run(join_command(ffmpeg, listings[-1], cut_head, maps))
        listings.append(_listing("%s\n%s\n" % (_concat_line(cut_head), _concat_line(tail))))
        _run(join_command(ffmpeg, listings[-1], output, maps))
        if not os.path.isfile(output):
            raise OSError("ffmpeg: нет файла")
    except BaseException:
        util.remove_quietly(output)
        raise
    finally:
        for listing in listings:
            util.remove_quietly(listing)
        util.remove_quietly(cut_head)


def _listing(lines):
    """Список для concat во временном файле: путь к нему."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as listing:
        listing.write("ffconcat version 1.0\n" + lines)
    return listing.name


def _merge_embedded(first, second):
    """Смайлы и значки обоих чатов: списки по ключам, без повторов."""
    if not first:
        return second
    if not second:
        return first
    merged = dict(first)
    for key, values in second.items():
        if isinstance(values, list) and isinstance(merged.get(key), list):
            seen = {json.dumps(item, sort_keys=True) for item in merged[key]}
            merged[key] = merged[key] + [item for item in values if json.dumps(item, sort_keys=True) not in seen]
        elif key not in merged:
            merged[key] = values
    return merged


def join_chat(head_json, tail_json, cut, length, target):
    """
    Чат склейки: сообщения начала до стыка и сообщения записи со сдвигом
    на длину начала. Чата нет ни у одной части — None. Возвращает число сообщений.
    """
    parts = []
    for path in (head_json, tail_json):
        try:
            with open(path, encoding="utf-8") as source:
                parts.append(json.load(source))
        except (OSError, ValueError):
            parts.append(None)
    head, tail = parts
    if head is None and tail is None:
        return None
    comments = []
    if head is not None:
        comments += [dict(comment) for comment in head.get("comments") or ()
                     if float(comment.get("content_offset_seconds", 0)) < cut]
    if tail is not None:
        for comment in tail.get("comments") or ():
            comment = dict(comment)
            comment["content_offset_seconds"] = round(float(comment.get("content_offset_seconds", 0)) + cut, 3)
            comments.append(comment)
    result = dict(head or tail)
    result["comments"] = comments
    result["video"] = dict((head or tail).get("video") or {}, start=0, end=round(length), length=round(length))
    result["embeddedData"] = _merge_embedded((head or {}).get("embeddedData"), (tail or {}).get("embeddedData"))
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False)
    os.replace(temporary, target)
    return len(comments)


# ------------------------------------------------------------ кто склеивает

def beginning_parts(root_path):
    """[(начало, запись)] — пары в папке готовых записей, у которых есть оба файла."""
    pairs = []
    for folder, _, names in os.walk(os.path.join(root_path, util.FINISHED_DIR)):
        for name in names:
            stem, extension = os.path.splitext(name)
            if not stem.endswith(PART00) or extension.lower() not in VIDEO_EXTENSIONS:
                continue
            base = os.path.join(folder, stem[:-len(PART00)])
            main = next((base + ext for ext in (".mp4", ".ts") if os.path.isfile(base + ext)), None)
            if main:
                pairs.append((os.path.join(folder, name), main))
    return pairs


class Joiner(threading.Thread):
    """
    Раз в минуту ищет пары «начало + запись» и склеивает готовые.
    busy(путь) — часть ещё занята: начало качается, запись пишется или
    обрабатывается, её чат доделывается. ffmpeg() — путь к ffmpeg или None.
    """

    def __init__(self, settings, ffmpeg, busy, chat_finisher=None, stop_event=None, interval=SCAN_INTERVAL):
        super().__init__(name="beginning-join", daemon=True)
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._busy = busy
        self._chat = chat_finisher
        self._stop = stop_event or threading.Event()
        self._interval = interval
        self._failed = set()        # пары, которые не склеились: до перезапуска не трогаются

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self._interval):
            try:
                self.scan()
            except Exception:
                logging.exception(tr("склейка начала эфира: сбой"))

    def enabled(self, path):
        platform, label = util.recording_channel(self._settings["root_path"], path)
        return bool(channel_setting(self._settings, "join_beginning", platform, label))

    def scan(self):
        for head, main in beginning_parts(self._settings["root_path"]):
            if self._stop.is_set():
                return
            if (head, main) in self._failed or not self.enabled(main):
                continue
            if self._busy(head) or self._busy(main):
                continue
            if not self.join(head, main):
                self._failed.add((head, main))

    def join(self, head, main):
        """Склеивает пару; True — склеено, False — части остались как есть."""
        from ..editing import probe
        ffmpeg = self._ffmpeg()
        name = os.path.basename(main)
        if not ffmpeg:
            return False
        head_length, head_size = probe(ffmpeg, head)
        main_length, main_size = probe(ffmpeg, main)
        if not head_length or not main_length:
            logging.warning(tr("начало эфира не склеено с записью {name}: не прочитать длительность", name=name))
            return False
        if head_size != main_size:
            logging.warning(tr("начало эфира не склеено с записью {name}: размер кадра разный ({head} и {main}) — "
                               "части остались рядом", name=name, head="%dx%d" % head_size if head_size else "?",
                               main="%dx%d" % main_size if main_size else "?"))
            return False
        # без перекодирования склеивается только одно и то же: опубликованная
        # запись YouTube бывает в другом кодеке, чем эфир
        head_codecs, main_codecs = dict(streams(ffmpeg, head)), dict(streams(ffmpeg, main))
        if head_codecs != main_codecs:
            logging.warning(tr("начало эфира не склеено с записью {name}: кодеки разные ({head} и {main}) — "
                               "части остались рядом", name=name,
                               head=", ".join(sorted(head_codecs.values())) or "?",
                               main=", ".join(sorted(main_codecs.values())) or "?"))
            return False
        needed = os.path.getsize(head) + os.path.getsize(main)
        if shutil.disk_usage(os.path.dirname(main)).free < needed * 1.1:
            logging.warning(tr("начало эфира не склеено с записью {name}: мало места на диске", name=name))
            return False
        cut = find_seam(ffmpeg, head, main, max(0.0, head_length - OVERLAP))
        if cut is None:
            logging.warning(tr("начало эфира не склеено с записью {name}: стык не найден — части остались рядом",
                               name=name))
            return False
        stem, extension = os.path.splitext(main)
        output = stem + ".joining" + extension
        logging.info(tr("склеиваю начало эфира с записью: {name}", name=name))
        try:
            join_video(ffmpeg, head, cut, main, output)
        except (OSError, subprocess.TimeoutExpired) as error:
            util.remove_quietly(output)
            logging.error(tr("начало эфира не склеилось с записью {name}: {error}", name=name, error=error))
            return False
        joined_length = probe(ffmpeg, output)[0]
        if not joined_length or abs(joined_length - (cut + main_length)) > 3:
            util.remove_quietly(output)
            logging.error(tr("начало эфира не склеилось с записью {name}: длина вышла {got} вместо {want}",
                             name=name, got=joined_length, want=round(cut + main_length)))
            return False
        head_chat, main_chat = archive_chat.chat_paths(head), archive_chat.chat_paths(main)
        try:
            count = join_chat(head_chat["json"], main_chat["json"], cut, joined_length, main_chat["json"] + ".joined")
        except (OSError, ValueError) as error:
            util.remove_quietly(output)
            logging.error(tr("чат не склеился: {name} — {error}", name=name, error=error))
            return False
        os.replace(output, main)
        if count is not None:
            os.replace(main_chat["json"] + ".joined", main_chat["json"])
            # HTML и видео чата — заново, по склеенному чату
            for key in ("html", "video"):
                util.remove_quietly(main_chat[key])
        for path in [head, *head_chat.values()]:
            util.remove_quietly(path)
        logging.info(tr("начало эфира склеено с записью: {name} — стык на {at}, вышло {length}",
                        name=name, at=clock(cut), length=clock(joined_length)))
        if count is not None and self._chat is not None:
            self._chat.enqueue(main)
        return True
