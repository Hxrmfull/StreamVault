# -*- coding: utf-8 -*-
# Чат эфиров в тестах по умолчанию выключен: иначе каждый тест записи канала
# Twitch подключался бы к настоящему чату. Тесты чата включают его сами.
from streamvault.settings import DEFAULTS

DEFAULTS["live_chat"] = "none"
