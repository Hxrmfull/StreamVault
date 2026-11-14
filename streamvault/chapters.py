# -*- coding: utf-8 -*-
"""
Разделы эфира по категориям: что стример показывал и когда.

Пока идёт запись, канал время от времени проверяется (live/recorder.py), и
смена категории или названия эфира ложится в файл рядом с пишущейся частью —
«….stream.json»: {"start": когда началась часть, "marks": [{"ts", "game",
"title"}]}. Когда часть становится готовой записью, из него получаются
разделы — файл «….chapters.json» рядом с видео: {"chapters": [{"start",
"game", "title"}]}, время — в секундах от начала видео, раздел идёт до
следующего. Проигрыватель рисует их делениями на шкале.

Файл — спутник записи, как её чат: вместе с ней делится на части,
сдвигается при склейке начала эфира, переезжает и удаляется
(archive/chat.companions).
"""

import json
import logging
import os
import threading
import time

from . import util
from .i18n import tr

SUFFIX = ".chapters.json"
TIMELINE = ".stream.json"
# Раздел короче этого (секунды) — не раздел: стример промахнулся категорией и тут же поправил.
MIN_CHAPTER = 20.0


def path_for(video):
    """Файл разделов записи: рядом с ней, с тем же именем."""
    return os.path.splitext(video)[0] + SUFFIX


def timeline_path(recorded):
    """Файл смен категории пишущейся части (рядом с ней, в папке незаконченных записей)."""
    return os.path.splitext(recorded)[0] + TIMELINE


def _clean(text):
    return " ".join(str(text or "").split())


def _same(left, right):
    return _clean(left).casefold() == _clean(right).casefold()


def _write(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as target:
        json.dump(data, target, ensure_ascii=False)
    os.replace(temporary, path)


def _read(path):
    try:
        with open(path, encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------ пока идёт запись

class Timeline:
    """
    Смены категории и названия эфира, пока пишется часть записи. Запись ведёт
    поток канала, а проверки эфира идут своим потоком — отсюда замок.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._path = None
        self._data = None

    def begin(self, recorded, began, stream=None):
        """Началась часть записи recorded (в began): первый раздел — то, что идёт сейчас."""
        with self._lock:
            self._path = timeline_path(recorded)
            self._data = {"start": began, "marks": []}
            self._note_locked(stream, began)

    def note(self, stream, now=None):
        """Эфир проверен: сменилась категория или название — запоминается. True — сменилось."""
        with self._lock:
            return self._note_locked(stream, time.time() if now is None else now)

    def end(self, keep=True):
        """Часть кончилась. keep=False — она пустая: файл не нужен."""
        with self._lock:
            path, data = self._path, self._data
            self._path = self._data = None
            if path is None:
                return
            if not keep:
                util.remove_quietly(path)
                return
            data["end"] = time.time()
            self._save(path, data)

    def _note_locked(self, stream, now):
        if self._data is None:
            return False
        game, title = _clean((stream or {}).get("game")), _clean((stream or {}).get("title"))
        marks = self._data["marks"]
        if marks and _same(marks[-1]["game"], game) and marks[-1]["title"] == title:
            return False
        # площадка в этот раз не назвала категорию — это не смена категории
        if marks and not game:
            game = marks[-1]["game"]
            if marks[-1]["title"] == title:
                return False
        marks.append({"ts": now, "game": game, "title": title})
        self._save(self._path, self._data)
        return True

    @staticmethod
    def _save(path, data):
        try:
            _write(path, data)
        except OSError as error:
            logging.debug("разделы эфира не записались: %s", error)


# ------------------------------------------------------------ разделы готовой записи

def from_timeline(data):
    """
    Смены категории части (Timeline) -> разделы: [{"start", "game", "title"}].
    Раздел — пока категория та же; смена одного названия раздела не начинает.
    Первый раздел — с нуля. Категория ни разу не названа — разделов нет.
    """
    try:
        start = float(data.get("start"))
    except (AttributeError, TypeError, ValueError):
        return []
    chapters = []
    for mark in data.get("marks") or ():
        try:
            moment = max(0.0, float(mark["ts"]) - start)
        except (KeyError, TypeError, ValueError):
            continue
        game, title = _clean(mark.get("game")), _clean(mark.get("title"))
        if chapters and _same(chapters[-1]["game"], game):
            continue
        if chapters and moment - chapters[-1]["start"] < MIN_CHAPTER:
            # прошлый раздел слишком короток: это была поправка, а не раздел
            chapters[-1].update(game=game, title=title)
            if len(chapters) > 1 and _same(chapters[-2]["game"], game):
                chapters.pop()
            continue
        chapters.append({"start": round(moment, 3) if chapters else 0.0, "game": game, "title": title})
    return chapters if any(chapter["game"] for chapter in chapters) else []


def read(video):
    """Разделы записи: [{"start", "game", "title"}] по порядку; нет файла — []."""
    data = _read(path_for(video))
    chapters = []
    for item in (data or {}).get("chapters") or ():
        try:
            chapters.append({"start": max(0.0, float(item["start"])), "game": _clean(item.get("game")),
                             "title": _clean(item.get("title"))})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return sorted(chapters, key=lambda chapter: chapter["start"])


def write(video, chapters):
    """Разделы — рядом с записью; пустые — файла нет."""
    target = path_for(video)
    if not chapters:
        util.remove_quietly(target)
        return
    _write(target, {"chapters": chapters})


def clip(chapters, begin, end):
    """
    Разделы куска begin–end записи, со временем от его начала: раздел,
    шедший на begin, начинается с нуля.
    """
    result = []
    for index, chapter in enumerate(chapters):
        following = chapters[index + 1]["start"] if index + 1 < len(chapters) else float("inf")
        if following <= begin or chapter["start"] >= end:
            continue
        result.append(dict(chapter, start=round(max(0.0, chapter["start"] - begin), 3)))
    return result


def shifted(chapters, shift):
    """Разделы записи, перед которой вклеили shift секунд (начало эфира): что было раньше, не известно."""
    return [dict(chapter, start=round(chapter["start"] + shift, 3)) for chapter in chapters]


def split(source, pieces, lengths):
    """Запись source поделили на pieces (длины lengths): разделы — по кускам."""
    chapters = read(source)
    if not chapters:
        return
    begin = 0.0
    try:
        for number, (piece, length) in enumerate(zip(pieces, lengths), 1):
            length = length or 0
            end = begin + length if number < len(pieces) else float("inf")
            write(piece, clip(chapters, begin, end))
            begin += length
    except OSError as error:
        logging.warning(tr("разделы эфира не поделились по частям записи: {error}", error=error))


def finish(recorded, videos, lengths=None):
    """
    Часть записи recorded стала файлом (videos — один) или кусками (несколько):
    её смены категории — в разделы рядом с ними. Нет смен (запись лежала
    готовой) — делятся уже готовые разделы. lengths() — длины кусков: нужны,
    только когда их несколько.
    """
    videos = list(videos)
    source = timeline_path(recorded)
    data = _read(source)
    try:
        if data is None:
            if len(videos) > 1 and os.path.isfile(path_for(recorded)):
                split(recorded, videos, lengths() if lengths else [None] * len(videos))
                if path_for(recorded) not in [path_for(video) for video in videos]:
                    util.remove_quietly(path_for(recorded))
            return
        chapters = from_timeline(data)
        if len(videos) == 1:
            write(videos[0], chapters)
        elif chapters:
            begin = 0.0
            sizes = lengths() if lengths else [None] * len(videos)
            for number, (piece, length) in enumerate(zip(videos, sizes), 1):
                length = length or 0
                write(piece, clip(chapters, begin, begin + length if number < len(videos) else float("inf")))
                begin += length
        util.remove_quietly(source)
    except OSError as error:
        logging.warning(tr("разделы эфира не сохранились: {name} — {error}",
                           name=os.path.basename(recorded), error=error))


def live(recorded):
    """Разделы части, которая ещё пишется (её смотрят в окне)."""
    data = _read(timeline_path(recorded))
    return from_timeline(data) if data else []
