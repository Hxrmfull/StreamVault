# -*- coding: utf-8 -*-
"""
Выпуск новой версии: python packaging\\release.py 0.2.0 [--notes файл] [--dry-run]

1. версия в streamvault/__init__.py обязана совпасть с указанной;
2. сборка (packaging/build.py) — архив dist\\StreamVault-<версия>-windows.zip;
3. выпуск на GitHub (gh release create v<версия>) с этим архивом.

Программа берёт обновление из последнего выпуска и сверяет архив с суммой
sha256, которую GitHub посчитал при выкладке. --dry-run — только собрать,
--skip-build — опубликовать уже собранный архив.
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from streamvault import __version__  # noqa: E402
from streamvault.update import ASSET, REPO  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Выпуск новой версии StreamVault")
    parser.add_argument("version", help="версия выпуска, как в streamvault/__init__.py")
    parser.add_argument("--notes", help="что нового (файл Markdown); без него — список изменений GitHub")
    parser.add_argument("--dry-run", action="store_true", help="собрать, не публикуя")
    parser.add_argument("--skip-build", action="store_true", help="взять уже собранный архив")
    args = parser.parse_args()

    if args.version != __version__:
        sys.exit("в streamvault/__init__.py версия %s, а не %s" % (__version__, args.version))
    if not args.skip_build:
        if subprocess.run([sys.executable, os.path.join(ROOT, "packaging", "build.py")]).returncode:
            sys.exit("сборка не удалась")
    archive = os.path.join(ROOT, "dist", ASSET.format(version=args.version))
    if not os.path.isfile(archive):
        sys.exit("нет архива: %s" % archive)
    print("%s: %d байт" % (os.path.basename(archive), os.path.getsize(archive)))

    if args.dry_run:
        print("без публикации: архив —", archive)
        return 0
    command = ["gh", "release", "create", "v" + args.version, archive,
               "--repo", REPO, "--title", "StreamVault " + args.version]
    command += ["--notes-file", args.notes] if args.notes else ["--generate-notes"]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
