# -*- coding: utf-8 -*-
"""
Чаты всех записей сразу: поиск по ним и сводка эфира.

Чат записи — .chat.json рядом с видео (у записей эфиров всех площадок и
у скачанного с чатом). Открывать каждый при каждом поиске долго: в нём
встроены картинки смайлов, а записей бывают сотни. Поэтому сообщения
один раз переписываются в базу SQLite (cache/chats.sqlite) — только ник,
текст и названия смайлов, — и дальше ищутся там. Изменился .chat.json
(размер или время) — его сообщения переписываются заново, удалён — уходят.

Поиск — подстрока без учёта регистра, как в поиске проигрывателя: «gg»
находит и «GG», и «ggwp». Регистр снимает Python (casefold), а не SQLite:
SQLite сам умеет это только для латиницы.
"""

import collections
import logging
import os
import sqlite3
import threading
import time

from .archive import chat
from .chatreplay import ChatReplay, activity, activity_bucket, peaks

# Сколько записей с совпадениями и сколько сообщений у каждой отдавать окну.
RESULT_FILES = 50
RESULT_MESSAGES = 20
# Дольше этого один поиск не переписывает чаты в базу: остальное — в следующий раз.
SYNC_BUDGET = 4.0
TOP = 10
VIDEO_EXTENSIONS = (".mp4", ".ts", ".mkv", ".webm", ".m4a", ".mp3")
SCHEMA = """
CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, key TEXT, video TEXT);
CREATE TABLE IF NOT EXISTS messages (file INTEGER, offset REAL, name TEXT, text TEXT, emotes TEXT,
                                     haystack TEXT, who TEXT);
CREATE INDEX IF NOT EXISTS messages_file ON messages (file, offset);
"""


def _key(path):
    stat = os.stat(path)
    return "%d|%d" % (stat.st_size, int(stat.st_mtime))


def video_of(json_path):
    """Видео чата: рядом, с тем же именем без .chat.json."""
    base = json_path[:-len(chat.SUFFIXES["json"])]
    for extension in VIDEO_EXTENSIONS:
        if os.path.exists(base + extension):
            return base + extension
    return ""


def _rows(replay):
    """Сообщения ChatReplay -> строки базы: (время, ник, текст, смайлы, где искать, ник для поиска)."""
    for offset, name, _, _, parts in replay.messages:
        text = "".join(part if isinstance(part, str) else part[1] for part in parts)
        emotes = "\n".join(part[1] for part in parts if not isinstance(part, str) and part[1])
        yield offset, name, text, emotes, (name + " " + text).casefold(), name.casefold()


class ChatIndex:
    """folders() — папки, где искать чаты: записи эфиров и загрузки."""

    def __init__(self, path, folders):
        self._path = path
        self._folders = folders
        self._lock = threading.Lock()
        self._db = None

    def _connect(self):
        if self._db is None:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            try:
                self._db = sqlite3.connect(self._path, check_same_thread=False)
                self._db.executescript(SCHEMA)
            except sqlite3.DatabaseError as error:
                # кэш испорчен — начать заново: всё в нём восстановимо из .chat.json
                logging.warning("база чатов испорчена (%s), начинаю заново", error)
                if self._db is not None:
                    self._db.close()
                os.remove(self._path)
                self._db = sqlite3.connect(self._path, check_same_thread=False)
                self._db.executescript(SCHEMA)
        return self._db

    # ------------------------------------------------------------ база

    def _chat_files(self):
        found = []
        for folder in self._folders():
            for root, _, names in os.walk(folder):
                found.extend(os.path.join(root, name) for name in names
                             if name.endswith(chat.SUFFIXES["json"]))
        return found

    def _index(self, db, path, known):
        """Переписать чат в базу. False — не читается (останется непроиндексированным)."""
        try:
            key = _key(path)
            replay = ChatReplay(path)
        except (OSError, ValueError, UnicodeDecodeError) as error:
            logging.debug("чат %s не читается: %s", path, error)
            return False
        with db:
            if path in known:
                db.execute("DELETE FROM messages WHERE file = ?", (known[path][0],))
                db.execute("UPDATE files SET key = ?, video = ? WHERE id = ?", (key, video_of(path), known[path][0]))
                file_id = known[path][0]
            else:
                file_id = db.execute("INSERT INTO files (path, key, video) VALUES (?, ?, ?)",
                                     (path, key, video_of(path))).lastrowid
            db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
                           ((file_id,) + row for row in _rows(replay)))
        return True

    def sync(self, budget=SYNC_BUDGET, only=None):
        """
        Привести базу к чатам на диске. budget — сколько секунд переписывать
        (остальное — в следующий раз); only — только эти чаты. Возвращает,
        сколько чатов ещё не в базе.
        """
        with self._lock:
            db = self._connect()
            known = {path: (file_id, key) for file_id, path, key in db.execute("SELECT id, path, key FROM files")}
            files = list(only) if only is not None else self._chat_files()
            if only is None:
                present = set(files)
                with db:
                    for path, (file_id, _) in known.items():
                        if path not in present:
                            db.execute("DELETE FROM messages WHERE file = ?", (file_id,))
                            db.execute("DELETE FROM files WHERE id = ?", (file_id,))
            stale = []
            for path in files:
                try:
                    if path not in known or known[path][1] != _key(path):
                        stale.append(path)
                except OSError:
                    continue
            started = time.monotonic()
            left = len(stale)
            for path in stale:
                if budget is not None and time.monotonic() - started > budget:
                    break
                self._index(db, path, known)
                left -= 1
            return left

    # ------------------------------------------------------------ поиск

    def search(self, text, by="text"):
        """
        Сообщения со словом во всех чатах: by="text" — в нике, тексте и
        смайлах, by="name" — только в нике. Ответ: {"results": [{"video",
        "chat", "count", "messages": [[время, ник, текст], …]}], "pending":
        сколько чатов ещё не просмотрено — окно спросит ещё раз}.
        """
        needle = str(text).strip().casefold()
        pending = self.sync()
        if not needle:
            return {"results": [], "pending": pending}
        column = "who" if by == "name" else "haystack"
        with self._lock:
            db = self._connect()
            counts = db.execute(
                "SELECT f.id, f.path, f.video, count(*) FROM messages m JOIN files f ON f.id = m.file "
                "WHERE instr(m.%s, ?) > 0 GROUP BY f.id ORDER BY f.path DESC LIMIT ?" % column,
                (needle, RESULT_FILES)).fetchall()
            results = []
            for file_id, path, video, count in counts:
                messages = db.execute(
                    "SELECT offset, name, text FROM messages WHERE file = ? AND instr(%s, ?) > 0 "
                    "ORDER BY offset LIMIT ?" % column, (file_id, needle, RESULT_MESSAGES)).fetchall()
                results.append({"chat": path, "video": video, "count": count,
                                "messages": [list(message) for message in messages]})
        return {"results": results, "pending": pending}

    # ------------------------------------------------------------ сводка

    def summary(self, json_paths, durations=None):
        """
        Сводка чата эфира из частей: сообщений, зрителей, самые активные,
        частые смайлы, всплески. durations — длительность каждой части
        (для графика), None — по последнему сообщению.
        """
        paths = [path for path in json_paths if os.path.isfile(path)]
        self.sync(budget=None, only=paths)
        durations = list(durations or []) + [None] * len(paths)
        names, emotes = collections.Counter(), collections.Counter()
        total, moments, per_part = 0, [], []
        with self._lock:
            db = self._connect()
            for index, path in enumerate(paths):
                row = db.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
                if row is None:
                    per_part.append({"messages": 0})
                    continue
                times = []
                for offset, name, line in db.execute(
                        "SELECT offset, name, emotes FROM messages WHERE file = ? ORDER BY offset", (row[0],)):
                    times.append(offset)
                    names[name] += 1
                    if line:
                        emotes.update(line.split("\n"))
                total += len(times)
                per_part.append({"messages": len(times)})
                if times:
                    bucket = activity_bucket(durations[index] or times[-1])
                    counts = activity(times, durations[index] or times[-1], bucket)
                    moments.extend({"part": index, "at": peak * bucket, "length": bucket, "count": counts[peak]}
                                   for peak in peaks(counts))
        moments.sort(key=lambda moment: -moment["count"])
        return {
            "messages": total,
            "chatters": len(names),
            "parts": per_part,
            "top_chatters": [list(pair) for pair in names.most_common(TOP)],
            "top_emotes": [list(pair) for pair in emotes.most_common(TOP)],
            # самые сильные всплески чата — в порядке эфира
            "moments": sorted(moments[:TOP], key=lambda moment: (moment["part"], moment["at"])),
        }

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
