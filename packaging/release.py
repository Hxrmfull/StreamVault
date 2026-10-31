# -*- coding: utf-8 -*-
"""
Выпуск новой версии: python packaging\\release.py 0.2.0 [--notes файл] [--dry-run]

1. версия в streamvault/__init__.py обязана совпасть с указанной;
2. сборка (packaging/build.py) — архив dist\\StreamVault-<версия>-windows.zip;
3. установщик (Inno Setup 6, packaging/StreamVault.iss) —
   dist\\StreamVault-<версия>-setup.exe;
4. выпуск на GitHub (gh release create v<версия>) с архивом и установщиком.

Программа берёт обновление из последнего выпуска и сверяет файл с суммой
sha256, которую GitHub посчитал при выкладке: переносная копия — архив,
установленная — установщик. --dry-run — только собрать, --skip-build —
опубликовать уже собранное (установщик собирается заново из build\\dist).
"""
import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamvault import __version__  # noqa: E402
from streamvault.update import ASSET, REPO, SETUP_ASSET  # noqa: E402

ISS = os.path.join(ROOT, "packaging", "StreamVault.iss")


def find_iscc():
    """Компилятор Inno Setup: из PATH или из обычных мест установки."""
    found = shutil.which("ISCC")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", "") + r"\Programs", os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("ProgramFiles", "")):
        path = os.path.join(base, "Inno Setup 6", "ISCC.exe")
        if base and os.path.isfile(path):
            return path
    return None


def main():
    parser = argparse.ArgumentParser(description="Выпуск новой версии StreamVault")
    parser.add_argument("version", help="версия выпуска, как в streamvault/__init__.py")
    parser.add_argument("--notes", help="что нового (файл Markdown); без него — список изменений GitHub")
    parser.add_argument("--dry-run", action="store_true", help="собрать, не публикуя")
    parser.add_argument("--skip-build", action="store_true", help="взять уже собранную программу")
    args = parser.parse_args()

    if args.version != __version__:
        sys.exit("в streamvault/__init__.py версия %s, а не %s" % (__version__, args.version))
    iscc = find_iscc()
    if not iscc:
        sys.exit("нет Inno Setup 6 (ISCC.exe): https://jrsoftware.org/isdl.php")
    if not args.skip_build:
        if subprocess.run([sys.executable, os.path.join(ROOT, "packaging", "build.py")]).returncode:
            sys.exit("сборка не удалась")
    if subprocess.run([iscc, "/Q", "/DAppVersion=" + args.version, ISS]).returncode:
        sys.exit("установщик не собрался")
    files = [os.path.join(ROOT, "dist", name.format(version=args.version)) for name in (SETUP_ASSET, ASSET)]
    for path in files:
        if not os.path.isfile(path):
            sys.exit("нет файла: %s" % path)
        print("%s: %d байт" % (os.path.basename(path), os.path.getsize(path)))

    if args.dry_run:
        print("без публикации:", ", ".join(files))
        return 0
    command = ["gh", "release", "create", "v" + args.version, *files,
               "--repo", REPO, "--title", "StreamVault " + args.version]
    command += ["--notes-file", args.notes] if args.notes else ["--generate-notes"]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
