# -*- coding: utf-8 -*-
"""
Сборка exe: python packaging\\build.py [--no-update]

Собирается в отдельном виртуальном окружении build\\venv, куда ставятся
только зависимости программы: в общем Python могут лежать PyQt, numpy
и прочее, что PyInstaller утащил бы в сборку. Окружение создаётся при
первой сборке и обновляется, когда меняются списки зависимостей.

Результат — папка dist\\StreamVault: StreamVault.exe (окно),
StreamVaultCLI.exe (консоль) и _internal с библиотеками, и она же в архиве
dist\\StreamVault-<версия>-windows.zip. ffmpeg в сборку не входит: программа
найдёт установленный или скачает его кнопкой.

Если программой уже пользуются прямо из dist\\StreamVault, сборка обновляет
там только её файлы: настройки, очередь загрузок, журналы и скачанные
кнопками инструменты (tools) остаются. Запущенную программу не обновить —
сборка попросит её закрыть.

--no-update — не обновлять yt-dlp и streamlink (например, без интернета).
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.join(ROOT, "packaging")
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
TARGET = os.path.join(DIST, "StreamVault")
# PyInstaller стирает свою папку целиком — поэтому собирает в сторонке
STAGE = os.path.join(BUILD, "dist")
VENV = os.path.join(BUILD, "venv")
VENV_PYTHON = os.path.join(VENV, "Scripts", "python.exe")
REQUIREMENTS = [os.path.join(ROOT, "requirements.txt"), os.path.join(HERE, "requirements-build.txt")]
STAMP = os.path.join(VENV, "requirements.sha256")

VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('ProductName', 'StreamVault'),
      StringStruct('FileDescription', 'StreamVault'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('InternalName', 'StreamVault'),
      StringStruct('OriginalFilename', 'StreamVault.exe')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def requirements_hash():
    digest = hashlib.sha256()
    for path in REQUIREMENTS:
        with open(path, "rb") as source:
            digest.update(source.read())
    return digest.hexdigest()


def prepare_venv(update=True):
    """
    Создаёт или обновляет build\\venv. yt-dlp и streamlink подтягиваются
    до свежих при каждой сборке: сайты меняются, и старая версия перестаёт
    их понимать. Остальное обновляется, только когда меняются списки.
    """
    if not os.path.exists(VENV_PYTHON):
        print("создаю окружение для сборки:", VENV, flush=True)
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
    wanted = requirements_hash()
    try:
        with open(STAMP, encoding="ascii") as source:
            current = source.read().strip() == wanted
    except OSError:
        current = False
    if not current:
        print("ставлю зависимости в окружение сборки", flush=True)
        subprocess.run([VENV_PYTHON, "-m", "pip", "install", "--upgrade", "pip"], check=True)
        command = [VENV_PYTHON, "-m", "pip", "install"]
        for path in REQUIREMENTS:
            command += ["-r", path]
        subprocess.run(command, check=True)
        with open(STAMP, "w", encoding="ascii") as target:
            target.write(wanted)
    if update:
        print("обновляю yt-dlp и streamlink", flush=True)
        subprocess.run([VENV_PYTHON, "-m", "pip", "install", "--upgrade", "--quiet",
                        "yt-dlp[default]", "streamlink"], check=True)


def make_zip():
    """Архив для раздачи: dist\\StreamVault-<версия>-windows.zip с чистой папкой StreamVault."""
    from streamvault import __version__
    os.makedirs(DIST, exist_ok=True)
    base = os.path.join(DIST, "StreamVault-%s-windows" % __version__)
    if os.path.exists(base + ".zip"):
        os.remove(base + ".zip")
    return shutil.make_archive(base, "zip", root_dir=STAGE, base_dir="StreamVault")


def make_icon():
    from streamvault.ui.tray import draw_icon
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    # каждый размер рисуется отдельно: мелкие значки чёткие, а не уменьшенные
    images = [draw_icon(size=size) for size in sizes]
    images[-1].save(os.path.join(BUILD, "StreamVault.ico"), format="ICO",
                    sizes=[(size, size) for size in sizes], append_images=images[:-1])


def make_version():
    from streamvault import __version__
    parts = [int(part) for part in __version__.split(".")][:4]
    numbers = tuple(parts + [0] * (4 - len(parts)))
    with open(os.path.join(BUILD, "version.txt"), "w", encoding="utf-8") as target:
        target.write(VERSION_TEMPLATE.format(numbers=numbers, version=__version__))


def build():
    """Уже внутри build\\venv."""
    sys.path.insert(0, ROOT)
    from streamvault.update import install_program
    os.makedirs(BUILD, exist_ok=True)
    make_icon()
    make_version()
    import PyInstaller.__main__
    PyInstaller.__main__.run([
        os.path.join(HERE, "StreamVault.spec"), "--noconfirm", "--clean",
        "--distpath", STAGE, "--workpath", os.path.join(BUILD, "pyinstaller"),
    ])
    print("архив:", make_zip(), flush=True)
    try:
        install_program(os.path.join(STAGE, "StreamVault"), TARGET)
    except OSError as error:
        print("Не удалось обновить %s (%s): закройте StreamVault и запустите сборку снова."
              % (TARGET, error), file=sys.stderr)
        return 1
    print("готово:", TARGET, flush=True)
    return 0


def main():
    if os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(VENV):
        return build()
    sys.path.insert(0, ROOT)
    from streamvault.update import program_in_use
    if program_in_use(TARGET):
        # проверка заранее: незачем собирать минуту, чтобы потом не суметь обновить
        print("StreamVault запущен из %s — закройте его и запустите сборку снова." % TARGET,
              file=sys.stderr)
        return 1
    prepare_venv(update="--no-update" not in sys.argv[1:])
    return subprocess.run([VENV_PYTHON, os.path.abspath(__file__)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
