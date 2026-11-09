# -*- coding: utf-8 -*-
"""
Пропуски в чате записи: где сообщений нет, хотя они, скорее всего, были.

Точно известные — разрывы связи с чатом во время записи: чат прямого эфира
отмечает их сам (livechat), а при переводе в .chat.json они ложатся меткой
в video.description — TwitchDownloader это поле не трогает. Остальные —
догадка по самому чату: он начинается заметно позже видео, кончается
заметно раньше или надолго замолкает посреди оживлённого места.

Догадка меряется темпом этого же чата рядом с паузой, а не общим порогом:
у тихого канала долгие паузы — обычное дело, и пропуском они не считаются.
Пауза — пропуск, только если при темпе соседних минут в ней ждали бы
не меньше EXPECTED сообщений (вероятность случайной тишины — меньше
одной миллионной).

Проверенные участки (их чат взяли с площадки или из архива, и там тоже
пусто) запоминаются той же меткой и больше не предлагаются.
"""

import bisect
import json
import re

MARK = "StreamVault gaps: "
MIN_RECORDED = 5.0        # разрыв связи короче — переподключение без потерь
EDGE_MIN = 120.0          # чат начался позже или кончился раньше видео хотя бы на столько
SILENCE_MIN = 90.0        # пауза посреди чата — не короче
WINDOW = 600.0            # темп чата — по стольким секундам до и после паузы
MIN_SAMPLE = 120.0        # меньше времени вокруг — темп не понять
EXPECTED = 15             # столько сообщений ждали бы — значит, пропуск
CHECKED_SHARE = 0.9       # участок проверен, если его закрывают проверенные куски

_MARK_LINE = re.compile(r"^" + re.escape(MARK) + r"(.*)$", re.MULTILINE)


# ------------------------------------------------------------ метка в описании

def _intervals(value):
    result = []
    for item in value if isinstance(value, list) else ():
        try:
            start, end = float(item[0]), float(item[1])
        except (TypeError, ValueError, IndexError):
            continue
        if end > start >= -1e9:
            result.append([round(start, 3), round(end, 3)])
    return result


def read_marks(description):
    """Метка пропусков из video.description: {"gaps": [[с, по]], "checked": [[с, по]]}."""
    match = _MARK_LINE.search(str(description or ""))
    data = {}
    if match:
        try:
            data = json.loads(match.group(1))
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return {"gaps": _intervals(data.get("gaps")), "checked": _intervals(data.get("checked"))}


def with_marks(description, marks):
    """Описание с новой меткой (пустая — убрать); остальное в описании остаётся как было."""
    text = _MARK_LINE.sub("", str(description or "")).strip("\n")
    marks = {key: _union(_intervals((marks or {}).get(key))) for key in ("gaps", "checked")}
    if not marks["gaps"] and not marks["checked"]:
        return text or None
    line = MARK + json.dumps({key: value for key, value in marks.items() if value}, separators=(",", ":"))
    return (text + "\n" + line) if text else line


def clip_marks(marks, begin, end, shift=0.0):
    """Метка куска begin–end: что в него попало, со временем от его начала (+shift)."""
    result = {}
    for key in ("gaps", "checked"):
        result[key] = [[round(max(start, begin) - begin + shift, 3), round(min(stop, end) - begin + shift, 3)]
                       for start, stop in _intervals((marks or {}).get(key)) if stop > begin and start < end]
    return result


def joined_marks(head, tail, cut, shift):
    """Метка склейки (live/joining.join_chat): начало до стыка, запись — со сдвигом."""
    head = clip_marks(head, 0.0, cut) if head else {"gaps": [], "checked": []}
    tail = clip_marks(tail, 0.0, float("inf"), shift) if tail else {"gaps": [], "checked": []}
    return {key: head[key] + tail[key] for key in ("gaps", "checked")}


def _union(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


# ------------------------------------------------------------ поиск

def find(times, length, marks=None):
    """
    Пропуски чата: [{"start", "end", "kind"}] по времени. times — время
    сообщений от начала видео (по порядку), length — длина видео. kind:
    recorded — разрыв связи при записи (точно); start, end — чат начался
    позже или кончился раньше видео; silence — тишина посреди оживлённого
    чата. Проверенные участки (marks["checked"]) не возвращаются.
    """
    times = sorted(float(moment) for moment in times)
    marks = marks or {}
    length = max(float(length or 0), times[-1] if times else 0.0)
    found = []
    for start, end in _intervals(marks.get("gaps")):
        start, end = max(0.0, start), min(length, end)
        if end - start >= MIN_RECORDED:
            found.append([start, end, "recorded"])
    if times:
        def rate(start, end):
            """Сообщений в секунду рядом с паузой start–end — по окну до и после неё."""
            before_from, after_to = max(0.0, start - WINDOW), min(length, end + WINDOW)
            seconds = (start - before_from) + (after_to - end)
            if seconds < MIN_SAMPLE:
                return None
            count = (bisect.bisect_left(times, start) - bisect.bisect_left(times, before_from)
                     + bisect.bisect_right(times, after_to) - bisect.bisect_right(times, end))
            return count / seconds

        def suspicious(start, end):
            pace = rate(start, end)
            return pace is not None and pace * (end - start) >= EXPECTED

        if times[0] >= EDGE_MIN and suspicious(0.0, times[0]):
            found.append([0.0, times[0], "start"])
        if length - times[-1] >= EDGE_MIN and suspicious(times[-1], length):
            found.append([times[-1], length, "end"])
        for previous, following in zip(times, times[1:]):
            if following - previous >= SILENCE_MIN and suspicious(previous, following):
                found.append([previous, following, "silence"])
    checked = _union(_intervals(marks.get("checked")))
    result = []
    # пересекающиеся — одним участком; точный разрыв важнее догадки
    for start, end, kind in sorted(found):
        if result and start <= result[-1]["end"]:
            last = result[-1]
            last["end"] = max(last["end"], end)
            if kind == "recorded":
                last["kind"] = "recorded"
            continue
        result.append({"start": start, "end": end, "kind": kind})
    return [gap for gap in result if _covered(checked, gap["start"], gap["end"]) < CHECKED_SHARE]


def _covered(intervals, start, end):
    """Какая доля start–end закрыта интервалами."""
    if end <= start:
        return 1.0
    total = sum(max(0.0, min(stop, end) - max(begin, start)) for begin, stop in intervals)
    return total / (end - start)


def summary(gaps):
    """(участков, секунд) — для подписи."""
    return len(gaps), sum(gap["end"] - gap["start"] for gap in gaps)


def video_span(video):
    """
    (начало, длина) видео чата из video в .chat.json. Длина — по end: в length
    TwitchDownloader, встраивая смайлы, пишет длину всей записи эфира на
    площадке, а она длиннее своей записи, если запись остановили посреди эфира.
    """
    try:
        start = float(video.get("start") or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        length = float(video.get("length") or 0)
    except (TypeError, ValueError):
        length = 0.0
    try:
        end = float(video.get("end") or 0)
    except (TypeError, ValueError):
        end = 0.0
    if end > start:
        length = min(length, end - start) if length else end - start
    return start, length


def chat_times(data):
    """(время сообщений от начала видео, длина видео) из .chat.json."""
    video = data.get("video") or {}
    start, length = video_span(video)
    times = []
    for comment in data.get("comments") or ():
        try:
            times.append(float(comment["content_offset_seconds"]) - start)
        except (KeyError, TypeError, ValueError):
            continue
    return times, length


def gaps_of(data):
    """Пропуски чата прямо из разобранного .chat.json."""
    times, length = chat_times(data)
    return find(times, length, read_marks((data.get("video") or {}).get("description")))


# ------------------------------------------------------------ докачанное — в чат

def fill(data, gaps, found, checked):
    """
    Вклеить докачанное в чат (разобранный .chat.json, меняется на месте).
    gaps — [[с, по]] участки; found — {номер участка: [сообщения со временем
    от начала видео]}; checked — номера участков, которые источник покрыл
    (их чат взят — даже если там пусто). Сообщения встают только внутрь
    своих участков, повторы по id не встают. Возвращает число вставленных.
    """
    video = data.setdefault("video", {})
    try:
        start = float(video.get("start") or 0)
    except (TypeError, ValueError):
        start = 0.0
    comments = data.setdefault("comments", [])
    known = {comment.get("_id") for comment in comments if comment.get("_id")}
    added = []
    for index, (begin, end) in enumerate(gaps):
        for comment in found.get(index) or ():
            try:
                moment = float(comment["content_offset_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            # на стыках — то, что уже есть в чате; повтор не нужен
            if not begin < moment < end or (comment.get("_id") and comment["_id"] in known):
                continue
            if comment.get("_id"):
                known.add(comment["_id"])
            added.append(dict(comment, content_offset_seconds=round(moment + start, 3)))
    if added:
        comments.extend(added)
        comments.sort(key=lambda comment: float(comment.get("content_offset_seconds") or 0))
    marks = read_marks(video.get("description"))
    covered = [list(gaps[index]) for index in checked]
    # проверенный разрыв связи больше не разрыв; участок — в проверенные
    # метки — во времени от начала видео, как и gaps
    marks["gaps"] = [gap for gap in marks["gaps"] if _covered(covered, gap[0], gap[1]) < CHECKED_SHARE]
    marks["checked"] = marks["checked"] + covered
    video["description"] = with_marks(video.get("description"), marks)
    return len(added)
