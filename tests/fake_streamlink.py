# -*- coding: utf-8 -*-
"""
Заглушка streamlink для тестов записи. Поведение задаёт FAKE_STREAMLINK_MODE:
  once     записать немного данных и выйти, как при конце эфира
  forever  писать, пока не убьют
  empty    ничего не записать и пожаловаться, как на стрим для подписчиков
"""

import os
import sys
import time

args = sys.argv[1:]
mode = os.environ.get("FAKE_STREAMLINK_MODE", "once")
# рекордер удваивает фигурные скобки для шаблонизатора streamlink
path = args[args.index("-o") + 1].replace("{{", "{").replace("}}", "}")

print("[cli][info] Opening stream: best (hls)", flush=True)
if mode == "empty":
    print("error: This stream is subscriber-only", flush=True)
    sys.exit(1)

with open(path, "wb") as output:
    if mode == "once":
        output.write(b"\x47" * 188 * 50)
    else:
        while True:
            output.write(b"\x47" * 188 * 10)
            output.flush()
            time.sleep(0.1)
