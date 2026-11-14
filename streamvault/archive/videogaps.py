# -*- coding: utf-8 -*-
"""
Пропуски видео своей записи — из записи эфира на площадке.

Когда запись не успевала за эфиром, в ней нет куска (integrity.py находит
такие места). Тот же кусок есть в записи эфира на площадке: он скачивается
отрезком с запасом по обе стороны и вклеивается на место — без
перекодирования, по кадрам, как склейка начала эфира (live/joining.py):

- первый кадр отрезка ищется в записи перед пропуском — там запись
  обрезается (cut);
- первый кадр записи после пропуска ищется в отрезке — там обрезается
  отрезок (piece_end);
- запись до cut, отрезок до piece_end и запись после пропуска склеиваются
  одним проходом, сколько бы пропусков ни было.

Запас нужен и для поиска кадров, и потому, что место пропуска в записи
эфира известно лишь примерно: эфир доходит до записи с задержкой в секунды.
Результат пишется рядом, запись заменяется им только целым и проверенным.
"""

import logging
import os
import subprocess

from .. import util
from ..i18n import tr
from ..live import joining

# Запас отрезка до и после пропуска, секунды: задержка эфира и место для поиска кадров.
PAD = 40.0
# Эфир доходит до записи с задержкой: место пропуска в записи эфира на деле раньше расчёта —
# отрезок берётся ещё на столько секунд раньше.
LATENCY = 20.0
# Сколько секунд записи перед пропуском просматривается в поисках первого кадра отрезка.
BEFORE = 2 * PAD + 10
# Первый кадр после пропуска должен быть ключевым: ищется не дальше стольких секунд от него.
KEYFRAME_NEAR = 1.5
# Кадры записи для сверки с отрезком берутся через столько секунд после пропуска.
AFTER = 0.5
# Уточняя место кадра, кадры перечитываются с ключевого не дальше стольких секунд назад.
REFINE_BACK = 15.0
# Длина результата может отличаться от ожидаемой на столько секунд.
LENGTH_SLACK = 3.0
FILLING = ".gapfill"


clock = joining.clock


class GapError(Exception):
    """Пропуск так не закрыть — текст для человека."""


def _locate(ffmpeg, path, wanted, start, length):
    """Где в path (в окне start…start+length) стоят кадры wanted: время первого или None."""
    window = joining.thumbnails(ffmpeg, path, start=max(0.0, start), length=length)
    if not wanted or len(window) < len(wanted):
        return None
    best, best_at = None, None
    for index in range(len(window) - len(wanted) + 1):
        score = sum(joining._difference(window[index + k][1], wanted[k]) for k in range(len(wanted))) / len(wanted)
        if best is None or score < best:
            best, best_at = score, window[index][0]
    return best_at if best is not None and best <= joining.MATCH_LIMIT else None


def _frames(ffmpeg, path, start=None):
    return [frame for _, frame in joining.thumbnails(ffmpeg, path, start=start, count=joining.FRAMES_TO_MATCH)]


def _keyframes(ffmpeg, path, start, end):
    """Время ключевых кадров файла между start и end (и ближайшего перед start) — по пакетам."""
    ffprobe = joining._ffprobe(ffmpeg)
    if not ffprobe:
        return []
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals",
                             "%.3f%%%.3f" % (max(0.0, start), end),
                             "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", path],
                            capture_output=True, text=True, timeout=120, creationflags=util.subprocess_flags())
    keys = []
    for line in result.stdout.splitlines():
        fields = line.strip().strip(",").split(",")
        try:
            if len(fields) > 1 and "K" in fields[1]:
                keys.append(float(fields[0]))
        except ValueError:
            continue
    return sorted(keys)


def keyframe_at(ffmpeg, path, moment):
    """
    Время ключевого кадра записи у места moment (первый кадр после пропуска) или
    None: с неключевого кадра запись без перекодирования не продолжить.
    """
    keys = [key for key in _keyframes(ffmpeg, path, moment - KEYFRAME_NEAR, moment + KEYFRAME_NEAR)
            if abs(key - moment) <= KEYFRAME_NEAR]
    return min(keys, key=lambda at: abs(at - moment)) if keys else None


def _from_key(ffmpeg, path, key, until=None, count=None):
    """
    Кадры файла подряд с ключевого кадра key: [(время, уменьшенный серый кадр)].
    Время здесь — настоящее, как у пакетов файла: декодер начинает ровно с
    ключевого кадра и ничего не отбрасывает. После обычной перемотки (-ss) у
    записей с B-кадрами подписи времени бывают сдвинуты на кадр, а один кадр
    идёт дважды — резать по таким подписям значило бы повторить или потерять кадр.
    """
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "info", "-noaccurate_seek", "-ss", "%.6f" % key,
               "-copyts", "-i", path, "-map", "0:v:0"]
    if until is not None:
        command += ["-to", "%.3f" % until]
    if count:
        command += ["-frames:v", str(count)]
    # кадры — как есть, по одному на кадр файла: без этого ffmpeg выравнивает частоту кадров
    # на выходе и там, где время кадров неровное, повторяет кадр — счёт кадров сбивался на один
    command += [*joining.frame_passthrough(ffmpeg),
                "-vf", "scale=%d:%d:flags=area,format=gray,showinfo" % joining.THUMB, "-f", "rawvideo", "pipe:1"]
    result = subprocess.run(command, capture_output=True, timeout=600, creationflags=util.subprocess_flags())
    size = joining.THUMB[0] * joining.THUMB[1]
    times = []
    for line in result.stderr.decode("utf-8", "replace").splitlines():
        if "Parsed_showinfo" in line and "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except (IndexError, ValueError):
                continue
    data = result.stdout
    return list(zip(times, [data[index * size:(index + 1) * size] for index in range(len(data) // size)]))


def _packet_times(ffmpeg, path, start, end):
    """Время показа кадров файла от start до end по порядку показа — по пакетам, без декодера."""
    ffprobe = joining._ffprobe(ffmpeg)
    if not ffprobe:
        return []
    result = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals",
                             "%.3f%%%.3f" % (max(0.0, start), end), "-show_entries", "packet=pts_time",
                             "-of", "csv=p=0", path],
                            capture_output=True, text=True, timeout=120, creationflags=util.subprocess_flags())
    times = []
    for line in result.stdout.splitlines():
        try:
            times.append(float(line.strip().strip(",")))
        except ValueError:
            continue
    return sorted(moment for moment in times if moment >= start - 0.0005)


def _exact(ffmpeg, path, wanted, about):
    """
    Настоящее время кадра, с которого в path идут кадры wanted: about — где он по
    грубому поиску (с точностью до кадра-другого). Кадры перечитываются с
    ключевого кадра перед этим местом, и время берётся не из подписи декодера
    (она бывает сдвинута на кадр), а счётом: какой это по порядку кадр от
    ключевого — такое по порядку и время среди пакетов файла. Не уточнить — about.
    """
    keys = [key for key in _keyframes(ffmpeg, path, about - REFINE_BACK, about + 0.5) if key <= about - 0.2]
    if not keys or not wanted:
        return about
    until = about + 0.5 + len(wanted) / 10.0
    frames = _from_key(ffmpeg, path, keys[-1], until=until)
    best, found = None, None
    for index in range(len(frames) - len(wanted) + 1):
        score = sum(joining._difference(frames[index + k][1], wanted[k]) for k in range(len(wanted))) / len(wanted)
        if best is None or score < best:
            best, found = score, index
    if best is None or best > joining.MATCH_LIMIT:
        return about
    times = _packet_times(ffmpeg, path, keys[-1], until + 1.0)
    return times[found] if found < len(times) else frames[found][0]


def plan(ffmpeg, recording, gap, piece, lead):
    """
    Как вклеить отрезок piece на место пропуска gap ({"at", "length", "kept"}):
    {"cut": докуда оставить запись, "resume": откуда её продолжить, "piece":
    отрезок, "piece_end": докуда его взять}. lead — сколько секунд отрезка
    идёт до пропуска по расчёту (его запас). Не сходится — GapError.
    """
    at, length = float(gap["at"]), float(gap["length"])
    # пропуск, оставшийся во времени видео: запись продолжается после него
    after = at + length if gap.get("kept") else at
    resume = keyframe_at(ffmpeg, recording, after)
    if resume is None:
        raise GapError(tr("после пропуска запись начинается не с ключевого кадра"))
    # первые кадры отрезка (он начинается с ключевого) — где они в записи перед пропуском:
    # сначала грубо, по окну, потом точно — с ключевого кадра записи
    opening = [frame for _, frame in _from_key(ffmpeg, piece, 0.0, count=joining.FRAMES_TO_MATCH)]
    cut = _locate(ffmpeg, recording, opening, at - BEFORE, BEFORE + 1)
    if cut is None or cut > at + 0.5:
        raise GapError(tr("начало отрезка не нашлось в записи перед пропуском"))
    cut = _exact(ffmpeg, recording, opening, cut)
    # сколько отрезка до пропуска — уже по найденному месту, а не по расчёту
    before = at - cut
    # кадры записи после пропуска берутся чуть дальше стыка: на самом стыке время кадров
    # бывает неровным, и первым «после» оказывался кадр до пропуска
    after_gap = _from_key(ffmpeg, recording, resume, until=resume + AFTER + 1.0)
    later = [item for item in after_gap if item[0] >= resume + AFTER][:joining.FRAMES_TO_MATCH]
    wanted = [frame for _, frame in later]
    found = _locate(ffmpeg, piece, wanted, max(0.0, before + length - PAD), 2 * PAD + 1) if later else None
    if found is None:
        raise GapError(tr("продолжение записи не нашлось в отрезке"))
    piece_end = _exact(ffmpeg, piece, wanted, found) - (later[0][0] - resume)
    if piece_end < before:
        raise GapError(tr("продолжение записи не нашлось в отрезке"))
    return {"cut": cut, "resume": resume, "piece": piece, "piece_end": piece_end,
            "added": round(piece_end - before - (length if gap.get("kept") else 0), 3)}


def _outpoint(ffmpeg, path, moment):
    """
    Где оборвать файл, чтобы кадр, показываемый в moment, в него уже не попал: время его
    декодирования, чуть раньше — в списке concat время пишется округлённым, и округление
    вверх оставляло этот кадр (он шёл дважды: из записи и из отрезка).
    """
    return max(0.0, joining.decode_time(ffmpeg, path, moment) - 0.0005)


def splice(ffmpeg, recording, plans, output, progress=None):
    """
    Запись с вклеенными отрезками — в output: plans — планы пропусков (plan)
    по порядку. Без перекодирования, одним проходом; отрезки перед этим
    обрезаются и перепаковываются в контейнер записи, как начало эфира при
    склейке. Сбой — OSError; output при этом не остаётся.
    """
    order = joining.stream_kinds(ffmpeg, recording) or ["video", "audio"]
    maps = ["0:%s:0" % kind[0] for kind in order]
    timescale = joining.video_timescale(ffmpeg, recording) if "video" in order else None
    extension = os.path.splitext(recording)[1]
    listings, cuts = [], []
    try:
        lines, since = [], None
        for number, item in enumerate(plans):
            # отрезок — до кадра, с которого запись продолжается
            trimmed = "%s.piece%d%s" % (output, number, extension)
            cuts.append(trimmed)
            outpoint = _outpoint(ffmpeg, item["piece"], item["piece_end"])
            listings.append(joining._listing("%s\noutpoint %.6f\nduration %.6f\n" % (
                joining._concat_line(item["piece"]), outpoint, item["piece_end"])))
            joining._run(joining.join_command(ffmpeg, listings[-1], trimmed, maps, timescale))
            # запись — от прошлого пропуска (или с начала) до этого
            lines.append(joining._concat_line(recording))
            if since is not None:
                lines.append("inpoint %.6f" % since)
            lines.append("outpoint %.6f" % _outpoint(ffmpeg, recording, item["cut"]))
            # длительность куска — по времени показа: с неё начинается следующий файл списка
            lines.append("duration %.6f" % (item["cut"] - (since or 0.0)))
            lines.append(joining._concat_line(trimmed))
            since = item["resume"]
        lines.append(joining._concat_line(recording))
        if since is not None:
            lines.append("inpoint %.6f" % since)
        listings.append(joining._listing("\n".join(lines) + "\n"))
        total = None
        if progress:
            from ..library import probe_duration
            total = probe_duration(ffmpeg, recording)
        joining._run(joining.join_command(ffmpeg, listings[-1], output, maps, timescale), progress, total)
        if not os.path.isfile(output) or not os.path.getsize(output):
            raise OSError("ffmpeg: нет файла")
    except BaseException:
        util.remove_quietly(output)
        raise
    finally:
        for listing in listings:
            util.remove_quietly(listing)
        for trimmed in cuts:
            util.remove_quietly(trimmed)


def fill(ffmpeg, recording, gaps, pieces, output, progress=None):
    """
    Закрыть пропуски записи отрезками: pieces — {номер пропуска в gaps: (файл
    отрезка, его запас до пропуска)}. Что сошлось, вклеивается в output;
    возвращает (планы закрытых пропусков — у каждого "index", номер пропуска,
    и "added", на сколько секунд запись стала длиннее; [причины несошедшихся]).
    Ни один не сошёлся — output не создаётся.
    """
    plans, notes = [], []
    for index, gap in enumerate(gaps):
        if index not in pieces:
            continue
        piece, lead = pieces[index]
        try:
            plans.append(dict(plan(ffmpeg, recording, gap, piece, lead), index=index))
        except GapError as error:
            notes.append(str(error))
            logging.info(tr("пропуск на {at} не закрыт: {error}", at=clock(gap["at"]), error=error))
    if plans:
        splice(ffmpeg, recording, plans, output, progress)
    return plans, notes
