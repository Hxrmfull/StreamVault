# -*- coding: utf-8 -*-
"""
Заглушка yt-dlp, читающего чат эфира YouTube: строки из файла FAKE_CHAT_LINES
по одной дописываются в <-o>.live_chat.json, как у настоящего. Затем
FAKE_CHAT_MODE: wait — ждать, пока не убьют; fail — выйти с ошибкой;
done — выйти, как после чата прошедшего эфира.
Каждый запуск дописывает свои аргументы в FAKE_CHAT_LOG.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
with open(os.environ["FAKE_CHAT_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args, ensure_ascii=False) + "\n")

with open(os.environ["FAKE_CHAT_LINES"], encoding="utf-8") as source:
    lines = source.read().splitlines()
output = args[args.index("-o") + 1] + ".live_chat.json"
with open(output, "a", encoding="utf-8") as target:
    for line in lines:
        target.write(line + "\n")
        target.flush()
        time.sleep(0.05)

if os.environ.get("FAKE_CHAT_MODE") == "done":
    sys.exit(0)
if os.environ.get("FAKE_CHAT_MODE") == "fail":
    print("ERROR: [youtube] чат недоступен")
    sys.exit(1)
while True:
    time.sleep(1)
