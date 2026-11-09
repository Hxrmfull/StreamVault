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

import array
import json
import logging
import math
import os
import re
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
# Склейка пишется сюда и лишь потом заменяет запись.
JOINING = ".joining"
# Её файлы, оставшиеся от прерванной склейки: «….joining.ts» и «….joining.ts.head.ts».
LEFTOVER = re.compile(r"\.joining(\.tail)?\.(ts|mp4|mkv|m4a)(\.head\.(ts|mp4|mkv|m4a))?$", re.IGNORECASE)
SCAN_INTERVAL = 60          # как часто искать готовые пары, секунды
# Стык ищется в последних SEARCH_BEFORE секундах начала перед тем местом, где
# по оценке начинается запись (начало скачано на OVERLAP секунд дальше).
SEARCH_BEFORE = 30
THUMB = (32, 18)            # размер кадра для сравнения
FRAMES_TO_MATCH = 3         # столько первых кадров записи ищется подряд
MATCH_LIMIT = 10.0          # средняя разница яркости (0–255), выше — «не то место»
VIDEO_EXTENSIONS = (".mp4", ".ts", ".mkv", ".webm")
# Запись только со звуком (конвертация «только звук»): стык ищется по громкости.
ENVELOPE_RATE = 1000        # звук для сравнения — моно, столько отсчётов в секунду
ENVELOPE_STEP = 10          # громкость — по столько отсчётов (10 мс)
COARSE = 5                  # грубый поиск — по столько шагов громкости (50 мс)
SOUND_TO_MATCH = 10         # столько первых секунд записи ищется в начале
SOUND_LIMIT = 0.8           # совпадение громкости (корреляция) ниже — «не то место»
# Запись открыта (смотрят в окне программы или в проигрывателе) — Windows не даёт
# подменить её склейкой и удалить начало: столько ждать, проверяя так часто, секунды
REPLACE_WAIT = 600
REPLACE_STEP = 5


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


def envelope(ffmpeg, path, start=None, length=None):
    """
    Громкость звука по шагам ENVELOPE_STEP отсчётов: [средний модуль]. Время —
    от start (или от начала файла).
    """
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start:
        command += ["-ss", "%.3f" % start]
    command += ["-i", path, "-map", "0:a:0"]
    if length:
        command += ["-t", "%.3f" % length]
    command += ["-ac", "1", "-ar", str(ENVELOPE_RATE), "-f", "s16le", "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=600, creationflags=util.subprocess_flags())
    samples = array.array("h")
    samples.frombytes(result.stdout[:len(result.stdout) // 2 * 2])
    return [sum(abs(value) for value in samples[index:index + ENVELOPE_STEP]) / ENVELOPE_STEP
            for index in range(0, len(samples) - ENVELOPE_STEP + 1, ENVELOPE_STEP)]


def _correlation(window, wanted, at):
    """Корреляция wanted с куском window с позиции at: 1 — совпадает по форме."""
    count = len(wanted)
    part = window[at:at + count]
    mean_a, mean_b = sum(part) / count, sum(wanted) / count
    top = sum((a - mean_a) * (b - mean_b) for a, b in zip(part, wanted))
    low = math.sqrt(sum((a - mean_a) ** 2 for a in part) * sum((b - mean_b) ** 2 for b in wanted))
    return top / low if low else 0.0


def _coarse(values):
    return [sum(values[index:index + COARSE]) / COARSE for index in range(0, len(values) - COARSE + 1, COARSE)]


def find_sound_seam(ffmpeg, head, tail, estimate):
    """
    find_seam для записи без видео: где в начале (head) звучат первые
    SOUND_TO_MATCH секунд записи (tail) — по громкости. Сначала грубо, по
    50 мс, потом точно, по 10 мс. Тишина или ничего похожего — None.
    """
    wanted = envelope(ffmpeg, tail, length=SOUND_TO_MATCH)
    begin = max(0.0, estimate - SEARCH_BEFORE)
    window = envelope(ffmpeg, head, start=begin)
    if len(wanted) < SOUND_TO_MATCH * ENVELOPE_RATE // ENVELOPE_STEP // 2 or len(window) < len(wanted):
        return None
    loudest = max(wanted)
    if not loudest or max(wanted) - min(wanted) < loudest * 0.05:
        logging.info(tr("стык начала и записи не найден: в начале записи тишина"))
        return None
    small_window, small_wanted = _coarse(window), _coarse(wanted)
    scores = [(_correlation(small_window, small_wanted, at), at)
              for at in range(len(small_window) - len(small_wanted) + 1)]
    if not scores:
        return None
    _, rough = max(scores)
    candidates = range(max(0, rough * COARSE - COARSE), min(len(window) - len(wanted), rough * COARSE + COARSE) + 1)
    best, at = max((_correlation(window, wanted, index), index) for index in candidates)
    if best < SOUND_LIMIT:
        logging.info(tr("стык начала и записи не найден (совпадение звука {score})", score=round(best, 2)))
        return None
    return begin + at * ENVELOPE_STEP / ENVELOPE_RATE


# ------------------------------------------------------------ склейка

def _concat_line(path):
    # полный путь: относительный concat искал бы рядом со списком, во временной папке
    return "file '%s'" % os.path.abspath(path).replace("\\", "/").replace("'", "'\\''")


# Контейнер склейки — как у записи.
CONTAINERS = {".ts": "mpegts", ".mkv": "matroska", ".m4a": "ipod"}


def join_command(ffmpeg, listing, output, maps=("0:v:0?", "0:a:0?"), timescale=None):
    """
    timescale — шкала времени видео записи (делений в секунду): у начала из
    HLS она 90 000, у mp4 записи — своя, и concat без перекодирования их не
    согласует — кадры начала и записи перемешивались («60 кадров в секунду»).
    """
    container = CONTAINERS.get(os.path.splitext(output)[1].lower(), "mp4")
    command = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", listing]
    for stream in maps:
        command += ["-map", stream]
    command += ["-c", "copy"]
    if container in ("mp4", "ipod"):
        command += ["-movflags", "+faststart"]
        if timescale:
            command += ["-video_track_timescale", str(timescale)]
    return command + ["-f", container, output]


def mp4_inside_ts(ffmpeg, path):
    """Куски mp4 внутри .ts (util.mp4_inside_ts): склейка в .ts теряла конец такой записи."""
    return util.mp4_inside_ts(ffmpeg, path)


def as_mp4(ffmpeg, path, target):
    """
    Запись — в обычный mp4 без перекодирования, со временем от нуля. Битый
    последний кадр (запись оборвали) не мешает: ошибки не роняют перепаковку.
    """
    try:
        _run([ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-err_detect", "ignore_err",
              *util.SKIP_CORRUPT, "-i", path, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart",
              "-f", "mp4", target])
    except BaseException:
        util.remove_quietly(target)
        raise


def video_timescale(ffmpeg, path):
    """Шкала времени видео файла («15360 tbn» в выводе ffmpeg) или None."""
    result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path], capture_output=True, timeout=120,
                            creationflags=util.subprocess_flags())
    match = re.search(r"Video: .*? (\d+)(k?) tbn", result.stderr.decode("utf-8", "replace"))
    return int(match.group(1)) * (1000 if match.group(2) else 1) if match else None


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


def _run(command, progress=None, length=None):
    """
    ffmpeg; сбой — OSError с его последней строкой. progress(доля 0..1) — по
    ходу работы: ffmpeg сообщает, до какой секунды дошёл (-progress), а
    length — сколько секунд всего.
    """
    if not progress or not length:
        result = subprocess.run(command, capture_output=True, timeout=6 * 3600,
                                creationflags=util.subprocess_flags())
        returncode, errors = result.returncode, result.stderr
    else:
        command = [command[0], "-progress", "pipe:1", "-nostats", *command[1:]]
        with tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr,
                                       creationflags=util.subprocess_flags())
            with process.stdout:
                for raw in process.stdout:
                    key, _, value = raw.decode("ascii", "replace").strip().partition("=")
                    if key == "out_time_us" and value.isdigit():
                        progress(min(1.0, int(value) / 1e6 / length))
            returncode = process.wait()
            stderr.seek(0)
            errors = stderr.read()
    if returncode != 0:
        lines = errors.decode("utf-8", "replace").strip().splitlines()
        raise OSError(lines[-1] if lines else "ffmpeg: %d" % returncode)


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


def join_video(ffmpeg, head, cut, tail, output, progress=None, tail_length=None):
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
    timescale = video_timescale(ffmpeg, tail) if "video" in order else None
    # у записи только со звуком кадров нет: режется ровно по стыку
    outpoint = decode_time(ffmpeg, head, cut) if "video" in order else cut
    trim = "outpoint %.6f\nduration %.6f\n" % (outpoint, cut)
    cut_head = output + ".head" + os.path.splitext(tail)[1]
    listings = []
    # доля склейки — по переписанным секундам: первый проход переписывает начало
    # (cut), второй — всё заново (cut + запись). Раньше второму отводился только
    # хвост, и на нём проценты «вставали» у 85%: он переписывает больше всех
    total = cut + (tail_length or 0)
    work = cut + total
    first = (lambda share: progress(share * cut / work)) if progress and total else None
    second = (lambda share: progress((cut + share * total) / work)) if progress and total else None
    try:
        listings.append(_listing("%s\n%s" % (_concat_line(head), trim)))
        _run(join_command(ffmpeg, listings[-1], cut_head, maps, timescale), first, cut)
        listings.append(_listing("%s\n%s\n" % (_concat_line(cut_head), _concat_line(tail))))
        _run(join_command(ffmpeg, listings[-1], output, maps, timescale), second, total)
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


def _remove_existing(path):
    """Удаляет файл; его уже нет — тоже хорошо."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


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


def join_chat(head_json, tail_json, cut, length, target, shift=None):
    """
    Чат склейки: сообщения начала до стыка и сообщения записи со сдвигом
    на длину начала. Чата нет ни у одной части — None. Возвращает число сообщений.
    shift — насколько сдвинуть сообщения tail; по умолчанию на стык. 0 — tail
    уже склеенная запись: чат начала докачали после склейки, и он встаёт перед ним.
    """
    shift = cut if shift is None else shift
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
            comment["content_offset_seconds"] = round(float(comment.get("content_offset_seconds", 0)) + shift, 3)
            comments.append(comment)
    result = dict(head or tail)
    comments.sort(key=lambda comment: float(comment.get("content_offset_seconds", 0)))
    result["comments"] = comments
    # пропуски чата (chatgaps): у начала — до стыка, у записи — со сдвигом, как сообщения
    from .. import chatgaps
    head_video, tail_video = (head or {}).get("video") or {}, (tail or {}).get("video") or {}
    marks = chatgaps.joined_marks(chatgaps.read_marks(head_video.get("description")) if head else None,
                                  chatgaps.read_marks(tail_video.get("description")) if tail else None, cut, shift)
    result["video"] = dict((head or tail).get("video") or {}, start=0, end=round(length), length=round(length),
                           description=chatgaps.with_marks((head or tail).get("video", {}).get("description"), marks))
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
            main = next((base + ext for ext in util.RECORDING_EXTENSIONS if os.path.isfile(base + ext)), None)
            if main:
                pairs.append((os.path.join(folder, name), main))
    return pairs


class Joiner(threading.Thread):
    """
    Раз в минуту ищет пары «начало + запись» и склеивает готовые.
    busy(путь) — часть ещё занята: начало качается, запись пишется или
    обрабатывается, её чат доделывается. ffmpeg() — путь к ffmpeg или None.
    """

    def __init__(self, settings, ffmpeg, busy, chat_finisher=None, stop_event=None, interval=SCAN_INTERVAL,
                 on_change=None):
        """on_change(событие) — как идёт склейка: {"head", "main", "state": joining | done | failed, …}."""
        super().__init__(name="beginning-join", daemon=True)
        self._settings = settings
        self._ffmpeg = ffmpeg
        self._busy = busy
        self._chat = chat_finisher
        self._stop = stop_event or threading.Event()
        self._interval = interval
        self._failed = set()        # пары, которые не склеились: до перезапуска не трогаются
        self._on_change = on_change

    def stop(self):
        self._stop.set()

    def run(self):
        self.remove_leftovers()
        while not self._stop.wait(self._interval):
            try:
                self.scan()
            except Exception:
                logging.exception(tr("склейка начала эфира: сбой"))

    def remove_leftovers(self):
        """
        Склейку оборвало закрытие программы — её файлы «… .joining.ts» и
        «… .joining.ts.head.ts» (гигабайты) остались. При запуске склейка
        ещё не идёт: их можно убрать, части целы и склеятся заново.
        """
        for folder, _, names in os.walk(os.path.join(self._settings["root_path"], util.FINISHED_DIR)):
            for name in names:
                if LEFTOVER.search(name):
                    path = os.path.join(folder, name)
                    util.remove_quietly(path)
                    logging.info(tr("убран файл прерванной склейки: {name}", name=name))

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
            try:
                joined = self.join(head, main)
            except Exception as error:
                # сбой одной пары (ffmpeg завис, файл пропал) — не повод склеивать её
                # заново каждую минуту: до перезапуска она не трогается, как и не склеенные
                logging.exception(tr("склейка начала эфира: сбой"))
                stem, extension = os.path.splitext(main)
                util.remove_quietly(stem + JOINING + extension)
                self._report(head, main, "failed", reason=str(error))
                joined = False
            if not joined:
                self._failed.add((head, main))

    def _when_free(self, action, path=None, on_wait=None):
        """
        action() — подмена или удаление файла path. Пока файл открыт в другой
        программе, Windows этого не даёт: попытки раз в REPLACE_STEP секунд,
        до REPLACE_WAIT. Бывает, что даёт — новый «Медиаплеер» разрешает
        удалить открытый файл, и подмена оборвала бы просмотр: поэтому сначала
        спрашивается, кто держит path. on_wait(программы) — один раз, когда
        пришлось ждать: окно скажет, что закрыть. True — вышло; False — нет или
        программа закрывается.
        """
        waited = 0
        told = False
        while True:
            try:
                holders = util.file_holders(path) if path is not None else []
                if holders:
                    if on_wait is not None and not told:
                        told = True
                        on_wait(holders)
                    raise PermissionError(path)
                action()
                return True
            except FileNotFoundError:
                raise
            except OSError:
                if waited >= REPLACE_WAIT or self._stop.wait(REPLACE_STEP):
                    return False
                waited += REPLACE_STEP

    def _report(self, head, main, state, **extra):
        if self._on_change is not None:
            self._on_change(dict(extra, head=head, main=main, state=state))

    def _refuse(self, head, main, reason, kept=True, error=False):
        """Пара не склеена: в журнал и окну — почему. kept — части остались рядом."""
        message = tr("начало эфира не склеено с записью {name}: {reason}", name=os.path.basename(main), reason=reason)
        if kept:
            message += tr(" — части остались рядом")
        (logging.error if error else logging.warning)(message)
        self._report(head, main, "failed", reason=reason)
        return False

    def join(self, head, main):
        """Склеивает пару; True — склеено, False — части остались как есть."""
        from ..editing import probe
        from ..library import probe_duration
        ffmpeg = self._ffmpeg()
        name = os.path.basename(main)
        if not ffmpeg:
            return False
        head_length, head_size = probe(ffmpeg, head)
        main_length, main_size = probe(ffmpeg, main)
        # у кусков mp4 внутри .ts заголовок считает время от начала трансляции: длина — настоящая
        main_length = probe_duration(ffmpeg, main) or main_length
        if not head_length or not main_length:
            return self._refuse(head, main, tr("не прочитать длительность"), kept=False)
        # запись сконвертирована в «только звук»: у начала берётся только звук
        sound_only = "video" not in stream_kinds(ffmpeg, main)
        if not sound_only and head_size != main_size:
            return self._refuse(head, main, tr("размер кадра разный ({head} и {main})",
                                               head="%dx%d" % head_size if head_size else "?",
                                               main="%dx%d" % main_size if main_size else "?"))
        # без перекодирования склеивается только одно и то же: опубликованная
        # запись YouTube бывает в другом кодеке, чем эфир
        head_codecs, main_codecs = dict(streams(ffmpeg, head)), dict(streams(ffmpeg, main))
        if sound_only:
            head_codecs = {kind: codec for kind, codec in head_codecs.items() if kind in main_codecs}
        if head_codecs != main_codecs:
            return self._refuse(head, main, tr("кодеки разные ({head} и {main})",
                                               head=", ".join(sorted(head_codecs.values())) or "?",
                                               main=", ".join(sorted(main_codecs.values())) or "?"))
        fmp4 = mp4_inside_ts(ffmpeg, main)
        # такую запись склейка ещё и перепаковывает: место — на неё тоже
        needed = os.path.getsize(head) + os.path.getsize(main) * (2 if fmp4 else 1)
        if shutil.disk_usage(os.path.dirname(main)).free < needed * 1.1:
            return self._refuse(head, main, tr("мало места на диске"), kept=False)
        stem, extension = os.path.splitext(main)
        tail, target = main, main
        if fmp4:
            # куски mp4 внутри .ts: склеивается с ней же, перепакованной в mp4, и итог — .mp4.
            # Перепаковка — минуты: окно показывает, что склейка уже началась
            self._report(head, main, "joining", percent=0, stage="repack")
            tail, target, extension = stem + JOINING + ".tail.mp4", stem + ".mp4", ".mp4"
            try:
                as_mp4(ffmpeg, main, tail)
            except (OSError, subprocess.TimeoutExpired) as error:
                return self._refuse(head, main, str(error), kept=False, error=True)
            main_length = probe(ffmpeg, tail)[0] or main_length
        seam = find_sound_seam if sound_only else find_seam
        cut = seam(ffmpeg, head, tail, max(0.0, head_length - OVERLAP))
        if cut is None:
            util.remove_quietly(tail if tail != main else "")
            return self._refuse(head, main, tr("стык не найден"))
        output = stem + JOINING + extension
        logging.info(tr("склейка начала эфира с записью: {name}", name=name))
        self._report(head, main, "joining", percent=0)
        last = [0]

        def progress(share):
            percent = int(share * 100)
            if percent != last[0]:
                last[0] = percent
                self._report(head, main, "joining", percent=percent)

        try:
            join_video(ffmpeg, head, cut, tail, output, progress, main_length)
        except (OSError, subprocess.TimeoutExpired) as error:
            util.remove_quietly(output)
            return self._refuse(head, main, str(error), kept=False, error=True)
        finally:
            if tail != main:
                util.remove_quietly(tail)
        # видео склеено — дальше проверка длины, чат и замена файлов: своим этапом,
        # а не «99%», которые будто зависли
        self._report(head, main, "joining", percent=100, stage="finish")
        joined_length = probe(ffmpeg, output)[0]
        if not joined_length or abs(joined_length - (cut + main_length)) > 3:
            util.remove_quietly(output)
            return self._refuse(head, main, tr("длина вышла {got} вместо {want}", got=joined_length,
                                               want=round(cut + main_length)), kept=False, error=True)
        head_chat, main_chat = archive_chat.chat_paths(head), archive_chat.chat_paths(main)
        head_had_chat = os.path.isfile(head_chat["json"])
        try:
            count = join_chat(head_chat["json"], main_chat["json"], cut, joined_length, main_chat["json"] + ".joined")
        except (OSError, ValueError) as error:
            util.remove_quietly(output)
            return self._refuse(head, main, tr("чат не склеился: {error}", error=error), kept=False, error=True)
        # запись смотрят — склейка ждёт, пока её закроют, а не начинается заново через минуту
        waiting = lambda holders: self._report(head, main, "joining", percent=100, waiting=", ".join(holders))
        if not self._when_free(lambda: os.replace(output, target), main, waiting):
            util.remove_quietly(output)
            util.remove_quietly(main_chat["json"] + ".joined")
            return self._refuse(head, main, tr("запись открыта в другой программе"))
        if target != main and os.path.exists(main) and not self._when_free(lambda: _remove_existing(main), main, waiting):
            logging.warning(tr("не удалить {name}: файл открыт в другой программе", name=os.path.basename(main)))
        if count is not None:
            os.replace(main_chat["json"] + ".joined", main_chat["json"])
            # HTML и видео чата — заново, по склеенному чату
            for key in ("html", "video"):
                util.remove_quietly(main_chat[key])
        for path in [head, *head_chat.values()]:
            # начало эфира открыто в проигрывателе — окно скажет, что закрыть, а не будет тихо ждать
            if os.path.exists(path) and not self._when_free(lambda path=path: _remove_existing(path), path, waiting):
                # начало осталось рядом со склеенной записью: снова его не склеивать
                logging.warning(tr("не удалить {name}: файл открыт в другой программе",
                                   name=os.path.basename(path)))
                self._failed.add((head, main))
        logging.info(tr("начало эфира склеено с записью: {name} — стык на {at}, вышло {length}",
                        name=name, at=clock(cut), length=clock(joined_length)))
        # итог мог стать .mp4 (куски mp4 внутри .ts): загрузки и окно смотрят на него
        # chat — был ли у начала свой чат: нет — его можно докачать потом (загрузки)
        self._report(head, target, "done", seam=cut, length=joined_length, chat=head_had_chat)
        if count is not None and self._chat is not None:
            self._chat.enqueue(target)
        return True
