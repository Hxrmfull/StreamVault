# -*- coding: utf-8 -*-
"""Постобработка записей: сырой .ts превращается в .mp4."""

import enum
import itertools
import logging
import math
import os
import queue
import shutil
import subprocess
import threading
import time
import traceback

from .. import util
from ..i18n import plural, tr
from ..util import PARTIAL_SUFFIX, remove_quietly, unique_path


GB = 1000 ** 3            # как его показывают Windows и сайты: 2 ГБ — два миллиарда байт
# Кусок короче этого не отрезается: хвост в полминуты прилипает к предыдущему.
MIN_PIECE = 60
MAX_PIECES = 100


def split_points(mode, duration, size, size_gb=0, minutes=0, count=0):
    """
    Где резать запись на куски: секунды от начала, [] — не резать.
    size — куски не больше size_gb: запись делится на равные куски, а не
    «по 2 ГБ и остаток» (4,1 ГБ — три куска по 1,4, а не 2 + 2 + 0,1).
    duration — по minutes минут. count — на count равных частей.
    Режет ffmpeg по ключевым кадрам: куски выходят примерно такими.
    """
    if not duration or duration < 2 * MIN_PIECE:
        return []
    if mode == "duration" and minutes > 0:
        step = minutes * 60
        points = [step * index for index in range(1, MAX_PIECES) if step * index < duration]
        # короткий хвост — к предыдущему куску
        if points and duration - points[-1] < MIN_PIECE:
            points.pop()
        return points
    if mode == "size" and size_gb > 0:
        pieces = math.ceil(size / (size_gb * GB))
    elif mode == "count":
        pieces = int(count)
    else:
        return []
    pieces = max(1, min(pieces, MAX_PIECES, int(duration // MIN_PIECE)))
    return [round(duration * index / pieces, 3) for index in range(1, pieces)]


def piece_name(target, number, total):
    """«… - part02.mp4» -> «… - part02 - piece 1 of 3.mp4»: куски одной части по порядку."""
    stem, extension = os.path.splitext(target)
    return "%s - piece %d of %d%s" % (stem, number, total, extension)


class Outcome(enum.Enum):
    DONE = 0        # задание закрыто
    RETRY = 1       # сбой, тратим попытку
    NOT_READY = 2   # файл ещё пишется, попытку не тратим


class ProcessingQueue(threading.Thread):
    """
    Постобработка записей: recorded/<платформа>/<канал>/*.ts -> processed/.../*.mp4.

    Упавшие задания возвращаются в очередь с задержкой, а не теряются.
    Каталоги периодически пересматриваются, чтобы подобрать файлы от прошлых
    запусков. Файл, в который прямо сейчас пишут, не трогается.

    О каждом шаге сообщается в шину событий типом "processing":
    queued, started, done, failed, gave_up.
    """

    def __init__(self, ffmpeg_path, disable_ffmpeg, stop_event, root_path=None,
                 max_attempts=3, retry_delay=120, rescan_interval=600,
                 settle_seconds=120, drain_grace=900, bus=None, on_done=None, faststart=True,
                 split=None, integrity=None):
        """
        ffmpeg_path — путь или функция, которая его возвращает (None — ffmpeg
        нет): ffmpeg могут установить, пока программа работает. disable_ffmpeg
        и faststart — значения или функции: настройки меняются на ходу;
        disable_ffmpeg(запись) — у канала бывает своя настройка конвертации.
        disable_ffmpeg — не конвертировать, а переносить .ts в processed как есть.
        faststart — оглавление mp4 в начало файла (ещё одна перезапись файла).
        on_done(запись, готовый файл) — после каждой обработанной записи (чат эфира);
        запись, поделённая на куски, — список файлов по порядку.
        split(запись) — как делить её на куски: (режим, ГБ, минуты, число частей);
        у канала бывает своё деление. integrity — IntegrityStore: запись
        проверяется на пропуски до конвертации (после неё длинные пропуски
        уже склеены и не видны), итог — у готовых файлов.
        """
        super().__init__(name="processing", daemon=True)
        self._ffmpeg_path = ffmpeg_path
        self._missing_reported = False
        self._disable_ffmpeg = disable_ffmpeg
        self._stop = stop_event
        self._root_path = root_path
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay
        self._rescan_interval = rescan_interval
        self._settle_seconds = settle_seconds
        self._drain_grace = drain_grace
        self._bus = bus
        self._on_done = on_done
        self._faststart = faststart
        self._split = split
        self._integrity = integrity

        self._queue = queue.PriorityQueue()
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._known = set()
        self._active = set()
        self._trusted = set()
        self._forced = set()     # конвертировать, даже если автоконвертация выключена
        self._split_once = {}    # как делить запись, выбранную в окне конвертации
        self._watched = []
        self._current = None   # что обрабатывается прямо сейчас
        # взводится, когда рекордеры отработали и новых заданий больше не будет
        self._inputs_closed = threading.Event()
        self._dropped = 0      # задания, брошенные при выходе

    def _done(self, recorded, target):
        if self._on_done is not None:
            try:
                self._on_done(recorded, target)
            except Exception:
                logging.exception(tr("после обработки {name}: сбой", name=os.path.basename(recorded)))
        # поделённая запись: result — первый кусок, pieces — все
        pieces = target if isinstance(target, list) else [target]
        self._emit("done", recorded, result=pieces[0], pieces=pieces)

    def _emit(self, action, recorded, **data):
        if self._bus is not None:
            self._bus.emit("processing", action=action, file=recorded, **data)

    def close_inputs(self):
        """
        Вызывается после join рекордеров. До этого очередь не имеет права
        считать себя пустой: streamlink получает Ctrl+C одновременно с нами
        и ставит последнюю запись в очередь уже после остановки.
        """
        self._inputs_closed.set()

    def watch(self, recorded_dir, processed_dir):
        with self._lock:
            if (recorded_dir, processed_dir) not in self._watched:
                self._watched.append((recorded_dir, processed_dir))

    def mark_active(self, path):
        with self._lock:
            self._active.add(path)

    def unmark_active(self, path):
        with self._lock:
            self._active.discard(path)

    def submit(self, recorded, processed, delay=0, trusted=False, convert=False, split=None):
        """
        trusted=True ставит рекордер после выхода streamlink: запись заведомо
        закончена, выдержку по времени изменения для неё применять не нужно.
        convert=True — сконвертировать в mp4, даже если автоконвертация
        выключена (кнопка в библиотеке). split — как делить именно эту запись
        (mode, size_gb, minutes, count), выбрано в окне конвертации; None —
        по настройкам канала.
        """
        with self._lock:
            if recorded in (self._forced if convert else self._known):
                return
            self._known.add(recorded)
            if trusted:
                self._trusted.add(recorded)
            if convert:
                self._forced.add(recorded)
            if split is not None:
                self._split_once[recorded] = tuple(split)
        self._queue.put((time.time() + delay, next(self._counter), 1, recorded, processed))
        self._emit("queued", recorded)

    def pending(self):
        """Сколько записей осталось необработанными, включая брошенные при выходе."""
        return self._queue.qsize() + self._dropped

    def snapshot(self):
        """Состояние очереди для окна программы."""
        with self._lock:
            current = dict(self._current) if self._current else None
        return {"pending": self.pending(), "current": current}

    def run(self):
        next_rescan = 0.0
        draining = False
        drain_deadline = 0.0

        while True:
            # после остановки не бросаем очередь, а доводим её до конца
            if self._stop.is_set() and not draining:
                draining = True
                drain_deadline = time.time() + self._drain_grace
                if not self._queue.empty():
                    logging.info(tr("доделываю оставшиеся записи ({count})",
                                    count=self._queue.qsize()))
            if draining:
                if time.time() > drain_deadline:
                    break
                # пустая очередь — ещё не повод уходить, пока живы рекордеры
                if self._queue.empty() and self._inputs_closed.is_set():
                    break

            if not draining and time.time() >= next_rescan:
                next_rescan = time.time() + self._rescan_interval
                try:
                    if self._rescan():
                        # запись, оборванную закрытием программы, при запуске не отличить
                        # от пишущейся — досмотреть, как только она «остынет», а не через 10 минут
                        next_rescan = min(next_rescan, time.time() + self._settle_seconds + 1)
                except Exception:
                    # сбой досмотра не должен останавливать обработку очереди
                    logging.error(tr("досмотр папок не удался") + "\n"
                                  + traceback.format_exc().rstrip())

            try:
                due, _, attempt, recorded, processed = self._queue.get(timeout=1)
            except queue.Empty:
                if draining and self._inputs_closed.is_set():
                    break
                continue

            remaining = due - time.time()
            if remaining > 0:
                if draining:
                    # отложенное при выходе не ждём, подхватится при следующем запуске
                    self._dropped += 1
                    continue
                self._queue.put((due, next(self._counter), attempt, recorded, processed))
                self._stop.wait(min(remaining, 1))
                continue

            with self._lock:
                self._current = {"file": recorded, "started": time.time()}
            try:
                outcome = self._process(recorded, processed)
            finally:
                with self._lock:
                    self._current = None

            if outcome is Outcome.DONE:
                with self._lock:
                    self._known.discard(recorded)
                    self._forced.discard(recorded)
            elif outcome is Outcome.NOT_READY:
                if draining:
                    self._dropped += 1
                    continue
                self._queue.put((time.time() + self._settle_seconds, next(self._counter),
                                 attempt, recorded, processed))
            elif attempt < self._max_attempts:
                logging.warning(tr("повтор обработки {name} (попытка {attempt} из {total}) "
                                   "через {seconds} с", name=os.path.basename(recorded),
                                   attempt=attempt + 1, total=self._max_attempts,
                                   seconds=self._retry_delay))
                self._emit("failed", recorded, attempt=attempt, retry_in=self._retry_delay)
                self._queue.put((time.time() + self._retry_delay, next(self._counter),
                                 attempt + 1, recorded, processed))
            elif not os.path.exists(recorded):
                logging.info(tr("файл уже обработан кем-то другим: {name}",
                                name=os.path.basename(recorded)))
                with self._lock:
                    self._known.discard(recorded)
                    self._forced.discard(recorded)
            elif recorded in self._forced:
                # ручная конвертация из библиотеки: запись так и остаётся в .ts
                logging.error(tr("конвертация не удалась (попыток: {count}), запись осталась "
                                 "в .ts: {path}", count=self._max_attempts, path=recorded))
                with self._lock:
                    self._known.discard(recorded)
                    self._forced.discard(recorded)
                self._emit("gave_up", recorded, attempt=attempt)
            else:
                logging.error(tr("обработка не удалась (попыток: {count}), файл оставлен "
                                 "в in-progress: {path}", count=self._max_attempts,
                                 path=recorded))
                self._emit("gave_up", recorded, attempt=attempt)

    def _is_being_written(self, path):
        """
        Файл ещё пишется? Перескан обходит все папки recorded/, в том числе
        принадлежащие другому запущенному экземпляру программы.
        """
        with self._lock:
            trusted = path in self._trusted
        try:
            if not trusted and time.time() - os.path.getmtime(path) < self._settle_seconds:
                return True
        except OSError:
            return False
        # Держит ли файл чужой streamlink. Переименование в то же самое имя ничего
        # не меняет, но на Windows падает с отказом в доступе, если файл открыт.
        # Открытие на дозапись тут не годится: Python пускает второго писателя.
        try:
            os.rename(path, path)
            return False
        except OSError:
            return True

    def _scan_targets(self):
        """
        Каталоги для досмотра: наблюдаемые сейчас каналы плюс всё, что лежит
        в recorded/<платформа>/<канал>. Иначе запись канала, который с тех пор
        убрали из списка, осталась бы необработанной навсегда.
        """
        with self._lock:
            targets = list(self._watched)
        if not self._root_path:
            return targets

        recorded_root = os.path.join(self._root_path, util.IN_PROGRESS_DIR)
        try:
            platforms = os.listdir(recorded_root)
        except OSError:
            return targets

        for platform in platforms:
            platform_dir = os.path.join(recorded_root, platform)
            if not os.path.isdir(platform_dir):
                continue
            try:
                channels = os.listdir(platform_dir)
            except OSError:
                continue
            for channel in channels:
                recorded_dir = os.path.join(platform_dir, channel)
                if not os.path.isdir(recorded_dir):
                    continue
                pair = (recorded_dir,
                        os.path.join(self._root_path, util.FINISHED_DIR, platform, channel))
                if pair not in targets:
                    targets.append(pair)
        return targets

    def _rescan(self):
        """Подхватывает необработанные .ts. True — какие-то отложены как ещё пишущиеся."""
        postponed = False
        for recorded_dir, processed_dir in self._scan_targets():
            self._remove_stale_partials(processed_dir)
            try:
                names = os.listdir(recorded_dir)
            except OSError as error:
                logging.error(tr("не читается {path} ({error})", path=recorded_dir, error=error))
                continue
            for name in names:
                # Только записи streamlink. Проводник сам кладёт в папки с видео
                # Thumbs.db и desktop.ini — ffmpeg бы на них падал при каждом запуске.
                if not name.lower().endswith(".ts"):
                    continue
                path = os.path.join(recorded_dir, name)
                if not os.path.isfile(path):
                    continue
                with self._lock:
                    if path in self._active or path in self._known:
                        continue
                if self._is_being_written(path):
                    postponed = True
                    continue
                logging.info(tr("найдена необработанная запись: {name}", name=name))
                self.submit(path, os.path.join(processed_dir, name))
        return postponed

    def _remove_stale_partials(self, processed_dir):
        """
        Недописанные результаты прошлых запусков: ffmpeg прервали, а исходник
        остался в recorded и будет обработан заново. Свежие не трогаем — их
        может прямо сейчас писать другой экземпляр программы.
        """
        try:
            names = os.listdir(processed_dir)
        except OSError:
            return
        for name in names:
            if not name.endswith(PARTIAL_SUFFIX):
                continue
            path = os.path.join(processed_dir, name)
            try:
                if time.time() - os.path.getmtime(path) > self._settle_seconds:
                    os.remove(path)
                    logging.info(tr("убран недописанный файл прошлого запуска: {name}",
                                    name=name))
            except OSError:
                pass

    def _split_points(self, ffmpeg, recorded, duration=None):
        """
        Где резать запись по настройке. duration — длительность готового
        видео, если её узнала проверка (пропуски дольше 10 с конвертация
        склеивает, и видео выходит короче, чем говорит заголовок .ts);
        иначе — от ffmpeg.
        """
        with self._lock:
            chosen = self._split_once.pop(recorded, None)
        if chosen is None and self._split is None:
            return []
        mode, size_gb, minutes, count = chosen or self._split(recorded)
        if mode not in ("size", "duration", "count"):
            return []
        from ..library import probe_duration
        size = os.path.getsize(recorded)
        if mode == "size" and size <= size_gb * GB:
            return []
        return split_points(mode, duration or probe_duration(ffmpeg, recorded), size, size_gb, minutes, count)

    def _check(self, ffmpeg, recorded):
        """Пропуски записи до конвертации: {"gaps", "duration"} или None (проверки нет)."""
        if self._integrity is None:
            return None
        from ..integrity import scan
        # без остановки по выходу: после него очередь доделывает оставшееся
        checked = scan(ffmpeg, recorded)
        if checked and checked["gaps"]:
            logging.info(tr("в записи {name} пропусков: {count}", name=os.path.basename(recorded),
                            count=len(checked["gaps"])))
        return checked

    def _remember_gaps(self, ffmpeg, checked, results):
        """Пропуски — у готовых файлов; у кусков — со временем от начала куска."""
        if checked is None or self._integrity is None:
            return
        if len(results) == 1:
            self._integrity.put(results[0], checked["gaps"], checked["duration"])
            return
        from ..integrity import map_to_pieces
        from ..library import probe_duration
        lengths = [probe_duration(ffmpeg, piece) for piece in results]
        for piece, gaps, length in zip(results, map_to_pieces(checked["gaps"], lengths), lengths):
            self._integrity.put(piece, gaps, length)

    def _wants_faststart(self, recorded):
        """faststart — значение или функция от пути записи: у канала может быть своё."""
        return self._faststart(recorded) if callable(self._faststart) else self._faststart

    def _convert_pieces(self, ffmpeg, recorded, target, points, checked=None):
        """
        Конвертация с нарезкой: сегментный муксер ffmpeg пишет куски один за
        другим, без перекодирования, каждый со временем от нуля. Куски пишутся
        во временные файлы и получают имена, только когда готовы все.
        """
        planned = len(points) + 1
        stem, extension = os.path.splitext(target)
        # в шаблоне ffmpeg «%» — служебный: в имени файла он удваивается
        pattern = (stem.replace("%", "%%") + " - piece %d" + extension + PARTIAL_SUFFIX)
        partials = [stem + " - piece %d" % number + extension + PARTIAL_SUFFIX for number in range(1, planned + 1)]
        faststart = self._wants_faststart(recorded)
        logging.info(tr("конвертирую в mp4 и делю на {count} {pieces}: {name}", count=planned,
                        pieces=plural(planned, "кусок|куска|кусков"), name=os.path.basename(recorded)))
        command = [ffmpeg, "-nostdin", "-y", "-err_detect", "ignore_err", "-i", recorded,
                   "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
                   "-f", "segment", "-segment_times", ",".join("%.3f" % point for point in points),
                   "-segment_start_number", "1", "-reset_timestamps", "1", "-segment_format", "mp4"]
        if faststart:
            command += ["-segment_format_options", "movflags=+faststart"]
        result = subprocess.run(command + [pattern], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", creationflags=util.subprocess_flags())
        produced = [path for path in partials if os.path.exists(path) and os.path.getsize(path) > 0]
        if result.returncode != 0 or not produced:
            logging.error(tr("ffmpeg не справился с {name} (код {code})",
                             name=os.path.basename(recorded), code=result.returncode))
            for line in (result.stderr or "").strip().splitlines()[-5:]:
                logging.error("  ffmpeg: %s", line)
            for path in partials:
                remove_quietly(path)
            return Outcome.RETRY
        # ключевых кадров бывает меньше, чем точек разреза, — тогда и кусков меньше
        if len(produced) == 1:
            pieces = [target]
        else:
            pieces = [unique_path(piece_name(target, number, len(produced)))
                      for number in range(1, len(produced) + 1)]
        for partial, piece in zip(produced, pieces):
            os.replace(partial, piece)
        try:
            os.remove(recorded)
        except OSError as error:
            logging.warning(tr("исходник занят ({error}), результат отброшен: {name}",
                               error=error.__class__.__name__, name=os.path.basename(recorded)))
            for piece in pieces:
                remove_quietly(piece)
            return Outcome.NOT_READY
        logging.info(tr("готово: {name}", name=", ".join(os.path.basename(piece) for piece in pieces)))
        self._remember_gaps(ffmpeg, checked, pieces)
        self._done(recorded, pieces if len(pieces) > 1 else pieces[0])
        return Outcome.DONE

    def _process(self, recorded, processed):
        try:
            if not os.path.exists(recorded):
                logging.warning(tr("файл исчез, пропускаю: {path}", path=recorded))
                return Outcome.DONE
            if self._is_being_written(recorded):
                logging.debug("файл ещё пишется, откладываю: %s", os.path.basename(recorded))
                return Outcome.NOT_READY
            if os.path.getsize(recorded) == 0:
                logging.warning(tr("пустая запись, удаляю: {name}",
                                   name=os.path.basename(recorded)))
                os.remove(recorded)
                return Outcome.DONE

            os.makedirs(os.path.dirname(processed), exist_ok=True)
            self._emit("started", recorded)

            with self._lock:
                forced = recorded in self._forced
            disabled = self._disable_ffmpeg(recorded) if callable(self._disable_ffmpeg) else self._disable_ffmpeg
            ffmpeg = self._ffmpeg_path() if callable(self._ffmpeg_path) else self._ffmpeg_path
            if disabled and not forced:
                # проверить можно и без конвертации — если ffmpeg есть
                checked = self._check(ffmpeg, recorded) if ffmpeg else None
                target = unique_path(processed)
                shutil.move(recorded, target)
                logging.info(tr("перемещено: {name}", name=os.path.basename(target)))
                self._remember_gaps(ffmpeg, checked, [target])
                self._done(recorded, target)
                return Outcome.DONE

            if not ffmpeg:
                # Без ffmpeg запись ждёт в .ts, попытки не тратятся: как только
                # его установят, очередь сконвертирует всё накопленное.
                if not self._missing_reported:
                    self._missing_reported = True
                    logging.warning(tr("ffmpeg не найден — записи ждут в .ts, пока его "
                                       "не установят (кнопка в окне программы)"))
                return Outcome.NOT_READY
            self._missing_reported = False

            target = unique_path(os.path.splitext(processed)[0] + ".mp4")
            checked = self._check(ffmpeg, recorded)
            points = self._split_points(ffmpeg, recorded, checked["duration"] if checked else None)
            if points:
                return self._convert_pieces(ffmpeg, recorded, target, points, checked)
            # ffmpeg пишет во временный файл и только по успеху он получает
            # настоящее имя. Иначе прерванная конвертация (Ctrl+C, закрытое окно,
            # сбой) оставляла битый mp4 под основным именем, а целый получал "(2)".
            partial = target + PARTIAL_SUFFIX
            logging.info(tr("конвертирую в mp4: {name}", name=os.path.basename(recorded)))
            faststart = self._wants_faststart(recorded)
            # faststart переносит оглавление mp4 в начало — для просмотра по сети;
            # для этого ffmpeg переписывает готовый файл ещё раз
            movflags = ["-movflags", "+faststart"] if faststart else []
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-y", "-err_detect", "ignore_err",
                 "-i", recorded, "-c", "copy", *movflags,
                 "-f", "mp4", partial],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=util.subprocess_flags())

            produced = os.path.exists(partial) and os.path.getsize(partial) > 0
            if result.returncode != 0 or not produced:
                logging.error(tr("ffmpeg не справился с {name} (код {code})",
                                 name=os.path.basename(recorded), code=result.returncode))
                for line in (result.stderr or "").strip().splitlines()[-5:]:
                    logging.error("  ffmpeg: %s", line)
                remove_quietly(partial)
                return Outcome.RETRY

            os.replace(partial, target)
            try:
                os.remove(recorded)
            except OSError as error:
                logging.warning(tr("исходник занят ({error}), результат отброшен: {name}",
                                   error=error.__class__.__name__,
                                   name=os.path.basename(recorded)))
                remove_quietly(target)
                return Outcome.NOT_READY

            logging.info(tr("готово: {name}", name=os.path.basename(target)))
            self._remember_gaps(ffmpeg, checked, [target])
            self._done(recorded, target)
            return Outcome.DONE
        except Exception as error:
            logging.error(tr("сбой обработки {path}: {error}", path=recorded, error=error))
            return Outcome.RETRY
