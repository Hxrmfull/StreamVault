# -*- coding: utf-8 -*-
"""
Правка записей в окне программы: вырезать клип и наложить видео чата.

- Клип: ffmpeg режет без перекодирования (-c copy) — за секунды, но только
  по ключевым кадрам: начало клипа уходит к ближайшему ключевому кадру
  раньше отмеченного, обычно на пару секунд. По желанию рядом ложится
  чат этого отрезка: .chat.json со временем от начала клипа.
- Видео с чатом поверх: готовое видео чата (.chat.mp4, его рисует
  TwitchDownloader) накладывается на запись справа или слева. Это
  перекодирование всего видео — долго, поэтому с прогрессом и отменой.

Результат ложится рядом с записью, с меткой в имени — «[clip 01h02m03s-01h05m00s]»
или «[chat]»: по ней библиотека отличает его от частей эфира и от скачанного
целиком видео.
"""

import copy
import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid

from . import util
from .archive import chat
from .i18n import tr

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "queued", "running", "done", "failed", "cancelled"
CLIP, OVERLAY = "clip", "overlay"
SIDES = ("right", "left")

_DURATION = re.compile(r"Duration: (\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_VIDEO_SIZE = re.compile(r"Stream #\S+.*?Video: .*?(\d{2,5})x(\d{2,5})")
# Сколько строк вывода ffmpeg хранить ради текста ошибки.
_TAIL = 20


class EditError(Exception):
    """Правка не удалась. Текст — для человека."""


def _stamp(seconds):
    seconds = int(seconds)
    return "%02dh%02dm%02ds" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def clip_path(source, start, end):
    """Куда ляжет клип: рядом с записью, с отрезком в имени, всегда mp4."""
    stem = os.path.splitext(source)[0]
    return util.unique_path("%s [clip %s-%s].mp4" % (stem, _stamp(start), _stamp(end)))


def overlay_path(source):
    return util.unique_path(os.path.splitext(source)[0] + " [chat].mp4")


def clip_chat(source_json, target_json, start, end, length=None):
    """
    Чат отрезка start–end записи: сообщения из него со временем от начала
    клипа. Смайлы и значки остаются внутри — клип смотрится без сети.
    length — длина видео отрезка, если end — не его конец (последний кусок
    поделённой записи забирает весь хвост чата). Возвращает число сообщений;
    без сообщений файл не создаётся.
    """
    with open(source_json, encoding="utf-8") as source:
        data = json.load(source)
    video = data.get("video") or {}
    try:
        # у отрезка записи Twitch время сообщений — от начала всей записи
        shift = float(video.get("start") or 0)
    except (TypeError, ValueError):
        shift = 0.0
    comments = []
    for comment in data.get("comments") or ():
        try:
            offset = float(comment["content_offset_seconds"]) - shift
        except (KeyError, TypeError, ValueError):
            continue
        if start <= offset <= end:
            comment = dict(comment)
            comment["content_offset_seconds"] = round(offset - start, 3)
            comments.append(comment)
    if not comments:
        return 0
    result = dict(data)
    result["comments"] = comments
    length = round(length if length is not None else end - start, 3)
    result["video"] = dict(video, start=0, end=length, length=length)
    temporary = target_json + ".tmp"
    with open(temporary, "w", encoding="utf-8") as target:
        json.dump(result, target, ensure_ascii=False)
    os.replace(temporary, target_json)
    return len(comments)


def probe(ffmpeg, path):
    """Длительность (с) и размер кадра видео — из того, что ffmpeg пишет, открыв файл."""
    try:
        result = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                                capture_output=True, timeout=60, creationflags=util.subprocess_flags())
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    output = result.stderr.decode("utf-8", "replace")
    duration = None
    match = _DURATION.search(output)
    if match:
        hours, minutes, seconds = match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    size = _VIDEO_SIZE.search(output)
    return duration, (int(size.group(1)), int(size.group(2))) if size else None


def clip_command(ffmpeg, source, start, end, target):
    # -ss до -i — быстрый переход к ключевому кадру; -c copy — без перекодирования
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-ss", "%.3f" % start, "-i", source,
            "-t", "%.3f" % (end - start), "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", "-f", "mp4", target]


def overlay_command(ffmpeg, source, chat_video, height, side, target):
    """
    Видео чата — высотой с запись, у правого или левого края. Длина —
    по записи: кончился чат — дальше видео без него.
    """
    x = "W-w" if side == "right" else "0"
    graph = "[1:v]scale=-2:%d[chat];[0:v][chat]overlay=%s:0:eof_action=pass[v]" % (height, x)
    return [ffmpeg, "-hide_banner", "-nostdin", "-y", "-i", source, "-i", chat_video,
            "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", "-f", "mp4", target]


class Editor(threading.Thread):
    """
    Очередь правок: по одной за раз, в своём потоке. События в шину —
    "edit" со снимком правки. ffmpeg() — путь к ffmpeg (его могут поставить
    кнопкой, пока программа работает).
    """

    def __init__(self, ffmpeg, bus=None):
        super().__init__(name="editor", daemon=True)
        self._ffmpeg = ffmpeg
        self._bus = bus
        self._lock = threading.Condition()
        self._jobs = []
        self._process = None
        self._halt = False

    # ------------------------------------------------------------ управление

    def cut(self, source, start, end, with_chat=False):
        """Клип start–end секунд записи source. Возвращает id правки."""
        start, end = float(start), float(end)
        if start < 0 or end - start < 1:
            raise ValueError(tr("клип — хотя бы секунда: конец позже начала"))
        return self._add(CLIP, source, start=start, end=end, with_chat=bool(with_chat))

    def overlay(self, source, side="right"):
        """Видео чата поверх записи source. Возвращает id правки."""
        if side not in SIDES:
            raise ValueError(tr("чат — справа или слева"))
        if not os.path.isfile(chat.chat_paths(source)["video"]):
            raise ValueError(tr("у записи нет видео чата — сделай его кнопкой «чат + видео чата»"))
        return self._add(OVERLAY, source, side=side)

    def cancel(self, job_id):
        with self._lock:
            job = self._job(job_id)
            if job["state"] not in (QUEUED, RUNNING):
                return
            process = self._process if job["state"] == RUNNING else None
            job.update(state=CANCELLED, finished=time.time())
        if process is not None:
            util.kill_process_tree(process)
        self._emit(job)

    def snapshot(self):
        with self._lock:
            return [copy.deepcopy(job) for job in self._jobs]

    def stop(self):
        with self._lock:
            self._halt = True
            process = self._process
            self._lock.notify_all()
        if process is not None:
            util.kill_process_tree(process)

    # ------------------------------------------------------------ работа

    def run(self):
        while True:
            with self._lock:
                while not self._halt and not any(job["state"] == QUEUED for job in self._jobs):
                    self._lock.wait()
                if self._halt:
                    return
                job = next(job for job in self._jobs if job["state"] == QUEUED)
                job["state"] = RUNNING
                job["started"] = time.time()
            self._emit(job)
            try:
                self._do(job)
            except EditError as error:
                self._finish(job, FAILED, str(error))
            except Exception as error:
                logging.exception(tr("правка {name} упала", name=job["name"]))
                self._finish(job, FAILED, str(error))

    def _do(self, job):
        ffmpeg = self._ffmpeg()
        if not ffmpeg:
            raise EditError(tr("нужен ffmpeg — его можно скачать в настройках"))
        source = job["source"]
        if job["kind"] == CLIP:
            command = clip_command(ffmpeg, source, job["start"], job["end"], job["partial"])
            duration = job["end"] - job["start"]
        else:
            duration, size = probe(ffmpeg, source)
            if not size:
                raise EditError(tr("не понял, какого размера видео: {name}", name=job["name"]))
            command = overlay_command(ffmpeg, source, chat.chat_paths(source)["video"], size[1],
                                      job["side"], job["partial"])
        self._run(job, command, duration)
        if job["state"] == CANCELLED:
            return
        os.replace(job["partial"], job["target"])
        if job["kind"] == CLIP and job["with_chat"]:
            chat_json = chat.chat_paths(source)["json"]
            # клип начался с ключевого кадра раньше отмеченного: чат — от него же,
            # иначе он спешил бы на эти секунды. Конец клипа — на отметке
            length = probe(ffmpeg, job["target"])[0]
            begin = max(0.0, min(job["start"], job["end"] - length)) if length else job["start"]
            if os.path.isfile(chat_json):
                try:
                    job["chat_messages"] = clip_chat(chat_json, chat.chat_paths(job["target"])["json"],
                                                     begin, job["end"])
                except (OSError, ValueError) as error:
                    logging.warning(tr("чат клипа не сохранился: {error}", error=error))
        logging.info(tr("готово: {name}", name=os.path.basename(job["target"])))
        self._finish(job, DONE)

    def _run(self, job, command, duration):
        """ffmpeg с прогрессом: -progress pipe:1 пишет out_time_us по ходу работы."""
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       stdin=subprocess.DEVNULL, creationflags=util.subprocess_flags())
        except OSError as error:
            raise EditError(tr("не удалось запустить ffmpeg: {error}", error=error))
        tail = []
        reader = threading.Thread(target=lambda: tail.extend(
            line.decode("utf-8", "replace").strip() for line in process.stderr), daemon=True)
        reader.start()
        with self._lock:
            self._process = process
            cancelled = job["state"] == CANCELLED
        if cancelled:
            util.kill_process_tree(process)
        last = 0.0
        for raw in process.stdout:
            key, _, value = raw.decode("utf-8", "replace").strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                percent = min(99, int(int(value) / 1e6 / duration * 100))
                if percent != job["progress"] and time.monotonic() - last > 0.5:
                    last = time.monotonic()
                    with self._lock:
                        job["progress"] = percent
                    self._emit(job)
        process.wait()
        reader.join(5)
        with self._lock:
            self._process = None
        if job["state"] == CANCELLED:
            util.remove_quietly(job["partial"])
            return
        if process.returncode != 0 or not os.path.isfile(job["partial"]):
            util.remove_quietly(job["partial"])
            lines = [line for line in tail if line][-_TAIL:]
            for line in lines[-5:]:
                logging.error("  ffmpeg: %s", line)
            raise EditError(lines[-1] if lines else tr("ffmpeg завершился с кодом {code}",
                                                        code=process.returncode))

    # ------------------------------------------------------------ внутреннее

    def _add(self, kind, source, **options):
        target = (clip_path(source, options["start"], options["end"]) if kind == CLIP
                  else overlay_path(source))
        job = {"id": uuid.uuid4().hex[:12], "kind": kind, "source": source, "target": target,
               "partial": target + util.PARTIAL_SUFFIX, "name": os.path.basename(target),
               "state": QUEUED, "progress": 0, "error": "", "chat_messages": None,
               "created": time.time(), "started": None, "finished": None}
        job.update(options)
        with self._lock:
            self._jobs.append(job)
            self._lock.notify_all()
        logging.info(tr("в работу: {name}", name=job["name"]))
        self._emit(job)
        return job["id"]

    def _finish(self, job, state, error=""):
        with self._lock:
            if job["state"] == CANCELLED:
                state = CANCELLED
            job.update(state=state, error=error, finished=time.time(),
                       progress=100 if state == DONE else job["progress"])
        if state == FAILED:
            logging.error(tr("не получилось: {name} — {error}", name=job["name"], error=error))
        self._emit(job)

    def _job(self, job_id):
        for job in self._jobs:
            if job["id"] == job_id:
                return job
        raise KeyError(tr("правки {id} нет", id=job_id))

    def _emit(self, job):
        if self._bus is not None:
            with self._lock:
                snapshot = copy.deepcopy(job)
            self._bus.emit("edit", job=snapshot)
