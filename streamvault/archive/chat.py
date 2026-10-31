# -*- coding: utf-8 -*-
"""
Чат записей и клипов Twitch — через TwitchDownloaderCLI. Чат записей
YouTube, Kick и VK Video Live скачивает vodchat.py, а HTML и видео чата из него — тоже
TwitchDownloader.

Рядом с видео ложатся:
  <имя>.chat.json  чат целиком, смайлы внутри: из него можно снова
                   сделать HTML или видео чата, даже когда запись удалят;
  <имя>.chat.html  чат для чтения в браузере;
  <имя>.chat.mp4   видео чата, если выбрано: его можно положить рядом
                   с записью или поверх неё в монтаже.
"""

import math
import os
import re
import subprocess

from .. import util
from ..i18n import tr

NONE, TEXT, RENDER = "none", "text", "render"
MODES = (NONE, TEXT, RENDER)
SUFFIXES = {"json": ".chat.json", "html": ".chat.html", "video": ".chat.mp4"}

# "[STATUS] - Downloading Embed Images 45%" и "[STATUS] - Rendering Video 45% (…)"
_PROGRESS = re.compile(r"\[STATUS\] - .*?(\d+)%")
_LINES = re.compile(rb"[\r\n]+")
# сколько строк вывода хранить ради текста ошибки: начало и конец
_HEAD, _TAIL = 20, 30


class ChatError(Exception):
    """Чат не скачался. Текст — для человека."""


class ChatStopped(Exception):
    """Загрузку остановили, пока шёл чат."""


def supported(platform, kind, live_status=""):
    """
    У каких загрузок бывает чат — решает модуль площадки (CHAT_KINDS):
    записи эфиров и клипы Twitch, Kick и VK Video Live, хайлайты Twitch,
    прошедшие эфиры YouTube (у обычного видео YouTube чата нет; неизвестно,
    был ли эфир, — пробуем).
    """
    from .. import sites
    return sites.recording_chat(platform, kind, live_status)


def chat_paths(video_path):
    base = os.path.splitext(video_path)[0]
    return {key: base + suffix for key, suffix in SUFFIXES.items()}


def existing(video_path):
    """Уже готовые файлы чата у этого видео."""
    return {key: path for key, path in chat_paths(video_path).items() if os.path.isfile(path)}


def commands(cli, url, video_path, mode, section=None, ffmpeg=None, size=(422, 1080, 18),
             temp_dir=None):
    """
    Шаги TwitchDownloaderCLI: (шаг, команда). cli — список, как запускать
    сам TwitchDownloaderCLI (тесты подставляют заглушку). section — (начало,
    конец) в секундах: чат только этого отрезка.
    """
    paths = chat_paths(video_path)
    download = [*cli, "chatdownload", "--id", url, "-o", paths["json"], "--embed-images",
                *_common(temp_dir)]
    if section:
        download += ["-b", "%ds" % int(section[0]), "-e", "%ds" % math.ceil(section[1])]
    return [("download", download)] + finishing(cli, video_path, mode, ffmpeg, size, temp_dir,
                                                 embedded=True)


def _common(temp_dir):
    common = ["--banner", "false", "--collision", "Overwrite"]
    return common + ["--temp-path", temp_dir] if temp_dir else common


def embedding(cli, json_path, temp_dir=None):
    """
    Шаг, который встраивает в .chat.json смайлы Twitch, 7TV, BTTV и FFZ
    и значки: такой чат смотрится в окне и рисуется без сети. TwitchDownloader
    пишет во временный файл рядом — (шаг, команда, временный файл); на место
    .chat.json его ставит вызывающий, когда шаг удался.
    """
    temporary = json_path[:-len(".json")] + ".embedding.json"
    return ("embed", [*cli, "chatupdate", "-i", json_path, "-o", temporary, "--embed-missing",
                      *_common(temp_dir)]), temporary


def finishing(cli, video_path, mode, ffmpeg=None, size=(422, 1080, 18), temp_dir=None,
              embedded=False):
    """
    Из готового .chat.json — .chat.html и, если mode == render, видео чата.
    embedded — смайлы и значки уже внутри JSON (чат записи). У чата эфира
    их нет: TwitchDownloader скачивает их сам, вместе с 7TV, BTTV и FFZ.
    """
    paths = chat_paths(video_path)
    html = [*cli, "chatupdate", "-i", paths["json"], "-o", paths["html"], *_common(temp_dir)]
    if not embedded:
        html.append("--embed-missing")
    steps = [("html", html)]
    if mode == RENDER:
        width, height, font = size
        render = [*cli, "chatrender", "-i", paths["json"], "-o", paths["video"],
                  "-w", str(width), "-h", str(height), "--font-size", str(font), *_common(temp_dir)]
        if embedded:
            # --offline: смайлы и значки уже внутри JSON — без него TwitchDownloader
            # заново качает их из сети, и видео рисуется в несколько раз дольше
            render.append("--offline")
        if ffmpeg:
            render += ["--ffmpeg-path", ffmpeg]
        steps.append(("render", render))
    return steps


def explain(lines):
    """Самое понятное из вывода упавшего TwitchDownloaderCLI."""
    message = _raw_error(lines)
    # частые случаи — своими словами
    if "Invalid VOD for clip" in message:
        return tr("запись, из которой вырезан клип, удалена — чата у клипа больше нет")
    if "Invalid VOD" in message:
        return tr("запись удалена или недоступна — чата нет")
    return message


def _raw_error(lines):
    for line in reversed(lines):
        if line.startswith("[ERROR] - "):
            return line[len("[ERROR] - "):]
    # необработанное исключение .NET: «---> System.X: текст» — самое точное
    for pattern in (r"--->\s*[\w.]+?: (.+)", r"Unhandled exception\. [\w.]+?: (.+)"):
        for line in lines:
            match = re.search(pattern, line)
            if match:
                return match.group(1).strip()
    return lines[-1] if lines else ""


def run(steps, on_progress=None, on_process=None, stopped=lambda: False):
    """
    Выполняет шаги по очереди. on_progress(шаг, проценты или None),
    on_process(процесс) — чтобы его можно было погасить; stopped() — не
    остановили ли загрузку. Остановили — ChatStopped, упал — ChatError.
    """
    for stage, command in steps:
        if stopped():
            raise ChatStopped()
        if on_progress:
            on_progress(stage, None)
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, creationflags=util.subprocess_flags())
        except OSError as error:
            raise ChatError(tr("не удалось запустить TwitchDownloader: {error}", error=error))
        if on_process:
            on_process(process)
        head, tail, rest = [], [], b""
        with process.stdout:
            # прогресс TwitchDownloader переписывает строку через \r — читаем кусками
            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    break
                *complete, rest = _LINES.split(rest + chunk)
                for raw in complete:
                    line = raw.decode("utf-8", "replace").strip()
                    match = _PROGRESS.match(line)
                    if match:
                        if on_progress:
                            on_progress(stage, min(100, int(match.group(1))))
                    elif line:
                        if len(head) < _HEAD:
                            head.append(line)
                        else:
                            tail = (tail + [line])[-_TAIL:]
        process.wait()
        if stopped() or process.returncode != 0:
            # недоделанный файл не должен сойти за готовый: по нему решают,
            # доделывать ли чат при следующем запуске
            if "-o" in command:
                util.remove_quietly(command[command.index("-o") + 1])
        if stopped():
            raise ChatStopped()
        if process.returncode != 0:
            lines = head + tail + ([rest.decode("utf-8", "replace").strip()] if rest.strip() else [])
            raise ChatError(explain(lines) or tr("TwitchDownloader завершился с кодом {code}",
                                                 code=process.returncode))
