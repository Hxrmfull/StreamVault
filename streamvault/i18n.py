# -*- coding: utf-8 -*-
"""
Перевод: русский и английский.

Ключ перевода — сама русская строка, как в gettext: код читается как есть,
а забытый перевод виден сразу — вместо английского останется русский текст.
Тест проверяет, что у каждой строки, переданной в tr(), есть перевод.

Язык выбирается при первом запуске по языку Windows и дальше хранится
в настройках. Всё, что не русское, показывается по-английски: своего
перевода для других языков нет, а английский понятнее русского для иностранца.

Переводятся окно, сообщения об ошибках, журнал и консольные команды —
у них язык тот же, что у окна (cli.choose_language).
"""

import locale
import os

RUSSIAN, ENGLISH = "ru", "en"
LANGUAGES = (RUSSIAN, ENGLISH)
LANGUAGE_NAMES = {RUSSIAN: "Русский", ENGLISH: "English"}

# Основной язык Windows (младшие 10 бит LANGID) у русского.
_WINDOWS_RUSSIAN = 0x19

_current = RUSSIAN


def language_from_locale(name):
    """Язык по имени локали: "ru_RU", "en-US", "Russian_Russia"..."""
    name = (name or "").strip().lower()
    return RUSSIAN if name.startswith(("ru", "russian")) else ENGLISH


def detect_system_language():
    """Язык интерфейса Windows, а не формат дат и чисел: его человек и читает."""
    if os.name == "nt":
        try:
            import ctypes
            langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return RUSSIAN if langid & 0x3FF == _WINDOWS_RUSSIAN else ENGLISH
        except (AttributeError, OSError):
            pass
    try:
        name = locale.getlocale()[0] or ""
    except ValueError:
        name = ""
    return language_from_locale(name or os.environ.get("LANG", ""))


def set_language(code):
    """Переключает язык. Незнакомый код не меняет ничего."""
    global _current
    if code in LANGUAGES:
        _current = code


def current_language():
    return _current


def tr(message, /, **fields):
    """
    Строка на текущем языке. Без перевода в таблице — русская: так пропуск
    виден, а программа не падает. Первый аргумент только позиционный:
    подстановки могут называться как угодно, хоть text.
    """
    if _current == ENGLISH:
        message = STRINGS.get(message, message)
    return message.format(**fields) if fields else message


def plural(count, forms):
    """
    Слово для числа. forms — русские формы через "|": "файл|файла|файлов";
    английские "file|files" лежат в той же таблице.
    """
    if _current == ENGLISH:
        english = STRINGS.get(forms, forms).split("|")
        return english[0] if abs(count) == 1 else english[-1]
    one, few, many = forms.split("|")
    n = abs(int(count)) % 100
    if 10 < n < 20:
        return many
    n %= 10
    return one if n == 1 else few if 1 < n < 5 else many


def init(settings, persist=True):
    """
    Язык из настроек, а при первом запуске — язык Windows. persist=False —
    не записывать выбор: консольной команде незачем трогать settings.json.
    """
    chosen = settings.get("language") or ""
    if chosen not in LANGUAGES:
        chosen = detect_system_language()
        if persist:
            settings.update(language=chosen)
    set_language(chosen)
    return chosen


# Русская строка -> английская.
STRINGS = {
    'отрезок начинается после конца записи: она длится {length}':
        'the section starts after the end of the recording: it is {length} long',
    'загрузка добавлена: {name}': 'download added: {name}',
    'загрузки {id} нет в списке': 'download {id} is not in the list',
    'скачиваю: {name}': 'downloading: {name}',
    'yt-dlp не установлен: python -m pip install yt-dlp':
        'yt-dlp is not installed: python -m pip install yt-dlp',
    'yt-dlp завершился с кодом {code}': 'yt-dlp exited with code {code}',
    'скачано: {path}': 'downloaded: {path}',
    'папка для загрузок недоступна: {path} ({error})':
        'downloads folder is not available: {path} ({error})',
    '{path} не читается ({error}), начинаю с пустой очереди':
        '{path} cannot be read ({error}), starting with an empty queue',
    'токен Twitch не годится: {error}': 'the Twitch token is not valid: {error}',
    'не скачалось: {name} — {error}': 'download failed: {name} — {error}',
    'очередь загрузок не сохранилась: {error}': 'the download queue was not saved: {error}',
    'сбой программы: {error}': 'program error: {error}',
    'по ссылке список из {count} видео — добавь нужные по отдельности':
        'the link points to a list of {count} videos — add the ones you need one by one',
    'загрузка {id}: сбой': 'download {id}: error',
    'для {platform} список записей канала не поддерживается — вставь ссылку на саму запись':
        'channel video lists are not supported for {platform} — paste a link to the video itself',
    'у {platform} нет списка «{kind}», есть: {kinds}':
        '{platform} has no “{kind}” list, available: {kinds}',
    'нужно имя канала, а не ссылка: {link}': 'a channel name is needed, not a link: {link}',
    'срок для клипов: {periods}': 'clip periods: {periods}',
    'лучшее': 'best',
    'токен Twitch состоит только из латинских букв и цифр — скопируй значение куки auth-token целиком':
        'a Twitch token contains only Latin letters and digits — copy the whole value of the auth-token cookie',
    'не понимаю время: «{text}» (нужно Ч:ММ:СС, ММ:СС или секунды)':
        'cannot read the time “{text}” (use H:MM:SS, MM:SS or seconds)',
    'отрезок пишется как НАЧАЛО-КОНЕЦ, например 1:00:00-1:30:00':
        'write a section as START-END, for example 1:00:00-1:30:00',
    'конец отрезка должен быть позже начала': 'the end of the section must come after its start',
    'только звук': 'audio only',
    'yt-dlp ответил непонятно': 'yt-dlp returned an unreadable answer',
    'не удалось запустить yt-dlp: {error}': 'could not start yt-dlp: {error}',
    'yt-dlp не ответил за {seconds} с': 'yt-dlp did not answer within {seconds} s',
    'подписчик на события {kind} упал': 'the {kind} event subscriber crashed',
    'ffmpeg по пути из настроек не найден ({path}), ищу сама':
        'ffmpeg was not found at the path from the settings ({path}), searching automatically',
    '{host} ответил {status}': '{host} answered {status}',
    'перенаправление на чужой адрес ({url}) — туда программа не обращается':
        'redirect to an unknown address ({url}) — the program does not connect there',
    'не удалось узнать версию ffmpeg: {problems}': 'could not find out the ffmpeg version: {problems}',
    'программа': 'the program',
    'контрольные суммы не совпадают ({sources}) — возможна подмена, установка остановлена':
        'the checksums do not match ({sources}) — the file may have been tampered with, installation stopped',
    'не удалось сверить контрольную сумму в двух местах: {problems}':
        'could not check the checksum in two places: {problems}',
    'медленно: {speed} КБ/с': 'too slow: {speed} KB/s',
    'связь оборвалась ({error})': 'the connection dropped ({error})',
    'связь оборвалась на {got} из {size} байт': 'the connection dropped at {got} of {size} bytes',
    'скачанный ffmpeg не запустился или назвал другую версию':
        'the downloaded ffmpeg did not start or reported a different version',
    'нет связи с {host}: {error}': 'cannot connect to {host}: {error}',
    'контрольная сумма в непонятном виде': 'the checksum is in an unreadable format',
    'архив ffmpeg повреждён': 'the ffmpeg archive is damaged',
    'в архиве нет {name}': 'the archive has no {name}',
    'отправлено в корзину: {path}': 'moved to the Recycle Bin: {path}',
    'ключи Twitch не заданы — каналы Twitch проверяются через streamlink':
        'Twitch keys are not set — Twitch channels are checked through streamlink',
    'токен Twitch получен, действителен ~{days} сут':
        'Twitch token received, valid for ~{days} days',
    '{channel}: превышен лимит запросов Twitch': '{channel}: Twitch request limit exceeded',
    '{channel}: Twitch ответил {status}': '{channel}: Twitch answered {status}',
    'токен Twitch: ключи приложения отклонены ({status}). Проверь Client ID и Client Secret в настройках':
        'Twitch token: the application keys were rejected ({status}). Check Client ID and Client Secret in the settings',
    'токен Twitch: ответ {status}, повтор через {delay} с':
        'Twitch token: answer {status}, retrying in {delay} s',
    'недопустимое имя канала Twitch (только латиница, цифры и _)':
        'invalid Twitch channel name (only Latin letters, digits and _)',
    '{channel}: непонятный ответ Twitch': '{channel}: unreadable Twitch answer',
    'такого канала на Twitch нет': 'there is no such channel on Twitch',
    '{channel}: проверка не уложилась в {seconds} с':
        '{channel}: the check did not finish within {seconds} s',
    '{channel}: не удалось запустить streamlink ({error})':
        '{channel}: could not start streamlink ({error})',
    'токен Twitch: сеть недоступна ({error}), повтор через {delay} с':
        'Twitch token: network unavailable ({error}), retrying in {delay} s',
    'токен Twitch: непонятный ответ, повтор через {delay} с':
        'Twitch token: unreadable answer, retrying in {delay} s',
    'конвертирую в mp4: {name}': 'converting to mp4: {name}',
    'готово: {name}': 'done: {name}',
    'найдена необработанная запись: {name}': 'found an unprocessed recording: {name}',
    'файл исчез, пропускаю: {path}': 'the file is gone, skipping: {path}',
    'пустая запись, удаляю: {name}': 'empty recording, deleting: {name}',
    'перемещено: {name}': 'moved: {name}',
    'ffmpeg не справился с {name} (код {code})': 'ffmpeg failed on {name} (code {code})',
    'сбой обработки {path}: {error}': 'processing error {path}: {error}',
    'доделываю оставшиеся записи ({count})': 'finishing the remaining recordings ({count})',
    'не читается {path} ({error})': 'cannot read {path} ({error})',
    'убран недописанный файл прошлого запуска: {name}':
        'removed an unfinished file from the previous run: {name}',
    'ffmpeg не найден — записи ждут в .ts, пока его не установят (кнопка в окне программы)':
        'ffmpeg not found — recordings wait as .ts until it is installed (button in the program window)',
    'исходник занят ({error}), результат отброшен: {name}':
        'the source file is busy ({error}), result discarded: {name}',
    'повтор обработки {name} (попытка {attempt} из {total}) через {seconds} с':
        'retrying {name} (attempt {attempt} of {total}) in {seconds} s',
    'файл уже обработан кем-то другим: {name}': 'the file was already processed elsewhere: {name}',
    'папка {old} теперь называется {new}': 'folder {old} is now called {new}',
    'записи из папки {old} перенесены в {new}': 'recordings from folder {old} were moved to {new}',
    'не перенёс {path} в {new}: {error}': 'could not move {path} to {new}: {error}',
    'досмотр папок не удался': 'folder rescan failed',
    '{tag}: проверка каждые {seconds} с через {probe}, качество {quality}':
        '{tag}: checking every {seconds} s via {probe}, quality {quality}',
    '{tag}: наблюдение остановлено': '{tag}: watching stopped',
    '{tag} в эфире: {title}': '{tag} is live: {title}',
    '{tag}: проверка через {probe} недоступна, перехожу на {fallback}':
        '{tag}: checking via {probe} is unavailable, switching to {fallback}',
    '{tag}: канал в эфире, но записать его не удаётся, следующая попытка через {seconds} с':
        '{tag}: the channel is live but cannot be recorded, next attempt in {seconds} s',
    '{tag}: останавливаю запись': '{tag}: stopping the recording',
    '{tag}: streamlink завершился за {seconds} с без данных ({count} подряд)':
        '{tag}: streamlink exited after {seconds} s without data ({count} in a row)',
    'streamlink не найден. Установи: python -m pip install streamlink':
        'streamlink not found. Install it: python -m pip install streamlink',
    '{tag}: не в эфире, жду выхода': '{tag}: offline, waiting for the stream',
    'без названия': 'untitled',
    '{tag}: три пустые попытки подряд, возвращаюсь к проверкам':
        '{tag}: three empty attempts in a row, back to checking',
    'streamlink не найден': 'streamlink not found',
    'сбой: {error}': 'error: {error}',
    'записать не удаётся': 'cannot be recorded',
    '{tag}: Twitch раз за разом не принимает свежий токен, перехожу на проверку без API':
        '{tag}: Twitch keeps rejecting a fresh token, switching to checks without the API',
    '{tag}: токен отклонён, обновляю': '{tag}: token rejected, refreshing',
    '{tag}: сбой, повтор через {seconds} с': '{tag}: error, retrying in {seconds} s',
    '{tag}: {reason} — проверь имя. Перепроверю через {minutes} мин':
        '{tag}: {reason} — check the name. Will check again in {minutes} min',
    '{tag}: нет связи, повтор через {seconds} с': '{tag}: no connection, retrying in {seconds} s',
    'платформа отвечает нештатно': 'the platform answers abnormally',
    '{tag}: {reason}, повтор через {seconds} с': '{tag}: {reason}, retrying in {seconds} s',
    'канал не найден': 'channel not found',
    'нет связи': 'no connection',
    'streamlink не найден: без него эфиры не записываются. Установи: python -m pip install streamlink':
        'streamlink not found: live streams cannot be recorded without it. Install it: python -m pip install streamlink',
    '{channel}: добавлен': '{channel}: added',
    '{channel}: убран из списка': '{channel}: removed from the list',
    'канала {channel} нет в списке': 'channel {channel} is not in the list',
    'папка для записей недоступна: {path} ({error})':
        'recordings folder is not available: {path} ({error})',
    'наблюдаю за каналами: {channels}': 'watching channels: {channels}',
    '{channel}: автозапись включена': '{channel}: auto-record on',
    '{channel}: автозапись выключена': '{channel}: auto-record off',
    'канал {channel} уже есть в списке': 'channel {channel} is already in the list',
    'поток {name} упал': 'thread {name} crashed',
    'пустая строка': 'empty input',
    'не понимаю, какой платформе принадлежит ссылка: {link}':
        'cannot tell which platform this link belongs to: {link}',
    'неизвестная платформа «{platform}» в «{text}»': 'unknown platform “{platform}” in “{text}”',
    'не указан канал в «{text}»': 'no channel given in “{text}”',
    'интервал опроса для {platform} задан непонятно ({value}), беру {default} с':
        'the check interval for {platform} is unreadable ({value}), using {default} s',
    '{platform}:{channel} указан дважды, второй раз пропускаю':
        '{platform}:{channel} is listed twice, skipping the second one',
    'настройки: {key} задан непонятно ({value}), оставляю {kept}':
        'settings: {key} is unreadable ({value}), keeping {kept}',
    '{path}: ожидался объект в фигурных скобках': '{path}: an object in curly braces was expected',
    'настройки: незнакомый параметр {key}, оставляю как есть':
        'settings: unknown option {key}, leaving it as is',
    '{path} испорчен: {error}': '{path} is damaged: {error}',
    '{path} не читается: {error}': '{path} cannot be read: {error}',
    'неизвестный вызов: {name}': 'unknown call: {name}',
    'аргументы вызова — список': 'call arguments must be a list',
    'вставь ссылку или имя канала': 'paste a link or a channel name',
    'нужна ссылка, начинающаяся с https://': 'a link starting with https:// is needed',
    'этот файл программа не открывает: {path}': 'the program does not open this file: {path}',
    'нет такой папки: {name}': 'no such folder: {name}',
    'открываются только ссылки http и https': 'only http and https links can be opened',
    'библиотека недоступна': 'the library is not available',
    'установка ffmpeg здесь недоступна': 'ffmpeg installation is not available here',
    'в браузере файл не выбрать — впиши путь руками':
        'a file cannot be picked in the browser — type the path',
    'файла нет: {path}': 'file not found: {path}',
    'это не ffmpeg или он не запускается: {path}':
        'this is not ffmpeg or it does not start: {path}',
    'настройки — объект': 'settings must be an object',
    'запуск вместе с Windows здесь недоступен': 'starting with Windows is not available here',
    'настройки изменены: {keys}': 'settings changed: {keys}',
    'в браузере папку не выбрать — впиши путь руками':
        'a folder cannot be picked in the browser — type the path',
    'вызов {name} из окна упал': 'window call {name} crashed',
    'внутренняя ошибка: {error}': 'internal error: {error}',
    'настройку {key} окно не меняет': 'the window cannot change the {key} setting',
    'запуск вместе с Windows включён': 'starting with Windows is on',
    'запуск вместе с Windows выключен': 'starting with Windows is off',
    '{key} — да или нет': '{key} must be yes or no',
    'незнакомый язык: {value}': 'unknown language: {value}',
    '{key} — от {low} до {high}': '{key} must be from {low} to {high}',
    'refresh — интервалы по платформам': 'refresh must be intervals per platform',
    'уровень лога: {levels}': 'log level: {levels}',
    '{key} — нужно целое число': '{key} must be a whole number',
    'неизвестная платформа: {name}': 'unknown platform: {name}',
    'интервал опроса — целое число секунд': 'the check interval must be a whole number of seconds',
    'StreamVault {version} запущен': 'StreamVault {version} started',
    'Для окна программы нужен pywebview: python -m pip install pywebview\nПока можно открыть StreamVault в браузере: python -m streamvault gui --browser':
        'The program window needs pywebview: python -m pip install pywebview\nMeanwhile StreamVault can be opened in a browser: python -m streamvault gui --browser',
    'StreamVault уже запущен — окном или консольной командой.':
        'StreamVault is already running — as a window or a console command.',
    'ffmpeg не найден: записи не будут конвертироваться в mp4, пока его не установят или не укажут путь в настройках':
        'ffmpeg not found: recordings will not be converted to mp4 until it is installed or its path is set in the settings',
    'pystray не установлен — значка в трее не будет: python -m pip install pystray':
        'pystray is not installed — there will be no tray icon: python -m pip install pystray',
    'закрываю: останавливаю записи и загрузки, доделываю конвертацию':
        'closing: stopping recordings and downloads, finishing conversion',
    'завершено': 'finished',
    'Настройки не читаются: {error}': 'Settings cannot be read: {error}',
    'необработанных записей: {count}, подхватятся при следующем запуске':
        'unprocessed recordings: {count}, they will be picked up on the next start',
    'StreamVault работает в фоне': 'StreamVault is running in the background',
    'Записи продолжаются. Открыть окно или выйти — через значок в трее.':
        'Recordings continue. Open the window or quit from the tray icon.',
    'остановка по Ctrl+C': 'stopping on Ctrl+C',
    'запуск вместе с системой настраивается только в Windows':
        'starting with the system can only be set up on Windows',
    'Пишется: {names}': 'Recording: {names}',
    'Сейчас ничего не пишется': 'Nothing is being recorded',
    ' · загрузок: {count}': ' · downloads: {count}',
    'Открыть StreamVault': 'Open StreamVault',
    'Проверить каналы сейчас': 'Check channels now',
    'Папка записей': 'Recordings folder',
    'Папка загрузок': 'Downloads folder',
    'Выход': 'Quit',
    'значок в трее не запустился': 'the tray icon did not start',
    'Запись началась: {name}': 'Recording started: {name}',
    'действие из меню значка не удалось': 'the tray menu action failed',
    'Эфир пишется в папку записей': 'The stream is being recorded to the recordings folder',
    'Скачано': 'Downloaded',
    'Не скачалось': 'Download failed',
    'корзина поддерживается только в Windows': 'the Recycle Bin is only supported on Windows',
    'не удалось отправить в корзину (код {code}): {path}':
        'could not move to the Recycle Bin (code {code}): {path}',
    'не удалось показать окно': 'could not show the window',
    'слишком много перенаправлений': 'too many redirects',
    '{name} {version} установлен: {path}': '{name} {version} installed: {path}',
    '{name} не установлен: {error}': '{name} was not installed: {error}',
    'установка {name} упала': '{name} installation crashed',
    '{name}: {source} — {problem}, пробую дальше': '{name}: {source} — {problem}, trying the next one',
    '{name} не скачался ни с одного источника: {problems}':
        '{name} could not be downloaded from any source: {problems}',
    '{name}: подозрительный размер файла — {size} байт': '{name}: suspicious file size — {size} bytes',
    'файл скачался не целиком: {got} из {size} байт':
        'the file was not downloaded completely: {got} of {size} bytes',
    'скачанный файл не совпал с контрольной суммой — возможна подмена, ничего не установлено':
        'the downloaded file does not match the checksum — it may have been tampered with, nothing was installed',
    'файл больше заявленного — установка остановлена':
        'the file is larger than stated — installation stopped',
    '{host} прислал непонятный ответ': '{host} sent an unreadable answer',
    'GitHub назвал непонятную версию: {version}': 'GitHub reported an unreadable version: {version}',
    'yt-dlp {version}: обновлений нет, последний выпуск — {latest}':
        'yt-dlp {version}: no updates, the latest release is {latest}',
    'в выпуске {version} нет файла {name}': 'release {version} has no file {name}',
    'в подписанном списке нет суммы для {name}': 'the signed list has no checksum for {name}',
    'в скачанном файле другая версия yt-dlp: {found} вместо {version}':
        'the downloaded file has a different yt-dlp version: {found} instead of {version}',
    'в скачанном файле нет yt-dlp': 'the downloaded file does not contain yt-dlp',
    'на PyPI нет подходящей версии yt-dlp': 'PyPI has no suitable yt-dlp version',
    'на PyPI нет сборки yt-dlp {version}': 'PyPI has no yt-dlp {version} package',
    'не удалось узнать, есть ли новая версия yt-dlp: {problems}':
        'could not find out whether a new yt-dlp version is available: {problems}',
    'обновление yt-dlp здесь недоступно': 'yt-dlp cannot be updated here',
    'подпись списка сумм yt-dlp не сходится ({reason}) — возможна подмена, обновление остановлено':
        'the signature of the yt-dlp checksum list is not valid ({reason}) — it may have been tampered with, update stopped',
    'скачанный yt-dlp не запустился или назвал другую версию':
        'the downloaded yt-dlp did not start or reported a different version',
    'скачанный yt-dlp повреждён': 'the downloaded yt-dlp is damaged',
    'сумма yt-dlp у GitHub не совпадает с подписанной — возможна подмена, обновление остановлено':
        'the yt-dlp checksum on GitHub does not match the signed one — it may have been tampered with, update stopped',
    'броня OpenPGP испорчена': 'the OpenPGP armor is damaged',
    'в броне нет открытого ключа': 'the armor contains no public key',
    'в подписи нет времени создания': 'the signature has no creation time',
    'встроенный ключ не тот: {fingerprint}': 'the built-in key is not the expected one: {fingerprint}',
    'ключ слишком короткий': 'the key is too short',
    'контрольная сумма брони OpenPGP не сходится': 'the OpenPGP armor checksum does not match',
    'не пакет OpenPGP': 'not an OpenPGP packet',
    'неизвестный алгоритм хеша: {id}': 'unknown hash algorithm: {id}',
    'неизвестный обязательный подпакет подписи: {id}': 'unknown critical signature subpacket: {id}',
    'нет заголовка брони OpenPGP': 'no OpenPGP armor header',
    'ожидалась ровно одна подпись': 'exactly one signature was expected',
    'пакет OpenPGP обрезан': 'the OpenPGP packet is truncated',
    'пакет неопределённой длины не поддерживается': 'packets of indeterminate length are not supported',
    'поддерживаются только ключи RSA версии 4': 'only version 4 RSA keys are supported',
    'поддерживаются только подписи RSA': 'only RSA signatures are supported',
    'поддерживаются только подписи версии 4': 'only version 4 signatures are supported',
    'подпакет подписи испорчен': 'a signature subpacket is damaged',
    'подписано другим ключом: {keys}': 'signed by a different key: {keys}',
    'подпись испорчена': 'the signature is damaged',
    'подпись не от этих данных': 'the signature does not belong to this data',
    'подпись не сходится': 'the signature is not valid',
    'подпись обрезана': 'the signature is truncated',
    'срок действия подписи истёк': 'the signature has expired',
    'частичная длина пакета не поддерживается': 'partial packet lengths are not supported',
    'число в пакете OpenPGP обрезано': 'a number in the OpenPGP packet is truncated',
    'это не подпись двоичного файла': 'this is not a binary document signature',
    'TwitchDownloader завершился с кодом {code}': 'TwitchDownloader exited with code {code}',
    'scoop: ссылка не на выпуск TwitchDownloader':
        'scoop: the link does not point to a TwitchDownloader release',
    '{version}: сумма есть только у {sources}': '{version}: only {sources} has a checksum',
    'архив TwitchDownloader повреждён': 'the TwitchDownloader archive is damaged',
    'видео чата не сделано: нет ffmpeg': 'the chat video was not made: ffmpeg is missing',
    'запись удалена или недоступна — чата нет': 'the VOD is deleted or unavailable — there is no chat',
    'запись, из которой вырезан клип, удалена — чата у клипа больше нет':
        'the VOD the clip was cut from is deleted — the clip has no chat anymore',
    'не удалось запустить TwitchDownloader: {error}': 'could not start TwitchDownloader: {error}',
    'скачанный TwitchDownloader не запустился или назвал другую версию':
        'the downloaded TwitchDownloader did not start or reported a different version',
    'скачиваю чат: {name}': 'downloading chat: {name}',
    'установка TwitchDownloader здесь недоступна': 'TwitchDownloader cannot be installed here',
    'чат делается к уже скачанному видео': 'a chat is made for an already downloaded video',
    'чат не скачался: {name} — {error}': 'chat was not downloaded: {name} — {error}',
    'чат отменён': 'chat cancelled',
    'чат скачан: {path}': 'chat downloaded: {path}',
    'чат: {modes}': 'chat: {modes}',
    'GitHub не сообщил контрольную сумму архива': 'GitHub did not report the archive checksum',
    'StreamVault обновлён до {version}': 'StreamVault updated to {version}',
    'архив выпуска повреждён': 'the release archive is damaged',
    'в архиве выпуска лишнее: {name}': 'the release archive contains something extra: {name}',
    'доступна новая версия StreamVault: {version}': 'a new StreamVault version is available: {version}',
    'новая версия ещё не скачана': 'the new version has not been downloaded yet',
    'обновление {version} скачано и проверено — поставится при перезапуске':
        'update {version} is downloaded and verified — it will be installed on restart',
    'обновление до {version} не удалось: {error}': 'the update to {version} failed: {error}',
    'обновление до {version}: программа закроется и запустится заново':
        'updating to {version}: the program will close and start again',
    'обновление программы здесь недоступно': 'the program cannot be updated here',
    'обновления: выпусков на GitHub пока нет': 'updates: there are no releases on GitHub yet',
    'обновления: проверить не удалось — {error}': 'updates: could not check — {error}',
    'проверка обновлений упала': 'the update check crashed',
    'в выпуске {version} нет файла {name}': 'release {version} has no {name}',
    'в чате эфира ни одного сообщения: {name}': 'no messages in the stream chat: {name}',
    'после обработки {name}: сбой': 'after processing {name}: crash',
    'сообщение|сообщения|сообщений': 'message|messages',
    'чат {channel}: не пишется ({error})': 'chat {channel}: cannot be written ({error})',
    'чат {channel}: связь прервалась ({error}), переподключаюсь':
        'chat {channel}: connection lost ({error}), reconnecting',
    'чат эфира готов: {name}': 'stream chat ready: {name}',
    'чат эфира не сохранился: {name} — {error}': 'stream chat was not saved: {name} — {error}',
    'чат эфира не доделан: {name} — {error}': 'stream chat not finished: {name} — {error}',
    'чат эфира: {count} {messages} — {name}': 'stream chat: {count} {messages} — {name}',
    'чат эфира: сбой': 'stream chat: crash',
    'конвертация недоступна: запись эфиров не запущена':
        'conversion is unavailable: stream recording is not running',
    'в mp4 конвертируются только записи .ts': 'only .ts recordings can be converted to mp4',
    '{channel} сейчас не в эфире': '{channel} is not live right now',
    '{channel}: запись начата вручную': '{channel}: recording started manually',
    '{channel}: запись остановлена вручную': '{channel}: recording stopped manually',
    '{name} в эфире': '{name} is live',
    '{tag} в эфире: {title} — не записывается': '{tag} is live: {title} — not recording',
    '{tag}: запись остановлена, эфир продолжается': '{tag}: recording stopped, the stream goes on',
    'запись эфиров не запущена': 'stream recording is not running',
    'нет связи ({error})': 'no connection ({error})',
    'платформа ответила {status}': 'the platform answered {status}',
    'платформа ответила непонятно': 'the platform gave an unexpected answer',
    '{channel}: убран из избранного': '{channel}: removed from favorites',
    'эта трансляция VK Видео уже закончилась': 'this VK Video stream has already ended',
    'в плейлисте записи нет кусков': 'the recording playlist has no segments',
    'запись зашифрована': 'the recording is encrypted',
    'кусок записи не скачался: {error}': 'a recording segment did not download: {error}',
    'остановлено': 'stopped',
    'плейлист записи не скачался: {error}': 'the recording playlist did not download: {error}',
    'это список качеств, а не плейлист записи': 'this is a list of qualities, not the recording playlist',
    'Windows завершает работу: закрываю записи, конвертация — при следующем запуске':
        'Windows is shutting down: closing recordings, conversion will run on the next start',
    '{tag}: жду запись эфира на площадке, чтобы добрать его начало ({length})':
        '{tag}: waiting for the platform to publish the stream to fetch its beginning ({length})',
    '{tag}: запись эфира на площадке не проверить ({error}) — попробую позже':
        '{tag}: could not check the stream recording on the platform ({error}) — will try later',
    'не сохранить, чего ждёт запись с начала эфира: {error}':
        'could not save what the stream-beginning download is waiting for: {error}',
    'у трансляции VK Видео не видно автора': 'the VK Video stream does not show its author',
    'эфира «{author}» нет на VK Video Live — программа записывает эфиры оттуда':
        '"{author}" is not live on VK Video Live — that is where the program records from',
    'это не ссылка на видео VK': 'this is not a VK video link',
    'поиск по {platform} недоступен': 'search on {platform} is not available',
    'ffmpeg сейчас занят конвертацией — повтори, когда она закончится':
        'ffmpeg is busy converting right now — try again when it finishes',
    'yt-dlp {version} сейчас занят — повтори, когда загрузки закончатся':
        'yt-dlp {version} is in use right now — try again when the downloads finish',
    'TwitchDownloader сейчас занят чатом — повтори, когда он закончит':
        'TwitchDownloader is busy with a chat right now — try again when it finishes',
    'эту копию программы не обновить отсюда — скачай выпуск со страницы GitHub':
        'this copy of the program cannot be updated from here — download the release from the GitHub page',
    'чат эфира сохранён в .chat.json; HTML и видео чата появятся, когда будет TwitchDownloader (настройки, раздел «Инструменты»)':
        'stream chat saved to .chat.json; the HTML and chat video will appear once TwitchDownloader is available (settings, Tools section)',
    'конвертация не удалась (попыток: {count}), запись осталась в .ts: {path}':
        'conversion failed (attempts: {count}), the recording stays in .ts: {path}',
    'обработка не удалась (попыток: {count}), файл оставлен в in-progress: {path}':
        'processing failed (attempts: {count}), the file is left in in-progress: {path}',
    '{tag}: эфир закончился': '{tag}: the stream has ended',
    '{tag}: эфир продолжается, пишу часть {part}': '{tag}: the stream continues, recording part {part}',
    '{tag}: эфир продолжается, пробую снова': '{tag}: the stream continues, trying again',
    'Записать можно в окне StreamVault': 'You can record it in the StreamVault window',
    'для чата нужен TwitchDownloader — его можно скачать в настройках, раздел «Инструменты»':
        'chat needs TwitchDownloader — it can be downloaded in the settings, Tools section',
    'чат {channel}: сбой, переподключаюсь': 'chat {channel}: failure, reconnecting',
    'чат закрыт — открой запись заново': 'the chat was closed — open the recording again',
    'чат не читается: {error}': 'cannot read the chat: {error}',
    'смайлы в чат эфира не встроены: {name} — {error}':
        'emotes were not embedded into the live chat: {name} — {error}',
    'у этого видео чата не бывает': 'this video has no chat',
    'у этой площадки чата записей нет': 'this platform has no chat for recordings',
    'чат не сохранился: {error}': 'the chat was not saved: {error}',
    'не понял, какая это запись Kick: {url}': 'cannot tell which Kick recording this is: {url}',
    'Kick не сказал, когда шла эта запись': 'Kick did not say when this recording took place',
    'в чате этого видео нет сообщений': "this video's chat has no messages",
    'чат сохранён в .chat.json; HTML и видео чата сделает TwitchDownloader — его можно скачать в настройках':
        'the chat is saved to .chat.json; TwitchDownloader will make the HTML and chat video — it can be downloaded in the settings',
    'площадка не отдала чат: {error}': 'the platform did not return the chat: {error}',
    'запись не найдена — её удалили или она закрыта':
        'the recording was not found — it was deleted or is private',
    'не понял, какая это запись VK Video Live: {url}':
        'cannot tell which VK Video Live recording this is: {url}',
    'VK Video Live не ответил: {error}': 'VK Video Live did not respond: {error}',
    'канал {name} на VK Video Live не найден': 'channel {name} was not found on VK Video Live',
    'VK Video Live не отдал записи: HTTP {code}': 'VK Video Live did not return the recordings: HTTP {code}',
    'VK Video Live ответил непонятно': 'VK Video Live returned an unexpected answer',
    'VK Video Live не сказал, из какой записи этот клип':
        'VK Video Live did not say which recording this clip is from',
    'у YouTube нет списка клипов канала — вместо них вкладка Shorts':
        'YouTube has no list of channel clips — the Shorts tab is there instead',
    '{tag}: место на диске кончается — останавливаю запись':
        '{tag}: the disk is running out of space — stopping the recording',
    '{value} ГБ': '{value} GB',
    '{value} МБ': '{value} MB',
    'Мало места на диске с записями': 'Low space on the recordings disk',
    'Осталось {free}. Запись остановлена, пока место не освободится.':
        '{free} left. Recording is stopped until space is freed.',
    'Осталось {free}. Освободи место, иначе запись остановится.':
        '{free} left. Free up space, or recording will stop.',
    'запись старше {days} дн. убрана в корзину: {path}':
        'recording older than {days} days moved to the Recycle Bin: {path}',
    'места на диске с записями снова хватает: {free}':
        'there is enough space on the recordings disk again: {free}',
    'на диске осталось {free} — запись остановлена, освободи место':
        '{free} left on the disk — recording stopped, free up space',
    'на диске с записями осталось {free} (порог {floor}) — освободи место, иначе запись остановится':
        '{free} left on the recordings disk (threshold {floor}) — free up space, or recording will stop',
    'на диске с записями осталось {free} — запись останавливается, пока место не освободится':
        '{free} left on the recordings disk — recording stops until space is freed',
    'старая запись не удалена: {path} ({error})':
        'old recording was not removed: {path} ({error})',
    'уборка старых записей не удалась': 'cleaning up old recordings failed',
    'позиция просмотра — число секунд': 'the playback position is a number of seconds',
    '{channel}: всё с вкладки «{kind}» — в очереди {added}, уже было {known}':
        '{channel}: everything from the “{kind}” tab — {added} queued, {known} already there',
    'ffmpeg завершился с кодом {code}': 'ffmpeg exited with code {code}',
    'в работу: {name}': 'queued for editing: {name}',
    'клип — хотя бы секунда: конец позже начала':
        'a clip must be at least a second long: the end after the start',
    'начало и конец клипа — число секунд': 'the clip start and end are numbers of seconds',
    'не получилось: {name} — {error}': 'failed: {name} — {error}',
    'не понял, какого размера видео: {name}': 'could not tell the video size: {name}',
    'не удалось запустить ffmpeg: {error}': 'could not start ffmpeg: {error}',
    'нужен ffmpeg — его можно скачать в настройках':
        'ffmpeg is needed — it can be downloaded in the settings',
    'правка {name} упала': 'editing {name} crashed',
    'правка записей здесь недоступна': 'editing recordings is not available here',
    'правки {id} нет': 'there is no edit {id}',
    'у записи нет видео чата — сделай его кнопкой «чат + видео чата»':
        'the recording has no chat video — make it with the “chat + chat video” option',
    'чат клипа не сохранился: {error}': 'the clip chat was not saved: {error}',
    'чат — справа или слева': 'the chat goes on the right or on the left',
    'канал сейчас не записывается': 'the channel is not being recorded right now',
    'смотреть идущую запись можно с ffmpeg — его можно скачать в настройках':
        'watching a recording in progress needs ffmpeg — it can be downloaded in the settings',
    '{key} — нужно число': '{key} must be a number',
    'деление записи: {modes}': 'splitting recordings: {modes}',
    'конвертирую в mp4 и делю на {count} {pieces}: {name}':
        'converting to mp4 and splitting into {count} {pieces}: {name}',
    'кусок|куска|кусков': 'piece|pieces',
    'чат не поделился по кускам записи: {error}':
        'the chat was not split along with the recording: {error}',
    '{key} задан непонятно: {value}':
        '{key} is set to something unclear: {value}',
    'в записи {name} пропусков: {count}':
        'gaps in recording {name}: {count}',
    'длительность — число секунд':
        'duration is a number of seconds',
    'искать можно в тексте или по нику':
        'you can search in messages or by username',
    'настройки канала: {error}':
        'channel settings: {error}',
    'настройку {key} у канала не задать':
        "{key} can't be set per channel",
    'поиск по чатам здесь недоступен':
        'chat search is not available here',
    'проверка записей — с ffmpeg: его можно скачать в настройках':
        'checking recordings needs ffmpeg: you can download it in the settings',
    'проверка записи: сбой':
        'recording check: failure',
    'Kick ещё не выложил запись этого эфира': "Kick has not published this stream's recording yet",
    'VK ещё не выложил запись этого эфира': "VK has not published this stream's recording yet",
    'YouTube ещё готовит запись эфира': "YouTube is still preparing the stream's recording",
    'YouTube не сохранил запись этого эфира': 'YouTube did not keep a recording of this stream',
    '{tag}: когда начался эфир, не узнать — запись идёт с текущего места':
        '{tag}: cannot tell when the stream started — recording from the current moment',
    '{tag}: начало эфира ({length}) скачается, когда площадка выложит его запись':
        '{tag}: the start of the stream ({length}) will be downloaded once the platform publishes its recording',
    '{tag}: начало эфира ({length}) скачивается — «Загрузки»':
        '{tag}: the start of the stream ({length}) is downloading — see Downloads',
    '{tag}: начало эфира не взять — {reason}': '{tag}: cannot get the start of the stream — {reason}',
    'записи эфира на площадке нет': 'the platform has no recording of the stream',
    'канал задан ссылкой': 'the channel is given as a link',
    'не известен номер трансляции': 'the broadcast id is unknown',
    'не узнать, когда начался эфир': 'cannot tell when the stream started',
    'у канала выключен архив прошедших эфиров': 'the channel has past broadcasts turned off',
    'у этой площадки начала эфира не взять': 'this platform cannot give the start of a stream',
    # консольные команды (cli.py)
    '{tag}: эфир прервался — жду его возвращения {minutes} мин':
        '{tag}: the stream dropped — waiting {minutes} min for it to come back',
    '{tag}: эфир вернулся': '{tag}: the stream is back',
    # склейка начала эфира с записью (live/joining.py)
    'начало эфира не склеено с записью {name}: мало места на диске':
        'the stream beginning was not joined to {name}: not enough disk space',
    'начало эфира не склеено с записью {name}: не прочитать длительность':
        "the stream beginning was not joined to {name}: couldn't read the duration",
    'начало эфира не склеено с записью {name}: размер кадра разный ({head} и {main}) — части остались рядом':
        'the stream beginning was not joined to {name}: different frame sizes ({head} and {main}) — the parts stay side by side',
    'начало эфира не склеено с записью {name}: кодеки разные ({head} и {main}) — части остались рядом':
        'the stream beginning was not joined to {name}: different codecs ({head} and {main}) — the parts stay side by side',
    'начало эфира не склеено с записью {name}: стык не найден — части остались рядом':
        'the stream beginning was not joined to {name}: the seam was not found — the parts stay side by side',
    'начало эфира не склеилось с записью {name}: {error}':
        'joining the stream beginning to {name} failed: {error}',
    'начало эфира не склеилось с записью {name}: длина вышла {got} вместо {want}':
        'joining the stream beginning to {name} failed: the length came out {got} instead of {want}',
    'начало эфира склеено с записью: {name} — стык на {at}, вышло {length}':
        'stream beginning joined to the recording: {name} — seam at {at}, {length} in total',
    'склеиваю начало эфира с записью: {name}': 'joining the stream beginning to the recording: {name}',
    'склейка начала эфира: сбой': 'joining the stream beginning: failure',
    'стык начала и записи не найден (разница {score})':
        'the seam between the beginning and the recording was not found (difference {score})',
    'чат не склеился: {name} — {error}': 'the chat was not joined: {name} — {error}',
    'как делить: off, size, duration или count': 'how to split: off, size, duration or count',
    # живая строка консоли (console.py)
    'чат {count}': 'chat {count}',
    'наблюдаю за каналами: {count}, в эфире: {live}': 'watching channels: {count}, live: {live}',
    'обработка: {name}': 'processing: {name}',
    'ждут обработки: {count}': 'waiting for processing: {count}',
    'в очереди: {count}': 'queued: {count}',
    'StreamVault {version}: запись эфиров и скачивание записей и клипов Twitch, Kick, YouTube, VK Video Live':
        'StreamVault {version}: records live streams and downloads VODs and clips from Twitch, Kick, YouTube and VK Video Live',
    'ФАЙЛ': 'FILE',
    'файл настроек (по умолчанию settings.json рядом с программой)':
        'settings file (settings.json next to the program by default)',
    'КОМАНДА': 'COMMAND',
    'УРОВЕНЬ': 'LEVEL',
    'открыть окно программы (так же без команды)':
        'open the program window (also without a command)',
    'открыть в браузере, а не в отдельном окне':
        'open in the browser instead of a separate window',
    'порт для режима браузера': 'port for the browser mode',
    'не открывать браузер самому, только напечатать адрес':
        "don't open the browser, only print the address",
    'запуститься свёрнутым в трей, без окна': 'start minimized to the tray, without a window',
    'записывать эфиры, пока не нажат Ctrl+C': 'record live streams until Ctrl+C is pressed',
    '[ПЛАТФОРМА:]КАНАЛ': '[PLATFORM:]CHANNEL',
    'например kick:xqc или legendaafk; можно повторять и перечислять через запятую. Без префикса — twitch. С этим ключом список каналов в настройках не меняется':
        "for example kick:xqc or legendaafk; can be repeated or comma-separated. Without a prefix it's twitch. The channel list in the settings isn't changed",
    'КАЧЕСТВО': 'QUALITY',
    'качество записи, например best или 720p': 'recording quality, for example best or 720p',
    'не конвертировать, просто переносить записи': "don't convert, just move the recordings",
    'скачать записи и клипы по ссылкам': 'download VODs and clips by links',
    'ССЫЛКА': 'LINK',
    'best, worst, audio, высота кадра (1080, 720...) или формат из команды info':
        'best, worst, audio, frame height (1080, 720...) or a format from the info command',
    'НАЧАЛО-КОНЕЦ': 'START-END',
    'скачать только отрезок, например 1:00:00-1:30:00':
        'download only a section, for example 1:00:00-1:30:00',
    'скачать заново, даже если уже скачано': 'download again even if already downloaded',
    'чат записей и клипов Twitch через TwitchDownloader: none, text (HTML и JSON) или render (ещё и видео чата); по умолчанию — как в настройках':
        'chat of Twitch VODs and clips via TwitchDownloader: none, text (HTML and JSON) or render (also a chat video); by default as in the settings',
    'что лежит по ссылке или какие записи есть у канала':
        'what is at a link, or which VODs a channel has',
    'ССЫЛКА|ПЛАТФОРМА:КАНАЛ': 'LINK|PLATFORM:CHANNEL',
    'vods, clips, highlights, uploads (Twitch), videos, shorts (YouTube); по умолчанию vods':
        'vods, clips, highlights, uploads (Twitch), videos, shorts (YouTube); vods by default',
    'за какой срок клипы Twitch: 24hr, 7d, 30d, all':
        'period for Twitch clips: 24hr, 7d, 30d, all',
    'сколько записей показать': 'how many entries to show',
    'показать поддерживаемые платформы': 'show the supported platforms',
    'перенести config.py старого рекордера в настройки':
        'import config.py of the old recorder into the settings',
    'перезаписать уже существующий файл настроек': 'overwrite an existing settings file',
    'опрос раз в {seconds} с': 'checked every {seconds} s',
    'ошибка: StreamVault уже запущен — окном или другой консольной командой. Закрой его или добавь каналы и ссылки в нём':
        'error: StreamVault is already running — as a window or another console command. Close it or add the channels and links there',
    'ошибка: {error}': 'error: {error}',
    'неизвестный уровень логирования: {level}': 'unknown log level: {level}',
    '{path} уже есть. Чтобы перезаписать, добавь --force':
        '{path} already exists. Add --force to overwrite it',
    'не удалось прочитать {path}: {error}': "couldn't read {path}: {error}",
    'настройки сохранены в {path}': 'settings saved to {path}',
    'каналов: {count}{names}': 'channels: {count}{names}',
    'ключи Twitch перенесены': 'Twitch keys imported',
    'не задан ни один канал: добавь каналы в {path} (channels) или передай -u':
        'no channels: add them to {path} (channels) or pass -u',
    'повторный Ctrl+C — выхожу, не дожидаясь обработки. Необработанные записи подхватятся при следующем запуске':
        'Ctrl+C again — exiting without waiting for processing. Unprocessed recordings will be picked up on the next start',
    'осталось необработанных записей: {count}. Они подхватятся при следующем запуске':
        'unprocessed recordings left: {count}. They will be picked up on the next start',
    'Б': 'B',
    'КБ': 'KB',
    'МБ': 'MB',
    'ГБ': 'GB',
    'чат': 'chat',
    '{done} из {total}': '{done} of {total}',
    '{size}/с': '{size}/s',
    'осталось {time}': '{time} left',
    'склейка': 'merging',
    'начинаю': 'starting',
    'уже скачано: {path} (заново — с --force)':
        'already downloaded: {path} (again with --force)',
    'остановлено. Скачанное сохранено: та же команда продолжит загрузку с того же места':
        'stopped. What was downloaded is kept: the same command resumes from the same place',
    'скачано {done} из {total}': 'downloaded {done} of {total}',
    'канал': 'channel',
    'дата': 'date',
    'длительность': 'duration',
    'качество': 'quality',
    'пусто': 'empty',
}
