# -*- coding: utf-8 -*-
"""Перевод: у каждой строки есть английский вариант, язык выбирается по системе."""
import ast
import html
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from streamvault import i18n
from streamvault.settings import Settings

PACKAGE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "streamvault")
WEB = os.path.join(PACKAGE, "ui", "web")
FIELDS = re.compile(r"\{(\w+)\}")
LITERAL = r'"(?:[^"\\\n]|\\.)*"'
# консольные команды намеренно остаются на русском
UNTRANSLATED = ("cli.py", "i18n.py")


def python_keys():
    keys = set()
    for folder, _, names in os.walk(PACKAGE):
        for name in names:
            if not name.endswith(".py") or name in UNTRANSLATED:
                continue
            with open(os.path.join(folder, name), encoding="utf-8") as source:
                tree = ast.parse(source.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("tr", "plural"):
                    index = 0 if node.func.id == "tr" else 1
                    if len(node.args) > index and isinstance(node.args[index], ast.Constant):
                        keys.add(node.args[index].value)
    return keys


def javascript_sources():
    """Модули окна — все .js, кроме самой таблицы перевода."""
    return sorted(name for name in os.listdir(WEB) if name.endswith(".js") and name != "i18n.js")


def javascript_keys():
    text = ""
    for name in javascript_sources():
        with open(os.path.join(WEB, name), encoding="utf-8") as source:
            text += source.read() + "\n"
    keys = set()
    for match in re.finditer(r"\b(t|tp)\(", text):
        pos = match.end()
        if match.group(1) == "tp":
            pos = text.find(",", pos) + 1
        parts = []
        while True:
            literal = re.compile(r"\s*(" + LITERAL + ")").match(text, pos)
            if not literal:
                break
            parts.append(json.loads(literal.group(1)))
            pos = literal.end()
            plus = re.compile(r"\s*\+(?=\s*\")").match(text, pos)
            if not plus:
                break
            pos = plus.end()
        if parts:
            keys.add("".join(parts))
    # постоянные таблицы переводятся в момент показа: t(KIND_LABELS[kind])
    for name in ("KIND_LABELS", "PERIOD_LABELS", "DOWNLOAD_QUALITIES", "LIBRARY_KINDS",
                 "VIDEO_KIND_LABELS", "LIVE_QUALITY_NOTES", "CHAT_MODES", "CHAT_STAGES", "YTDLP_TIPS"):
        start = text.index("const %s =" % name)
        for literal in re.findall(LITERAL, text[start:text.index(";\n", start)]):
            value = json.loads(literal)
            if re.search("[а-яё]", value, re.I):
                keys.add(value)
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as source:
        markup = source.read()
    keys.update(html.unescape(value) for value in
                re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', markup))
    return keys


def javascript_table():
    with open(os.path.join(WEB, "i18n.js"), encoding="utf-8") as source:
        text = source.read()
    body = text[text.index("const EN = {") + len("const EN = "):]
    body = body[:body.index("};") + 1]
    return json.loads(re.sub(r",\s*}", "}", body))


def python_literals():
    """Все строки в коде программы: ключ перевода бывает и не прямо в tr() — в таблице причин."""
    found = set()
    for folder, _, names in os.walk(PACKAGE):
        for name in names:
            if name.endswith(".py") and name != "i18n.py":
                with open(os.path.join(folder, name), encoding="utf-8") as source:
                    tree = ast.parse(source.read())
                found.update(node.value for node in ast.walk(tree)
                             if isinstance(node, ast.Constant) and isinstance(node.value, str))
    return found


def javascript_literals():
    """Все строки модулей окна (и склеенные через +) и подписи index.html."""
    found = set()
    for name in javascript_sources():
        with open(os.path.join(WEB, name), encoding="utf-8") as source:
            text = source.read()
        for chain in re.finditer(LITERAL + r"(?:\s*\+\s*" + LITERAL + r")*", text):
            parts = [json.loads(part) for part in re.findall(LITERAL, chain.group(0))]
            found.add("".join(parts))
            found.update(parts)
    with open(os.path.join(WEB, "index.html"), encoding="utf-8") as source:
        found.update(html.unescape(value) for value in
                     re.findall(r'data-i18n(?:-placeholder|-title)?="([^"]+)"', source.read()))
    return found


class CoverageTest(unittest.TestCase):

    def check(self, keys, table):
        missing = sorted(key for key in keys if key not in table)
        self.assertEqual(missing, [], "нет английского перевода")
        for key in keys:
            self.assertEqual(set(FIELDS.findall(key)), set(FIELDS.findall(table[key])),
                             "подстановки разошлись: %r" % key)
            if key.count("|") == 2:
                self.assertEqual(table[key].count("|"), 1, "формы числа: %r" % key)

    def test_python_messages(self):
        self.check(python_keys(), i18n.STRINGS)

    def test_window(self):
        self.check(javascript_keys(), javascript_table())

    def test_no_unused_python_strings(self):
        # строка таблицы, которой нет в коде, — забытая после переделки
        literals = python_literals()
        self.assertEqual([key for key in i18n.STRINGS if key not in literals], [],
                         "перевод есть, а строки в коде нет")

    def test_no_unused_window_strings(self):
        literals = javascript_literals()
        self.assertEqual([key for key in javascript_table() if key not in literals], [],
                         "перевод есть, а строки в окне нет")


class LanguageTest(unittest.TestCase):

    def tearDown(self):
        i18n.set_language(i18n.RUSSIAN)

    def test_from_locale(self):
        for name, expected in (("ru_RU", "ru"), ("Russian_Russia", "ru"), ("en_US", "en"),
                               ("zh_CN", "en"), ("uk_UA", "en"), ("", "en")):
            self.assertEqual(i18n.language_from_locale(name), expected, name)

    def test_translate_and_plural(self):
        i18n.set_language("en")
        self.assertEqual(i18n.tr("{tag}: эфир закончился", tag="kick:xqc"), "kick:xqc: the stream has ended")
        # подстановка может называться text: первый аргумент только позиционный
        self.assertIn("“1:x”", i18n.tr("не понимаю время: «{text}» (нужно Ч:ММ:СС, ММ:СС или секунды)",
                                       text="1:x"))
        self.assertEqual(i18n.tr("строки нет в таблице"), "строки нет в таблице")
        i18n.set_language("ru")
        self.assertEqual([i18n.plural(n, "файл|файла|файлов") for n in (1, 3, 5, 11, 21)],
                         ["файл", "файла", "файлов", "файлов", "файл"])
        i18n.set_language("zh")   # нет такого — язык не меняется
        self.assertEqual(i18n.current_language(), "ru")

    def test_first_start_takes_system_language(self):
        folder = tempfile.mkdtemp()
        try:
            settings = Settings(os.path.join(folder, "settings.json"))
            with mock.patch.object(i18n, "detect_system_language", return_value="en"):
                self.assertEqual(i18n.init(settings), "en")
            self.assertEqual(Settings.load(settings.path)["language"], "en")
            # дальше — выбранный, а не системный
            settings.update(language="ru")
            with mock.patch.object(i18n, "detect_system_language", return_value="en"):
                self.assertEqual(i18n.init(settings), "ru")
            # консольная команда настройки не трогает
            fresh = Settings(os.path.join(folder, "other.json"))
            with mock.patch.object(i18n, "detect_system_language", return_value="en"):
                i18n.init(fresh, persist=False)
            self.assertFalse(os.path.exists(fresh.path))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
