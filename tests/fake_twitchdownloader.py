# -*- coding: utf-8 -*-
"""
Заглушка TwitchDownloaderCLI для тестов чата. Поведение задаёт FAKE_TDL_MODE:
  ok     сделать файл (-o) и вывести прогресс, как настоящий: строки через \\r
  fail   упасть, как на удалённой записи
  slow   начать файл (-o) и выводить прогресс, пока не убьют
Каждый запуск дописывает свои аргументы в FAKE_TDL_LOG.
"""

import json
import os
import sys
import time

args = sys.argv[1:]
mode = os.environ.get("FAKE_TDL_MODE", "ok")
log = os.environ.get("FAKE_TDL_LOG")
if log:
    with open(log, "a", encoding="utf-8") as target:
        target.write(json.dumps(args, ensure_ascii=False) + "\n")

if mode == "fail":
    print("Unhandled exception. System.AggregateException: One or more errors occurred. "
          "(Invalid VOD, deleted/expired VOD possibly?)")
    print(" ---> System.NullReferenceException: Invalid VOD, deleted/expired VOD possibly?")
    print("   at TwitchDownloaderCore.ChatDownloader.InitChatRoot()")
    sys.exit(127)

stage = {"chatdownload": "Downloading", "chatupdate": "Updating Video Info",
         "chatrender": "Rendering Video"}.get(args[0], "Working")
if mode == "slow":
    # как настоящий: файл появляется сразу и дописывается, пока идёт работа
    with open(args[args.index("-o") + 1], "w", encoding="utf-8") as target:
        target.write("недоделано")
    percent = 0
    while True:
        sys.stdout.write("\r[STATUS] - %s %d%%" % (stage, percent % 100))
        sys.stdout.flush()
        percent += 1
        time.sleep(0.05)

for percent in (0, 50, 100):
    sys.stdout.write("[STATUS] - %s %d%%\r" % (stage, percent))
    sys.stdout.flush()
sys.stdout.write("\n[STATUS] - Writing Output File\r\n")
output = args[args.index("-o") + 1]
with open(output, "w", encoding="utf-8") as target:
    target.write("чат " + args[0])
