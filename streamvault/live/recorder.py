# -*- coding: utf-8 -*-
"""Наблюдение за одним каналом и запись его эфиров."""

import copy
import datetime
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import traceback

from .. import util
from ..i18n import tr
from ..platforms import MIN_REFRESH_STREAMLINK, channel_id, channel_label, stream_url
from ..util import build_recording_path, remove_quietly, unique_path
from .chatreaders import make_chat
from .livechat import jsonl_path
from .probes import API_RETRY_CAP, NETWORK_RETRY_CAP, Status, describe_stream
from .storage import CRITICAL, DISK_CHECK_INTERVAL, human_size

# Аргументы streamlink, помогающие пережить короткий обрыв.
STREAMLINK_RESILIENCE_ARGS = [
    "--retry-streams", "5",
    "--retry-max", "3",
    "--retry-open", "3",
    "--stream-segment-attempts", "5",
    "--stream-segment-timeout", "15",
    "--stream-timeout", "120",
]


def streamlink_output_arg(path):
    """
    streamlink считает путь в -o шаблоном: {title} и {author} подставляет сам,
    а на непарной скобке падает с ValueError. Удвоенные скобки шаблонизатор
    возвращает буквально — файл ляжет ровно туда, где его потом ищут.
    """
    return path.replace("{", "{{").replace("}", "}}")


# Высоты кадра, которые бывают у площадок: Twitch 160–1080 (и 936), YouTube 144–2160,
# Kick 160–1080, VK Video Live 240–1440.
_FRAME_HEIGHTS = (144, 160, 240, 360, 480, 540, 720, 900, 936, 1080, 1440, 2160)


def quality_with_fallback(quality):
    """
    Качество для streamlink с запасными вариантами. Twitch называет потоки
    то "720p", то "720p30" или "720p60", а у небольших каналов бывает только
    исходное качество. Без запаса запрошенное "720p" не находилось,
    и эфир не писался вовсе.

    Нет такого — ближайшее по высоте кадра (при равенстве — меньшее), и только
    потом лучшее: у YouTube нет 160p, и запрос «160p» писал эфир в 1080p.
    """
    quality = (quality or "best").strip()
    if "," in quality or quality in ("best", "worst"):
        return quality
    options = [quality]
    match = re.fullmatch(r"(\d+)p(\d*)", quality)
    if match:
        height, fps = int(match.group(1)), match.group(2)
        for nearest in sorted(set(_FRAME_HEIGHTS) | {height}, key=lambda h: (abs(h - height), h > height)):
            if fps:
                options.append("%dp%s" % (nearest, fps))
            options += ["%dp%s" % (nearest, rate) for rate in ("", "60", "30")]
    options.append("best")
    return ",".join(dict.fromkeys(options))


class StreamlinkMissing(Exception):
    """streamlink пропал из PATH уже после старта."""


class ChannelRecorder(threading.Thread):
    """
    Следит за одним каналом одной платформы и пишет его, пока тот в эфире.

    Состояние канала — в эфире ли он, что пишется, когда следующая проверка —
    хранится здесь же и уходит в шину событий типом "channel" при каждом
    изменении. По нему окно программы рисует список каналов.

    Значения поля state:
      starting     только запущен, первой проверки ещё не было
      offline      не в эфире
      live         в эфире, запись не идёт: автозапись выключена или запись остановили
      recording    в эфире, идёт запись
      unavailable  в эфире, но записать не выходит: только для подписчиков, гео-блок
      not_found    такого канала нет
      error        нет связи, сбой платформы или программы; подробности в message
      paused       не наблюдается: автозапись выключена (консольный режим)
      stopped      наблюдение остановлено

    Наблюдение и запись разделены: канал проверяется всегда, а пишется,
    если включена автозапись или запись начали кнопкой (record_now).
    Остановленная кнопкой запись (stop_recording) этого эфира больше
    не начинается сама — до его конца.
    """

    # Пауза после непредвиденного сбоя: отключили диск, удалили папку.
    CRASH_RETRY = 60
    # Потолок паузы, когда канал в эфире, а записать его не выходит.
    FRUITLESS_CAP = 600
    # Как часто перепроверять канал, которого не нашлось.
    MISSING_RECHECK = 600
    # Сколько раз подряд терпеть 401 на свежий токен, прежде чем отказаться от API.
    UNAUTHORIZED_LIMIT = 3
    # Сколько ждать, пока streamlink сам закроет файл после остановки по Ctrl+C.
    STOP_GRACE = 10
    # Поток пропал, а канал «не в эфире»: столько секунд ждать возвращения
    # эфира (короткий обрыв у стримера) и так часто его проверять.
    RETURN_GRACE = 300
    RETURN_CHECK = 15
    # Сколько последних строк вывода streamlink показывать, когда запись не пошла.
    OUTPUT_TAIL = 5

    def __init__(self, platform, channel, probe, quality, refresh, root_path,
                 processing, fallback_probe=None, bus=None, live_chat=None, chat_factory=None,
                 autorecord=True, once=False, on_exit=None, live_restart=None, disk=None,
                 beginning=None):
        """
        live_chat() — писать ли чат эфира (режим chat.MODES).
        live_restart() — записывать ли эфир с начала, если запись началась позже;
        beginning — кто скачивает это начало из записи площадки (beginning.Beginning).
        disk — DiskGuard: хватает ли места; без него место не проверяется.
        chat_factory(площадка, канал) — клиент чата (chatreaders.make_chat;
        тесты подставляют свой); None — у площадки чат не читается.
        autorecord — писать каждый эфир сам. once — канал не отслеживается,
        его записали кнопкой: поток завершается вместе с этой записью.
        on_exit(рекордер) — когда поток завершился.
        """
        self._platform = platform
        self._channel = channel
        self._label = channel_label(channel)
        self._probe = probe
        # на что переключиться, если основной опросчик окажется негодным
        self._fallback_probe = fallback_probe
        self._quality = quality
        self._refresh = refresh
        self._processing = processing
        self._bus = bus
        self._live_chat = live_chat
        self._chat_factory = chat_factory or make_chat
        self._chat = None         # чат идущего эфира (ChatReader)
        # Своя остановка у каждого канала: его можно выключить или удалить,
        # не трогая остальные.
        self._stop = threading.Event()
        # Будит из паузы между проверками: остановка или "проверить сейчас".
        self._wake = threading.Event()
        self._stop_grace = self.STOP_GRACE
        self._final_state = "stopped"
        self._autorecord = bool(autorecord)
        self._once = bool(once)
        self._on_exit = on_exit
        self._live_restart = live_restart
        self._beginning = beginning
        self._session_meta = {}
        self._disk = disk
        self._disk_full = False   # запись остановлена: на диске не осталось места
        self._disk_free = None
        self._manual = False      # записать идущий эфир: нажали «Начать запись»
        self._skip = False        # запись этого эфира остановили кнопкой
        # конец записи по кнопке: наблюдение при этом продолжается
        self._end_session = threading.Event()
        self.id = channel_id(platform, channel)
        self.tag = "%s:%s" % (platform, self._label)
        super().__init__(name=self.tag, daemon=True)
        self.recorded_path = os.path.join(root_path, util.IN_PROGRESS_DIR, platform, self._label)
        self.processed_path = os.path.join(root_path, util.FINISHED_DIR, platform, self._label)

        # счётчики подряд идущих неудач — от них растут паузы
        self._network_failures = 0
        self._api_failures = 0
        self._unauthorized = 0
        self._fruitless = 0
        self._announced = None   # состояние, о котором уже сказано в логе

        self._state_lock = threading.Lock()
        self._state = {
            "id": self.id,
            "platform": platform,
            "channel": channel,
            "label": self._label,
            "enabled": self._autorecord,     # автозапись
            "temporary": self._once,         # не отслеживается, записан кнопкой
            "state": "starting",
            "message": "",
            "probe": probe.name,
            "quality": quality,
            "refresh": refresh,
            "stream": None,       # describe_stream() идущего эфира
            "recording": None,    # файл, часть, начало, сколько записано
            # начало эфира, которое запись не застала: ищется, ждёт, скачивается, недоступно
            "beginning": None,
            "last_check": None,
            "next_check": None,
        }

    # ------------------------------------------------------------ управление

    def stop(self, grace=None, final_state="stopped", silent=False):
        """
        Останавливает наблюдение. Идущей записи даётся grace секунд, чтобы
        streamlink закрыл файл сам (в консоли до него доходит тот же Ctrl+C),
        затем дерево его процессов гасится. Без консоли ждать нечего: grace=0.

        final_state — каким канал останется в списке: stopped или paused.
        silent — больше не слать событий: канал удалён, и окну программы
        незачем снова узнавать о нём.
        """
        if grace is not None:
            self._stop_grace = grace
        self._final_state = final_state
        if silent:
            self._bus = None
        self._stop.set()
        self._wake.set()

    def check_now(self):
        """Прервать паузу и проверить канал сразу."""
        self._wake.set()

    def set_autorecord(self, enabled):
        """
        Автозапись. Включили — идущий эфир начинает писаться сразу; выключили —
        идущая запись продолжается, её останавливает stop_recording.
        """
        self._autorecord = bool(enabled)
        if enabled:
            self._skip = False
        elif self.snapshot()["state"] == "recording":
            # идущая запись дописывается, как начатая кнопкой: иначе первый
            # же обрыв потока молча закончил бы её
            self._manual = True
        self._set(enabled=self._autorecord)
        self._wake.set()

    def record_now(self):
        """Записать идущий эфир, даже если автозапись выключена."""
        self._manual = True
        self._skip = False
        self._wake.set()

    def stop_recording(self):
        """
        Остановить идущую запись, не прекращая наблюдения. Записанное уходит
        в обработку; сама запись этого эфира больше не начнётся.
        """
        self._manual = False
        self._skip = True
        self._end_session.set()
        self._wake.set()

    def make_permanent(self):
        """Канал, записанный кнопкой, стал отслеживаемым: поток не завершается."""
        self._once = False
        self._set(temporary=False)

    def _wants_recording(self):
        return (self._autorecord or self._manual) and not self._skip

    def set_quality(self, quality):
        """Новое качество действует со следующей записи или её части."""
        self._quality = quality
        self._set(quality=quality)

    def snapshot(self):
        """Состояние канала. Размер записи читается с диска в момент вызова."""
        with self._state_lock:
            state = copy.deepcopy(self._state)
        recording = state["recording"]
        if recording:
            try:
                current = os.path.getsize(recording["file"])
            except OSError:
                current = 0
            recording["bytes"] = recording["done_bytes"] + current
            chat = self._chat
            recording["chat_messages"] = chat.messages if chat is not None else None
        return state

    def _set(self, **changes):
        with self._state_lock:
            if all(self._state.get(key) == value for key, value in changes.items()):
                return
            self._state.update(changes)
        bus = self._bus
        if bus is not None:
            bus.emit("channel", channel=self.snapshot())

    def _wait_for_return(self):
        """
        streamlink потерял поток, а площадка говорит «не в эфире». Короткий
        обрыв у стримера (переподключился OBS, пропала сеть) выглядит так же,
        как конец эфира, — поэтому эфиру даётся RETURN_GRACE секунд вернуться.
        Вернулся — (статус, сведения о нём): пишется следующая часть того же
        эфира. Нет, или запись остановили — None.
        """
        logging.info(tr("{tag}: эфир прервался — жду его возвращения {minutes} мин",
                        tag=self.tag, minutes=self.RETURN_GRACE // 60))
        for _ in range(max(1, self.RETURN_GRACE // self.RETURN_CHECK)):
            self._sleep(self.RETURN_CHECK)
            if self._stop.is_set() or self._end_session.is_set():
                return None
            status, again = self._probe.check(self._platform, self._channel)
            if status is Status.ONLINE:
                logging.info(tr("{tag}: эфир вернулся", tag=self.tag))
                return status, again
        return None

    def _done_once(self):
        """Канал записывали кнопкой, и эта запись закончилась."""
        return self._once and not self._manual

    def _sleep(self, seconds):
        """Пауза до следующей проверки. Её прерывают остановка и check_now."""
        if self._stop.is_set() or self._done_once():
            return
        self._set(next_check=time.time() + seconds)
        self._wake.wait(seconds)
        if not self._stop.is_set():
            self._wake.clear()

    # ------------------------------------------------------------ наблюдение

    def prepare_directories(self):
        """Папки канала и их место в досмотре очереди обработки."""
        os.makedirs(self.recorded_path, exist_ok=True)
        os.makedirs(self.processed_path, exist_ok=True)
        self._processing.watch(self.recorded_path, self.processed_path)

    def run(self):
        logging.info(tr("{tag}: проверка каждые {seconds} с через {probe}, качество {quality}",
                        tag=self.tag, seconds=self._refresh, probe=self._probe.name,
                        quality=self._quality))
        dirs_ready = False

        while not self._stop.is_set() and not self._done_once():
            try:
                if not dirs_ready:
                    self.prepare_directories()
                    dirs_ready = True
                self._iteration()
            except StreamlinkMissing:
                logging.critical(tr("streamlink не найден. Установи: python -m pip install streamlink"))
                self._set(state="error", message=tr("streamlink не найден"), recording=None)
                self._sleep(self.CRASH_RETRY)
            except Exception as error:
                # Диск отключили, папку удалили, платформа ответила чем-то
                # невиданным. Канал не должен молча умирать, пока остальные пишутся:
                # причина уходит в лог, через минуту попытка повторяется.
                logging.error(tr("{tag}: сбой, повтор через {seconds} с", tag=self.tag,
                                 seconds=self.CRASH_RETRY)
                              + "\n" + traceback.format_exc().rstrip())
                self._set(state="error", message=tr("сбой: {error}", error=error),
                          recording=None)
                self._sleep(self.CRASH_RETRY)

        self._set(state=self._final_state, message="", stream=None, recording=None,
                  next_check=None)
        logging.info(tr("{tag}: наблюдение остановлено", tag=self.tag))
        if self._on_exit is not None:
            self._on_exit(self)

    def _iteration(self):
        # ключи Twitch отвергнуты — не бросаем канал, а спрашиваем streamlink
        if self._fallback_probe is not None and self._probe.is_unusable():
            logging.warning(tr("{tag}: проверка через {probe} недоступна, перехожу на {fallback}",
                               tag=self.tag, probe=self._probe.name,
                               fallback=self._fallback_probe.name))
            self._probe = self._fallback_probe
            self._fallback_probe = None
            self._refresh = max(MIN_REFRESH_STREAMLINK, self._refresh)
            self._set(probe=self._probe.name, refresh=self._refresh)

        # нажатие "проверить сейчас" во время проверки уже исполнено ею
        self._wake.clear()
        status, meta = self._probe.check(self._platform, self._channel)
        self._set(last_check=time.time())
        reason = meta.get("reason") if isinstance(meta, dict) else None
        if status is not Status.UNAUTHORIZED:
            self._unauthorized = 0

        if status is Status.ONLINE:
            self._network_failures = self._api_failures = 0
            if not self._wants_recording():
                # в эфире, но писать не просили: показываем эфир и ждём дальше
                stream = describe_stream(meta, self._platform, self._channel)
                if self._announced is not Status.ONLINE:
                    logging.info(tr("{tag} в эфире: {title} — не записывается", tag=self.tag,
                                    title=stream["title"] or tr("без названия")))
                    self._announced = Status.ONLINE
                self._set(state="live", message="", stream=stream, recording=None)
                self._sleep(self._refresh)
                return
            self._announced = None
            if self._no_room():
                # места нет — запись не начинается, пока его не освободят
                self._set(state="unavailable", recording=None,
                          stream=describe_stream(meta, self._platform, self._channel),
                          message=self._no_room_message())
                self._sleep(self._refresh)
                return
            failure = []
            if self._record_session(meta, failure):
                self._fruitless = 0
                if self._disk_full:
                    # остановлено из-за места: продолжится, когда его освободят
                    self._set(state="unavailable", recording=None, message=self._no_room_message())
                elif self._end_session.is_set():
                    # запись остановили кнопкой, а эфир идёт дальше
                    logging.info(tr("{tag}: запись остановлена, эфир продолжается", tag=self.tag))
                    self._announced = Status.ONLINE
                    self._set(state="live", recording=None)
                self._sleep(self._refresh)
            elif self._disk_full:
                self._set(state="unavailable", recording=None, message=self._no_room_message())
                self._sleep(self._refresh)
            elif self._end_session.is_set():
                self._set(state="live", recording=None)
                self._sleep(self._refresh)
            else:
                # Канал в эфире, но streamlink ничего не скачал: стрим только для
                # подписчиков, нет запрошенного качества, гео-блок. Без растущей
                # паузы перезапуски шли бы каждые полминуты весь стрим.
                self._fruitless += 1
                delay = min(self._refresh * 2 ** self._fruitless, self.FRUITLESS_CAP)
                logging.warning(tr("{tag}: канал в эфире, но записать его не удаётся, "
                                   "следующая попытка через {seconds} с",
                                   tag=self.tag, seconds=delay))
                self._set(state="unavailable", recording=None,
                          message=failure[-1] if failure else tr("записать не удаётся"))
                self._sleep(delay)

        elif status is Status.OFFLINE:
            self._network_failures = self._api_failures = self._fruitless = 0
            # эфир кончился: кнопки «начать» и «остановить» относились к нему
            self._manual = self._skip = False
            # первый раз говорим вслух, дальше молчим: иначе при опросе
            # раз в минуту лог заполнится одинаковыми строками
            if self._announced is not Status.OFFLINE:
                logging.info(tr("{tag}: не в эфире, жду выхода", tag=self.tag))
                self._announced = Status.OFFLINE
            else:
                logging.debug("%s: не в эфире", self.tag)
            self._set(state="offline", message="", stream=None, recording=None)
            self._sleep(self._refresh)

        elif status is Status.UNAUTHORIZED:
            self._unauthorized += 1
            if self._unauthorized > self.UNAUTHORIZED_LIMIT:
                logging.error(tr("{tag}: Twitch раз за разом не принимает свежий токен, "
                                 "перехожу на проверку без API", tag=self.tag))
                self._probe.disable()
            else:
                logging.info(tr("{tag}: токен отклонён, обновляю", tag=self.tag))
                self._probe.refresh_token()
                # первый раз — сразу, потом с паузой: без неё упорный 401
                # превращался в бесконечную долбёжку API без остановки
                self._sleep(5 * (self._unauthorized - 1))

        elif status is Status.NOT_FOUND:
            if self._announced is not Status.NOT_FOUND:
                logging.error(tr("{tag}: {reason} — проверь имя. Перепроверю через {minutes} мин",
                                 tag=self.tag, reason=reason or tr("канал не найден"),
                                 minutes=self.MISSING_RECHECK // 60))
                self._announced = Status.NOT_FOUND
            self._manual = False
            self._set(state="not_found", message=reason or tr("канал не найден"),
                      stream=None, recording=None)
            self._sleep(max(self._refresh * 4, self.MISSING_RECHECK))

        elif status is Status.NETWORK_ERROR:
            self._network_failures += 1
            delay = min(self._refresh * self._network_failures, NETWORK_RETRY_CAP)
            logging.warning(tr("{tag}: нет связи, повтор через {seconds} с",
                               tag=self.tag, seconds=delay))
            self._set(state="error", message=tr("нет связи"))
            self._sleep(delay)

        else:
            self._api_failures += 1
            delay = min(30 * (2 ** (self._api_failures - 1)), API_RETRY_CAP)
            # жалоба плагина куда полезнее общей фразы: по ней видно,
            # что канал в эфире, но платформа не отдаёт поток
            reason = reason or tr("платформа отвечает нештатно")
            logging.warning(tr("{tag}: {reason}, повтор через {seconds} с",
                               tag=self.tag, reason=reason, seconds=delay))
            self._set(state="error", message=reason)
            self._sleep(delay)

    # ------------------------------------------------------------ место на диске

    def _no_room(self):
        """На диске с записями почти не осталось места (меньше пятой части порога)."""
        if self._disk is None:
            return False
        level, self._disk_free = self._disk.check(self.recorded_path)
        self._disk_full = level == CRITICAL
        return self._disk_full

    def _no_room_message(self):
        return tr("на диске осталось {free} — запись остановлена, освободи место",
                  free=human_size(self._disk_free or 0))

    # ------------------------------------------------------------ запись

    def _wait_for_streamlink(self, process):
        """
        Ждёт конца записи. Ctrl+C из консоли streamlink получает сам и закрывает
        файл. Но при остановке иначе — канал выключили в окне, программа без
        консоли — он оставался бы сиротой и качал стрим в файл, который уже
        никто не обработает. Поэтому после остановки даём ему _stop_grace секунд
        и гасим всё дерево процессов: streamlink.exe — только лаунчер, сама
        запись идёт в дочернем python.exe.
        """
        stop_seen_at = None
        disk_checked = time.monotonic()
        while True:
            try:
                process.wait(timeout=1)
                return
            except subprocess.TimeoutExpired:
                pass
            if time.monotonic() - disk_checked >= DISK_CHECK_INTERVAL:
                disk_checked = time.monotonic()
                if self._no_room():
                    # лучше остановить запись, чем получить битый файл на забитом диске
                    logging.error(tr("{tag}: место на диске кончается — останавливаю запись",
                                     tag=self.tag))
                    self._end_session.set()
            # запись остановили кнопкой — ждать нечего, консоли у streamlink нет
            ended = self._end_session.is_set()
            if not self._stop.is_set() and not ended:
                continue
            if stop_seen_at is None:
                stop_seen_at = time.monotonic()
            if ended or time.monotonic() - stop_seen_at >= self._stop_grace:
                logging.info(tr("{tag}: останавливаю запись", tag=self.tag))
                util.kill_process_tree(process)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                return

    def _run_streamlink(self, url, recorded):
        """
        Пишет поток в файл до конца эфира или остановки. Возвращает вывод
        streamlink: он идёт во временный файл, а не в консоль, — так причина
        неудачи доходит до лога и окна программы, а прогресс streamlink
        не перемешивается со строками других каналов.
        """
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(
                    [*util.STREAMLINK, *STREAMLINK_RESILIENCE_ARGS,
                     url, quality_with_fallback(self._quality),
                     "-o", streamlink_output_arg(recorded)],
                    stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env=util.child_environment(), creationflags=util.subprocess_flags())
            except FileNotFoundError:
                raise StreamlinkMissing()
            self._wait_for_streamlink(process)
            output.seek(0)
            return output.read().decode("utf-8", "replace")

    def _explain_failure(self, output):
        """
        Хвост вывода streamlink — в лог. Возвращает самую содержательную
        строку: ошибку, если она есть, иначе последнюю.
        """
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for line in lines[-self.OUTPUT_TAIL:]:
            logging.warning("%s:   streamlink: %s", self.tag, line)
        for line in reversed(lines):
            lowered = line.lower()
            if lowered.startswith("error:"):
                return line[len("error:"):].strip()
            if "][error]" in lowered:
                return line.split("]", 2)[-1].strip()
        return lines[-1] if lines else ""

    def _record_session(self, meta, failure):
        """
        Пишет стрим, продолжая новыми частями после обрывов.
        Возвращает True, если записать удалось хоть что-то. Причину неудачи
        дописывает в список failure.
        """
        self._end_session.clear()
        stream = describe_stream(meta, self._platform, self._channel)
        title = stream["title"]
        started = datetime.datetime.now()
        logging.info(tr("{tag} в эфире: {title}", tag=self.tag,
                        title=title or tr("без названия")))
        self._set(state="recording", message="", stream=stream, beginning=None)
        self._session_meta = dict(meta or {})

        url = stream_url(self._platform, self._channel)
        chat = None
        if self._live_chat and self._live_chat() != "none":
            # чат пишется, пока идёт запись: анонимно, без входа в аккаунт
            chat = self._chat_factory(self._platform, self._channel)
            if chat is not None:
                chat.start()
                self._chat = chat
        try:
            return self._record_parts(url, title, started, chat, failure)
        finally:
            self._chat = None
            if chat is not None:
                chat.stop()
            if self._beginning is not None:
                # YouTube и VK выкладывают запись эфира после конца — тогда и начало
                self._beginning.session_ended(self._platform, self._channel)

    def _fetch_beginning(self, began, title, started):
        """
        Запись началась позже эфира: его начало — из записи на площадке, первой
        частью «part00» рядом с этой записью. Только у первой части: после
        обрыва новая часть продолжает с места.
        """
        if self._beginning is None or self._live_restart is None or not self._live_restart():
            return
        target = os.path.splitext(build_recording_path(self.processed_path, self._label, title, started, 0))[0]
        chat_mode = self._live_chat() if self._live_chat else "none"
        self._beginning.session_started(self._platform, self._channel, began, target, title, self._quality,
                                        chat_mode, meta=self._session_meta,
                                        report=lambda status: self._set(beginning=status))

    def _record_parts(self, url, title, started, chat, failure):
        """Части записи одного эфира; чат каждой части — рядом с ней."""
        recorded_any = False
        part = 1
        empty_parts = 0
        done_bytes = 0
        # stop_recording выставляет _skip раньше _end_session: нажатие
        # между проверкой и началом записи тоже не даст ей начаться
        while not self._stop.is_set() and self._wants_recording():
            if self._no_room():
                failure.append(self._no_room_message())
                return recorded_any
            recorded = unique_path(build_recording_path(
                self.recorded_path, self._label, title, started, part))
            processed = os.path.join(self.processed_path, os.path.basename(recorded))

            self._set(recording={"file": recorded, "part": part, "started": time.time(),
                                 "quality": self._quality, "done_bytes": done_bytes})
            self._processing.mark_active(recorded)
            began = time.time()
            if chat is not None:
                chat.begin_part(recorded, began)
            if part == 1 and not recorded_any:
                self._fetch_beginning(began, title, started)
            try:
                output = self._run_streamlink(url, recorded)
            finally:
                if chat is not None:
                    chat.end_part()
                self._processing.unmark_active(recorded)

            size = os.path.getsize(recorded) if os.path.exists(recorded) else 0
            # файл закрыт и ушёл в обработку; следующая часть заведёт свой
            self._set(recording=None)
            if size > 0:
                empty_parts = 0
                recorded_any = True
                done_bytes += size
                # streamlink уже вышел, файл дописан — выдержка не нужна
                self._processing.submit(recorded, processed, trusted=True)
            else:
                empty_parts += 1
                if os.path.exists(recorded):
                    os.remove(recorded)
                remove_quietly(jsonl_path(recorded))
                logging.warning(tr("{tag}: streamlink завершился за {seconds} с без данных "
                                   "({count} подряд)", tag=self.tag,
                                   seconds=round(time.time() - began), count=empty_parts))
                reason = self._explain_failure(output)
                if reason:
                    failure.append(reason)
                if empty_parts >= 3:
                    logging.error(tr("{tag}: три пустые попытки подряд, возвращаюсь к проверкам",
                                     tag=self.tag))
                    return recorded_any
                self._sleep(self._refresh)

            if self._stop.is_set() or self._end_session.is_set():
                return recorded_any

            # канал всё ещё в эфире? значит это был обрыв, а не конец стрима
            status, again = self._probe.check(self._platform, self._channel)
            if status is not Status.ONLINE:
                returned = self._wait_for_return()
                if returned is not None:
                    status, again = returned
            if status is not Status.ONLINE:
                if self._stop.is_set() or self._end_session.is_set():
                    return recorded_any
                logging.info(tr("{tag}: эфир закончился", tag=self.tag))
                # запись по кнопке относилась к этому эфиру
                self._manual = self._skip = False
                self._set(state="offline", stream=None, recording=None)
                return recorded_any

            fresh = describe_stream(again, self._platform, self._channel)
            if fresh["title"]:
                stream, title = fresh, fresh["title"]
                self._set(stream=stream)
            if size > 0:
                part += 1
                logging.info(tr("{tag}: эфир продолжается, пишу часть {part}",
                                tag=self.tag, part=part))
            else:
                # пустая попытка новой части не заводит: номер остаётся прежним
                self._set(state="recording")
                logging.info(tr("{tag}: эфир продолжается, пробую снова", tag=self.tag))
        return recorded_any
