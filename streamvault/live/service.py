# -*- coding: utf-8 -*-
"""
Запись эфиров как служба: набор каналов, который можно менять на ходу.

Через неё с записью работают и консольный режим, и окно программы:
добавить или удалить канал, выключить автозапись, проверить сейчас,
узнать состояние всех каналов разом.
"""

import copy
import functools
import logging
import os
import shutil
import threading

from .. import util
from ..ffmpeg import FFmpegLocator, own_dir_for
from ..twitchdownloader import TwitchDownloaderLocator
from ..twitchdownloader import own_dir_for as twitchdownloader_dir
from ..i18n import tr
from ..integrity import IntegrityStore, integrity_path
from ..platforms import (MIN_REFRESH_API, MIN_REFRESH_STREAMLINK, channel_id,
                         channel_label, clean_profile, is_url, normalize_channels,
                         parse_channel, refresh_for)
from .probes import PublicProfiles, Status, TwitchProbe, build_probes, describe_stream
from .chatreaders import make_chat
from .joining import Joiner
from .livechat import ChatFinisher
from .processing import ProcessingQueue
from .recorder import ChannelRecorder
from .storage import GB, DiskGuard, Retention

# Сколько ждать, пока выключенный канал погасит свой streamlink,
# прежде чем запустить его заново.
RELAUNCH_WAIT = 15
# свои настройки записи канала: с ними канал хранится, даже если он не в избранном
OWN_SETTINGS = ("quality", "keep_days", "options")


def preflight(root_path):
    """
    Проверки до старта. Без них отсутствующий streamlink или недоступный диск
    всплывали трассировкой из потока уже после "наблюдаю за каналами",
    а в файл лога попадало только "завершено". Заодно папки прошлых версий
    получают новые имена.

    Отсутствие ffmpeg здесь не ошибка: записи подождут в .ts, а ffmpeg
    можно установить кнопкой из окна.
    """
    problems = []
    if shutil.which(util.STREAMLINK[0]) is None:
        problems.append(tr("streamlink не найден: без него эфиры не записываются. "
                           "Установи: python -m pip install streamlink"))
    try:
        os.makedirs(root_path, exist_ok=True)
        probe = os.path.join(root_path, ".write-test")
        with open(probe, "wb"):
            pass
        os.remove(probe)
    except OSError as error:
        problems.append(tr("папка для записей недоступна: {path} ({error})",
                           path=root_path, error=error.strerror or error))
    else:
        migrate_folders(root_path)
    return problems


def migrate_folders(root_path):
    """
    recorded и processed прошлых версий — в in-progress и finished. Папку
    целиком переименовать нельзя, если её держит проводник или новая уже
    есть, — тогда файлы переносятся по одному. Не перенеслось — остаётся
    на месте и в журнале.
    """
    for old, new in util.OLD_RECORDING_DIRS.items():
        source = os.path.join(root_path, old)
        if not os.path.isdir(source):
            continue
        target = os.path.join(root_path, new)
        if not os.path.exists(target):
            try:
                os.rename(source, target)
                logging.info(tr("папка {old} теперь называется {new}", old=old, new=new))
                continue
            except OSError:
                pass
        left = 0
        for folder, _, names in os.walk(source):
            destination = os.path.join(target, os.path.relpath(folder, source))
            for name in names:
                try:
                    os.makedirs(destination, exist_ok=True)
                    shutil.move(os.path.join(folder, name),
                                util.unique_path(os.path.join(destination, name)))
                except OSError as error:
                    left += 1
                    logging.warning(tr("не перенёс {path} в {new}: {error}",
                                       path=os.path.join(folder, name), new=new,
                                       error=error.strerror or error))
        # пустые папки — снизу вверх
        for folder, _, _ in sorted(os.walk(source), key=lambda item: -len(item[0])):
            try:
                os.rmdir(folder)
            except OSError:
                pass
        if not left:
            logging.info(tr("записи из папки {old} перенесены в {new}", old=old, new=new))


class _ChannelBus:
    """Шина для потока канала: его снимки дополняет служба (LiveService._enrich)."""

    def __init__(self, service):
        self._service = service

    def emit(self, kind, **payload):
        if kind == "channel" and isinstance(payload.get("channel"), dict):
            payload["channel"] = self._service._enrich(payload["channel"])
        self._service._bus.emit(kind, **payload)

    def __getattr__(self, name):
        return getattr(self._service._bus, name)


class LiveService:
    """
    Все наблюдаемые каналы и общая очередь постобработки.

    События в шину: "channel" — изменилось состояние канала (полный снимок),
    "channel_removed" — канал удалён, "processing" — шаги очереди обработки.
    """

    def __init__(self, settings, bus=None, channels=None, quality=None,
                 disable_ffmpeg=False, ffmpeg=None, twitchdownloader=None, chat_factory=None,
                 watch_all=True, ytdlp_copy=None):
        """
        channels — разовый список каналов (ключ -u в консоли): с ним список
        в настройках не меняется и не сохраняется. quality — качество поверх
        настроек для всех каналов. ffmpeg — FFmpegLocator, общий с загрузками;
        twitchdownloader — TwitchDownloaderLocator для чата эфиров; chat_factory —
        клиент чата эфира (тесты подставляют свой); ytdlp_copy — YtdlpLocator:
        чат YouTube читает yt-dlp, и обновлённый лучше. watch_all — следить и за
        каналами без автозаписи (окно показывает, кто в эфире); консольной
        записи они ни к чему.
        """
        self._settings = settings
        self._bus = bus
        self._ffmpeg = ffmpeg or FFmpegLocator(settings, own_dir_for(settings))
        self._twitchdownloader = twitchdownloader or TwitchDownloaderLocator(
            twitchdownloader_dir(settings))
        self._chat_factory = chat_factory or functools.partial(
            make_chat, ytdlp_environment=ytdlp_copy.environment if ytdlp_copy else None)
        self._persist = channels is None
        # каналы консольной записи (-u) — чтобы их записать: автозапись у них включена
        self._entries = (normalize_channels(settings["channels"]) if channels is None
                         else normalize_channels(channels, enabled=True))
        self._quality_override = quality
        self._disable_ffmpeg = disable_ffmpeg
        self._watch_all = watch_all

        self._lock = threading.RLock()
        # общая остановка: токен Twitch и очередь обработки
        self._stop = threading.Event()
        self._recorders = {}
        self._probes = None
        self._streamlink_probe = None
        self._public_profiles = PublicProfiles()
        # место на диске под записи: общее у всех каналов
        self._beginning = None
        self._disk = DiskGuard(lambda: self._settings["min_free_gb"] * GB, bus)
        self._retention = None
        self._processing = None
        self._chat_finisher = None
        self._joiner = None
        self._started = False

    # ------------------------------------------------------------ запуск и остановка

    def preflight(self):
        return preflight(self._settings["root_path"])

    def start(self):
        with self._lock:
            if self._started:
                return
            self._probes, self._streamlink_probe = build_probes(self._settings, self._stop)
            # чат эфиров: HTML и видео чата — в своём потоке, после конвертации
            self._chat_finisher = ChatFinisher(self._settings, self._ffmpeg, self._twitchdownloader,
                                               threading.Event())
            self._chat_finisher.start()
            self._processing = ProcessingQueue(
                self._ffmpeg.path,
                # консольный ключ --disable-ffmpeg или выключенная автоконвертация —
                # общая или у канала записи
                lambda recorded=None: self._disable_ffmpeg or not self._recording_setting(
                    recorded, "convert_recordings"),
                self._stop,
                root_path=self._settings["root_path"],
                max_attempts=self._settings["ffmpeg_attempts"],
                retry_delay=self._settings["ffmpeg_retry_delay"],
                drain_grace=self._settings["drain_grace"],
                bus=self._bus, on_done=self._chat_finisher.finish,
                faststart=lambda recorded=None: self._recording_setting(recorded, "mp4_faststart"),
                # пропуски в записи ищутся до конвертации; итог видит окно
                integrity=IntegrityStore.at(integrity_path(self._settings)),
                split=lambda recorded: tuple(self._recording_setting(recorded, key) for key in
                                             ("split_mode", "split_size_gb", "split_minutes", "split_count")))
            self._processing.start()
            # начало эфира, скачанное отдельно, — склеить с записью, когда обе части готовы
            self._joiner = Joiner(self._settings, self._ffmpeg.path, self._join_busy, self._chat_finisher,
                                  threading.Event())
            self._joiner.start()
            # записи старше срока — в корзину, раз в час
            self._retention = Retention(self._settings, stop_event=self._stop)
            self._retention.start()
            self._started = True
            for entry in self._entries:
                if self._watched(entry):
                    self._launch(entry)
            watched = [recorder.tag for recorder in self._recorders.values()]
        if watched:
            logging.info(tr("наблюдаю за каналами: {channels}", channels=", ".join(watched)))

    def shutdown(self, grace=0, drain=True):
        """
        Останавливает запись и доводит очередь обработки. Возвращает число
        записей, оставшихся необработанными. Ctrl+C во время ожидания уходит
        наверх: консольный режим сам решает, ждать ли дальше.

        grace — сколько дать streamlink, чтобы он сам закрыл файл. В консоли
        до него доходит тот же Ctrl+C, без консоли ждать нечего. drain=False —
        не ждать конвертацию (Windows выключается): записи остаются в
        in-progress и обработаются при следующем запуске.
        """
        with self._lock:
            if not self._started:
                return 0
            recorders = list(self._recorders.values())
        self._stop.set()
        for recorder in recorders:
            recorder.stop(grace=grace)
        for recorder in recorders:
            util.join_interruptibly(recorder, grace + 30)
        # только теперь очередь вправе считать, что заданий больше не будет
        self._processing.close_inputs()
        util.join_interruptibly(self._processing, self._settings["drain_grace"] + 30 if drain else 1)
        # недоклеенное начало эфира склеится при следующем запуске
        self._joiner.stop()
        # недоделанный HTML чата подхватится при следующем запуске
        self._chat_finisher.stop()
        util.join_interruptibly(self._chat_finisher, 10)
        return self._processing.pending()

    def convert_recording(self, path, split=None):
        """
        Запись, оставленную в .ts, — в mp4 рядом с ней (кнопка в библиотеке).
        split — (mode, size_gb, minutes, count), выбранные в окне конвертации.
        """
        with self._lock:
            processing = self._processing
        if processing is None:
            raise ValueError(tr("конвертация недоступна: запись эфиров не запущена"))
        if not path.lower().endswith(".ts"):
            raise ValueError(tr("в mp4 конвертируются только записи .ts"))
        processing.submit(path, path, trusted=True, convert=True, split=split)

    def _join_busy(self, path):
        """
        Часть эфира ещё не готова к склейке: начало качается, запись пишется
        или конвертируется (её .ts ещё в in-progress), чат доделывается.
        """
        with self._lock:
            beginning, finisher = self._beginning, self._chat_finisher
        if beginning is not None and beginning.busy(path):
            return True
        if finisher is not None and finisher.busy(path):
            return True
        stem = os.path.splitext(path)[0]
        if any(os.path.exists(stem + suffix) for suffix in (".part", ".mp4.part", ".ts.part", ".ytdl")):
            return True
        finished = os.path.join(self._settings["root_path"], util.FINISHED_DIR)
        try:
            relative = os.path.relpath(stem, finished)
        except ValueError:
            return False
        recording = os.path.join(self._settings["root_path"], util.IN_PROGRESS_DIR, relative)
        return any(os.path.exists(recording + ext) for ext in (".ts", ".chat.jsonl"))

    def set_beginning(self, beginning):
        """
        Кто скачивает начало эфира, если запись началась позже (beginning.Beginning):
        ему нужна очередь загрузок, а её заводит окно. Без него начало не добирается.
        """
        with self._lock:
            self._beginning = beginning
            recorders = list(self._recorders.values())
        for recorder in recorders:
            recorder._beginning = beginning

    def rescan_chats(self):
        """Чаты эфиров без HTML — заново в работу: например, только что поставили TwitchDownloader."""
        if self._chat_finisher is not None:
            self._chat_finisher.rescan()

    def disk_status(self):
        """Хватает ли места на диске с записями: для окна при запуске."""
        self._disk.check(self._settings["root_path"])
        return self._disk.snapshot()

    def sweep_old_recordings(self):
        """Убрать старые записи сейчас (поменяли срок). Возвращает удалённые пути."""
        retention = self._retention or Retention(self._settings)
        return retention.sweep()

    def set_keep_days(self, cid, days=None):
        """Свой срок хранения записей канала, дней; None — как в общих настройках."""
        from ..platforms import keep_days_value
        with self._lock:
            entry = self._entry_or_new(cid)
            value = keep_days_value(days)
            if value is None:
                entry.pop("keep_days", None)
            else:
                entry["keep_days"] = value
            removed = self._settle(entry)
        self._announce(cid, removed)

    def set_options(self, cid, changes):
        """
        Свои настройки записи канала: {ключ: значение}, None — снова как
        в общих настройках. Значения уже проверены (platforms.channel_option_value).
        Идущая запись их подхватывает со следующей части, конвертация — со следующей записи.
        """
        with self._lock:
            entry = self._entry_or_new(cid)
            options = dict(entry.get("options") or {})
            for key, value in changes.items():
                if value is None:
                    options.pop(key, None)
                else:
                    options[key] = value
            if options:
                entry["options"] = options
            else:
                entry.pop("options", None)
            removed = self._settle(entry)
        self._announce(cid, removed)

    def is_running(self):
        return self._started and not self._stop.is_set()

    def has_enabled_channels(self):
        with self._lock:
            return any(entry["enabled"] for entry in self._entries)

    # ------------------------------------------------------------ каналы

    def add_channel(self, text, quality=None, enabled=False, profile=None):
        """
        Добавляет канал в избранное (он виден в панели слева): "kick:xqc",
        ссылку или имя (без префикса — Twitch). enabled — включить и
        автозапись; profile — имя и аватарка из выдачи поиска. Канал, который
        уже пишется, с автозаписью или своими настройками, остаётся каким был.
        Возвращает ключ канала. Негодная строка или канал уже в избранном — ValueError.
        """
        platform, channel = parse_channel(text)
        if platform is None:
            raise ValueError(channel)
        cid = channel_id(platform, channel)
        profile = clean_profile(profile)

        with self._lock:
            entry = self._find(platform, channel)
            if entry is not None and self._favorite(entry):
                raise ValueError(tr("канал {channel} уже есть в списке", channel=cid))
            if entry is None:
                entry = {"platform": platform, "channel": channel, "enabled": False}
                self._entries.append(entry)
            entry.pop("favorite", None)
            recorder = self._recorders.get(cid)
            alive = recorder is not None and recorder.is_alive()
            if enabled and not entry["enabled"]:
                entry["enabled"] = True
                if alive:
                    recorder.set_autorecord(True)
            if quality:
                entry["quality"] = quality.strip()
                if alive:
                    recorder.set_quality(self._quality_for(entry))
            if profile:
                entry["profile"] = profile
            self._settle(entry)
        logging.info(tr("{channel}: добавлен", channel=cid))
        self._emit_channel(cid)
        return cid

    def set_favorite(self, cid, favorite):
        """
        В избранном канал виден в панели слева — и только: запись, автозапись
        и свои настройки от этого не зависят. Убранный из избранного канал без
        автозаписи и своих настроек уходит из списка, а идущая запись дописывается.
        """
        if favorite:
            with self._lock:
                entry = next((entry for entry in self._entries if self._key(entry) == cid), None)
                if entry is not None and self._favorite(entry):
                    return
            self.add_channel(cid)
            return
        with self._lock:
            entry = self._entry(cid)
            entry["favorite"] = False
            removed = self._settle(entry)
        logging.info(tr("{channel}: убран из избранного", channel=cid))
        self._announce(cid, removed)

    def record_now(self, text):
        """
        Записать идущий эфир сейчас — и отслеживаемого канала с выключенной
        автозаписью, и любого другого: такой канал виден в списке, пока пишется,
        и не сохраняется. Не в эфире — ValueError.
        """
        platform, channel = parse_channel(text)
        if platform is None:
            raise ValueError(channel)
        cid = channel_id(platform, channel)
        with self._lock:
            if not self._started or self._stop.is_set():
                raise ValueError(tr("запись эфиров не запущена"))
            recorder = self._recorders.get(cid)
            state = recorder.snapshot()["state"] if recorder is not None and recorder.is_alive() else None
            probe = self._probe_for(platform, channel)
        if state not in ("live", "recording", "unavailable"):
            # не знаем, что с каналом, или знаем, что эфира нет: спросим сейчас
            status, meta = probe.check(platform, channel)
            if status is not Status.ONLINE:
                reason = meta.get("reason") if isinstance(meta, dict) else None
                raise ValueError(reason if status is Status.NOT_FOUND and reason else
                                 tr("{channel} сейчас не в эфире", channel=channel_label(channel)))
        with self._lock:
            entry = self._find(platform, channel)
            if entry is None:
                entry = {"platform": platform, "channel": channel, "enabled": False, "favorite": False,
                         "temporary": True}
                self._entries.append(entry)
            recorder = self._recorders.get(cid)
            if recorder is not None and recorder.is_alive():
                recorder.record_now()
            else:
                self._launch(entry, record_now=True)
        logging.info(tr("{channel}: запись начата вручную", channel=cid))
        self._emit_channel(cid)
        return cid

    def check_channel(self, text):
        """
        Разовая проверка канала не из списка — для его страницы: в эфире ли
        он и что идёт. {"state": live | offline | not_found | error, "message",
        "stream"}. Отслеживаемые каналы проверяет их поток, а не это.
        """
        platform, channel = parse_channel(text)
        if platform is None:
            raise ValueError(channel)
        with self._lock:
            if not self._started or self._stop.is_set():
                raise ValueError(tr("запись эфиров не запущена"))
            probe = self._probe_for(platform, channel)
        status, meta = probe.check(platform, channel)
        reason = meta.get("reason") if isinstance(meta, dict) else ""
        if status is Status.ONLINE:
            return {"state": "live", "message": "",
                    "stream": describe_stream(meta, platform, channel)}
        if status is Status.OFFLINE:
            return {"state": "offline", "message": "", "stream": None}
        if status is Status.NOT_FOUND:
            return {"state": "not_found", "message": reason or tr("канал не найден"), "stream": None}
        return {"state": "error", "message": reason or tr("нет связи"), "stream": None}

    def stop_recording(self, cid):
        """Остановить идущую запись канала. Наблюдение продолжается."""
        with self._lock:
            self._entry(cid)
            recorder = self._recorders.get(cid)
        if recorder is not None and recorder.is_alive():
            recorder.stop_recording()
            logging.info(tr("{channel}: запись остановлена вручную", channel=cid))

    def remove_channel(self, cid):
        """Убирает канал. Идущая запись останавливается и уходит в обработку."""
        with self._lock:
            entry = self._entry(cid)
            self._entries.remove(entry)
            self._save()
            recorder = self._recorders.pop(cid, None)
        if recorder is not None:
            recorder.stop(grace=0, silent=True)
        logging.info(tr("{channel}: убран из списка", channel=cid))
        if self._bus is not None:
            self._bus.emit("channel_removed", id=cid)

    def set_enabled(self, cid, enabled):
        """
        Включает или выключает автозапись канала. Наблюдение за каналом
        продолжается; идущую запись выключение не прерывает — для этого
        stop_recording.
        """
        enabled = bool(enabled)
        with self._lock:
            # автозапись бывает и у канала не из избранного: он заводится сам
            entry = self._entry_or_new(cid) if enabled else self._entry(cid)
            if entry["enabled"] == enabled:
                return
            entry["enabled"] = enabled
            recorder = self._recorders.get(cid)
            if recorder is not None and recorder.is_alive():
                recorder.set_autorecord(enabled)
            elif self._started and self._watched(entry):
                self._launch(entry)
            elif recorder is not None:
                # консольный режим: без автозаписи канал не наблюдается
                recorder.stop(grace=0, final_state="paused")
            removed = self._settle(entry)
        logging.info(tr("{channel}: автозапись включена", channel=cid) if enabled
                     else tr("{channel}: автозапись выключена", channel=cid))
        self._announce(cid, removed)

    def set_quality(self, cid, quality=None):
        """Своё качество канала; None — как в общих настройках."""
        with self._lock:
            entry = self._entry_or_new(cid)
            if quality:
                entry["quality"] = quality.strip()
            else:
                entry.pop("quality", None)
            recorder = self._recorders.get(cid)
            effective = self._quality_for(entry)
            removed = self._settle(entry)
        if not removed and recorder is not None and recorder.is_alive():
            recorder.set_quality(effective)
        self._announce(cid, removed)

    def check_now(self, cid=None):
        """Проверить канал (или все каналы) сразу, не дожидаясь паузы."""
        with self._lock:
            if cid is None:
                recorders = list(self._recorders.values())
            else:
                self._entry(cid)
                recorders = [self._recorders[cid]] if cid in self._recorders else []
        for recorder in recorders:
            recorder.check_now()

    # ------------------------------------------------------------ состояние

    def snapshot(self):
        """Всё состояние разом: для первой отрисовки окна и консольной сводки."""
        with self._lock:
            entries = copy.deepcopy(self._entries)
            recorders = dict(self._recorders)
            processing = self._processing
        channels = [self._channel_snapshot(entry, recorders.get(self._key(entry)))
                    for entry in entries]
        queue_state = processing.snapshot() if processing else {"pending": 0, "current": None}
        return {"channels": channels, "processing": queue_state}

    def twitch_profiles(self, logins):
        """
        Имена и аватарки каналов Twitch: {логин: {"name", "avatar"}}. С ключами
        Twitch — через Helix; без них (или если Helix не ответил) — так же,
        как их берёт сайт twitch.tv. Не нашлось — окно нарисует букву.
        """
        with self._lock:
            probe = (self._probes or {}).get("twitch")
        found = {}
        if isinstance(probe, TwitchProbe) and not probe.is_unusable():
            found = probe.profiles(logins)
        rest = [login for login in logins if login.lower() not in found]
        if rest:
            found.update(self._public_profiles.profiles(rest))
        return found

    def remember_profile(self, cid, profile):
        """
        Имя и аватарка, найденные для отслеживаемого канала, хранятся с ним —
        как профиль из выдачи поиска. Возвращает, изменилось ли что-нибудь.
        """
        profile = clean_profile(profile)
        if not profile:
            return False
        with self._lock:
            try:
                entry = self._entry(cid)
            except KeyError:
                return False
            if entry.get("profile") == profile:
                return False
            entry["profile"] = profile
            if not entry.get("temporary"):
                self._save()
        self._emit_channel(cid)
        return True

    def channel_snapshot(self, cid):
        with self._lock:
            entry = copy.deepcopy(self._entry(cid))
            recorder = self._recorders.get(cid)
        return self._channel_snapshot(entry, recorder)

    def _channel_snapshot(self, entry, recorder):
        if recorder is not None and (recorder.is_alive() or self._watched(entry)):
            return self._enrich(recorder.snapshot(), entry)
        return {
            "favorite": self._favorite(entry),
            "profile": entry.get("profile"),
            "keep_days": entry.get("keep_days"),
            "options": dict(entry.get("options") or {}),
            "id": self._key(entry),
            "platform": entry["platform"],
            "channel": entry["channel"],
            "label": channel_label(entry["channel"]),
            "enabled": entry["enabled"],
            "temporary": bool(entry.get("temporary")),
            # наблюдаемый, но не запущенный канал бывает только до start()
            "state": "starting" if self._watched(entry) else "paused",
            "message": "",
            "probe": None,
            "quality": self._quality_for(entry),
            "refresh": None,
            "stream": None,
            "recording": None,
            "last_check": None,
            "next_check": None,
        }

    def _emit_channel(self, cid):
        if self._bus is None:
            return
        try:
            snapshot = self.channel_snapshot(cid)
        except KeyError:
            return
        self._bus.emit("channel", channel=snapshot)

    # ------------------------------------------------------------ внутреннее

    @staticmethod
    def _key(entry):
        return channel_id(entry["platform"], entry["channel"])

    def _find(self, platform, channel):
        for entry in self._entries:
            if entry["platform"] == platform and entry["channel"].lower() == channel.lower():
                return entry
        return None

    def _enrich(self, snapshot, entry=None):
        """
        К снимку потока канала — то, что знает только служба: избранное,
        профиль, свои срок хранения и настройки. Окно заменяет канал снимком
        целиком: без этого после любого события потока они бы пропадали.
        """
        if entry is None:
            with self._lock:
                entry = copy.deepcopy(next((entry for entry in self._entries
                                            if self._key(entry) == snapshot.get("id")), None))
        if entry is not None:
            snapshot["favorite"] = self._favorite(entry)
            snapshot["profile"] = entry.get("profile")
            snapshot["keep_days"] = entry.get("keep_days")
            snapshot["options"] = dict(entry.get("options") or {})
        return snapshot

    @staticmethod
    def _favorite(entry):
        """В избранном — виден в панели слева. Каналы из прежних версий — все в избранном."""
        return entry.get("favorite", True) and not entry.get("temporary")

    def _entry_or_new(self, cid):
        """
        Канал из списка, а нет его — новый, не в избранном: так автозапись и
        свои настройки бывают у любого канала. Под self._lock; после правки — _settle.
        """
        entry = next((entry for entry in self._entries if self._key(entry) == cid), None)
        if entry is not None:
            return entry
        platform, channel = parse_channel(cid)
        if platform is None or channel_id(platform, channel) != cid:
            raise KeyError(tr("канала {channel} нет в списке", channel=cid))
        entry = {"platform": platform, "channel": channel, "enabled": False, "favorite": False}
        self._entries.append(entry)
        return entry

    def _settle(self, entry):
        """
        Под self._lock, после любой правки канала. Нужен канал — в избранном,
        с автозаписью или своими настройками — он хранится и наблюдается. Не
        нужен, но пишется — дописывает запись и уходит (temporary); не нужен
        и не пишется — уходит сразу. Возвращает True, если ушёл.
        """
        cid = self._key(entry)
        recorder = self._recorders.get(cid)
        alive = recorder is not None and recorder.is_alive()
        if entry.get("favorite", True) or entry["enabled"] or any(key in entry for key in OWN_SETTINGS):
            if entry.pop("temporary", None) and alive:
                recorder.make_permanent()
            if self._started and not alive and self._watched(entry):
                self._launch(entry)
            self._save()
            return False
        if not entry.get("temporary") and alive and recorder.busy():
            entry["temporary"] = True
            recorder.make_temporary()
        if entry.get("temporary"):
            self._save()
            return False
        self._entries.remove(entry)
        self._recorders.pop(cid, None)
        if recorder is not None:
            recorder.stop(grace=0, silent=True)
        self._save()
        return True

    def _announce(self, cid, removed):
        if removed:
            if self._bus is not None:
                self._bus.emit("channel_removed", id=cid)
        else:
            self._emit_channel(cid)

    def _entry(self, cid):
        for entry in self._entries:
            if self._key(entry) == cid:
                return entry
        raise KeyError(tr("канала {channel} нет в списке", channel=cid))

    def _save(self):
        if self._persist:
            # записанные кнопкой каналы не отслеживаются и в список не попадают
            self._settings.update(channels=[copy.deepcopy(entry) for entry in self._entries
                                            if not entry.get("temporary")])

    def _option(self, cid, key):
        """Настройка записи канала: своя (options) или общая."""
        with self._lock:
            entry = next((entry for entry in self._entries if self._key(entry) == cid), None)
            options = dict(entry.get("options") or {}) if entry else {}
        return options[key] if key in options else self._settings[key]

    def _recording_setting(self, recorded, key):
        """Настройка для записи по её пути: канал — по папке in-progress/<площадка>/<канал>."""
        platform, label = util.recording_channel(self._settings["root_path"], recorded) if recorded else (None, None)
        with self._lock:
            entry = next((entry for entry in self._entries if entry["platform"] == platform
                          and channel_label(entry["channel"]).lower() == (label or "").lower()), None)
        return self._option(self._key(entry), key) if entry else self._settings[key]

    def _quality_for(self, entry):
        return (self._quality_override or entry.get("quality")
                or self._settings["quality"])

    def _watched(self, entry):
        return self._watch_all or entry["enabled"] or bool(entry.get("temporary"))

    def _probe_for(self, platform, channel):
        """Чем проверять канал. Вызывается под self._lock после start()."""
        probe = self._probes[platform]
        # ссылка на запись или клип Twitch к логину не сводится — Helix её не поймёт
        if probe is not self._streamlink_probe and is_url(channel):
            probe = self._streamlink_probe
        return probe

    def _launch(self, entry, record_now=False):
        """Запускает наблюдение за каналом. Вызывается под self._lock."""
        platform, channel = entry["platform"], entry["channel"]
        cid = channel_id(platform, channel)

        previous = self._recorders.get(cid)
        if previous is not None and previous.is_alive():
            # канал только что выключили, его streamlink ещё гаснет;
            # без ожидания на один эфир писали бы два процесса сразу.
            # Его on_exit ждал бы self._lock, который держим мы, — он не нужен.
            previous._on_exit = None
            previous.stop(grace=0, final_state="paused", silent=True)
            previous.join(RELAUNCH_WAIT)

        probe = self._probe_for(platform, channel)
        floor = MIN_REFRESH_API if probe.name == "helix" else MIN_REFRESH_STREAMLINK
        refresh = refresh_for(platform, self._settings["refresh"], floor)
        fallback = self._streamlink_probe if probe is not self._streamlink_probe else None

        recorder = ChannelRecorder(platform, channel, probe, self._quality_for(entry),
                                   refresh, self._settings["root_path"], self._processing,
                                   fallback_probe=fallback,
                                   bus=_ChannelBus(self) if self._bus is not None else None,
                                   live_chat=lambda: self._option(cid, "live_chat"),
                                   live_restart=lambda: self._option(cid, "record_from_start"),
                                   disk=self._disk,
                                   beginning=self._beginning,
                                   chat_factory=self._chat_factory,
                                   autorecord=entry["enabled"],
                                   once=bool(entry.get("temporary")),
                                   on_exit=self._recorder_exited)
        if record_now:
            recorder.record_now()
        self._recorders[cid] = recorder
        recorder.start()

    def _recorder_exited(self, recorder):
        """
        Поток канала завершился. Канал, записанный кнопкой, после своей записи
        уходит из списка — он не отслеживается.
        """
        with self._lock:
            if self._recorders.get(recorder.id) is not recorder:
                return
            entry = next((entry for entry in self._entries if self._key(entry) == recorder.id), None)
            if entry is None or not entry.get("temporary"):
                return
            self._entries.remove(entry)
            del self._recorders[recorder.id]
        if self._bus is not None:
            self._bus.emit("channel_removed", id=recorder.id)
