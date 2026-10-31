# -*- mode: python ; coding: utf-8 -*-
# Сборка PyInstaller: папка dist\StreamVault с двумя exe на общих библиотеках.
# Запускается из packaging\build.py — он же готовит значок и сведения о версии.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = os.path.dirname(SPECPATH)
BUILD = os.path.join(ROOT, "build")

# streamlink подгружает плагины по имени, а не import'ом — PyInstaller их
# не видит. _plugins.json и метаданные пакета нужны для быстрой загрузки:
# streamlink сверяет файл с суммой из RECORD.
hiddenimports = collect_submodules("streamlink") + collect_submodules("streamlink_cli")


def plugin_files():
    """
    Свои расширения yt-dlp — файлами: yt-dlp ищет их в папке (--plugin-dirs).
    Без __pycache__: в кэше записан путь к исходникам на машине сборки.
    """
    base = os.path.join(ROOT, "streamvault", "ytdlp_plugins")
    found = []
    for folder, dirs, names in os.walk(base):
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        target = os.path.normpath(os.path.join("streamvault", "ytdlp_plugins", os.path.relpath(folder, base)))
        found += [(os.path.join(folder, name), target) for name in names if not name.endswith(".pyc")]
    return found


datas = [(os.path.join(ROOT, "streamvault", "ui", "web"), os.path.join("streamvault", "ui", "web"))]
datas += plugin_files()
datas += collect_data_files("streamlink") + copy_metadata("streamlink") + copy_metadata("yt-dlp")

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pydoc_data", "test"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

common = dict(exclude_binaries=True, debug=False, strip=False, upx=False,
              icon=os.path.join(BUILD, "StreamVault.ico"),
              version=os.path.join(BUILD, "version.txt"))
window = EXE(pyz, a.scripts, [], name="StreamVault", console=False, **common)
console = EXE(pyz, a.scripts, [], name="StreamVaultCLI", console=True, **common)

coll = COLLECT(window, console, a.binaries, a.datas, strip=False, upx=False, name="StreamVault")
