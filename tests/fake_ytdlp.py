# -*- coding: utf-8 -*-
"""
Заглушка yt-dlp для тестов загрузок. Поведение задаёт FAKE_YTDLP_MODE:
  ok           скачать: прогресс, склейка, готовый файл (недокачанное продолжает)
  slow         качать в .part, пока не убьют
  fail         ошибка, как у записи только для подписчиков
  nofile       выйти без ошибки, но и без файла
  inspect_fail ошибка ещё на вопросе "что по ссылке"
Ответ на -J берётся из файла FAKE_YTDLP_INFO, иначе — запись эфира Twitch.
Каждый запуск дописывает свои аргументы в FAKE_YTDLP_LOG — и путь
к обновлённому yt-dlp, если программа его передала.
"""

import json
import os
import sys
import time

VIDEO = {
    "_type": "video", "extractor_key": "TwitchVod", "id": "v123",
    "title": "Тест {x} 100%", "uploader": "Streamer", "uploader_id": "streamer",
    "channel": None, "timestamp": 1792875046, "duration": 3600,
    "thumbnail": "https://example.com/thumb.jpg",
    "webpage_url": "https://www.twitch.tv/videos/123",
    "formats": [
        {"format_id": "Audio_Only", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "mp4"},
        {"format_id": "720p60", "height": 720, "fps": 60.0, "ext": "mp4"},
        {"format_id": "1080p60", "height": 1080, "fps": 60.0, "ext": "mp4"},
    ],
}

args = sys.argv[1:]
mode = os.environ.get("FAKE_YTDLP_MODE", "ok")
log = os.environ.get("FAKE_YTDLP_LOG")


def note(**data):
    if log:
        with open(log, "a", encoding="utf-8") as target:
            target.write(json.dumps(dict(data, args=args, copy=os.environ.get("STREAMVAULT_YTDLP")),
                                    ensure_ascii=False) + "\n")


def progress(**data):
    print("SVP " + json.dumps(data), flush=True)


if "-J" in args:
    note(kind="inspect")
    if mode == "inspect_fail":
        print("ERROR: [twitch:vod] 1: Video does not exist", file=sys.stderr)
        sys.exit(1)
    source = os.environ.get("FAKE_YTDLP_INFO")
    if source:
        with open(source, encoding="utf-8") as info:
            print(info.read())
    else:
        print(json.dumps(VIDEO, ensure_ascii=False))
    sys.exit(0)

template = args[args.index("-o") + 1]
base = template[:-len(".%(ext)s")].replace("%%", "%")
final, partial = base + ".mp4", base + ".mp4.part"
resumed = os.path.exists(partial)
note(kind="download", resumed=resumed)

if mode == "fail":
    print("ERROR: [twitch:vod] v123: This video is only available to subscribers", flush=True)
    sys.exit(1)
if mode == "nofile":
    sys.exit(0)

with open(partial, "ab") as output:
    if mode == "slow":
        while True:
            output.write(b"x" * 1000)
            output.flush()
            progress(status="downloading", downloaded_bytes=output.tell(),
                     total_bytes=10 ** 6, speed=10000.0, eta=99)
            time.sleep(0.1)
    for step in range(1, 4):
        output.write(b"x" * 1000)
        progress(status="downloading", downloaded_bytes=output.tell(),
                 total_bytes=output.tell() + (3 - step) * 1000, speed=5000.0, eta=3 - step)

progress(status="finished", downloaded_bytes=os.path.getsize(partial))
print('SVPP {"status": "started", "postprocessor": "Merger"}', flush=True)
time.sleep(0.2)
print('SVPP {"status": "finished", "postprocessor": "Merger"}', flush=True)
os.replace(partial, final)
print("SVFILE " + os.path.abspath(final), flush=True)
