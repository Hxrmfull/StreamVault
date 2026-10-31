# -*- coding: utf-8 -*-
"""
Точка входа exe. Из неё собираются оба файла программы:

  StreamVault.exe     — окно, без консоли; с --tray стартует в трее;
  StreamVaultCLI.exe  — консольные команды (record, download, info…)
                        и скрытые помощники, которыми окно запускает
                        встроенные streamlink и yt-dlp, а новая версия
                        ставит себя на место старой (streamvault/update.py).
"""
import os
import sys
import traceback

from streamvault import util


def honor_ioencoding():
    """
    Собранный Python не читает PYTHONIOENCODING, а окно задаёт его streamlink
    и yt-dlp (util.child_environment) и читает их вывод как utf-8. Без этого
    помощники писали бы в cp1251, и кириллица в названиях превращалась в мусор.
    """
    value = os.environ.get("PYTHONIOENCODING")
    if not value:
        return
    encoding, _, errors = value.partition(":")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding=encoding or None, errors=errors or None)
        except (AttributeError, LookupError, ValueError):
            pass


def main(args):
    honor_ioencoding()
    if args[:1] == [util.RUN_STREAMLINK]:
        from streamlink_cli.main import main as streamlink
        sys.argv = ["streamlink", *args[1:]]
        return streamlink()
    if args[:1] == [util.APPLY_UPDATE]:
        from streamvault.update import apply_update
        return apply_update(args[1:])
    if args[:1] == [util.RUN_YTDLP]:
        # обновлённый yt-dlp из архива — раньше встроенного в сборку
        copy = os.environ.get(util.YTDLP_COPY_ENV)
        if copy:
            sys.path.insert(0, copy)
        import yt_dlp
        return yt_dlp.main(args[1:])
    # какой это из двух exe, видно по имени: по наличию консоли не понять —
    # у запущенного другой программой консольного exe её окна может не быть
    if os.path.basename(sys.executable).lower() == util.CONSOLE_EXE.lower():
        from streamvault.cli import main as cli
        return cli(args)
    try:
        from streamvault.ui.app import run
        return run(start_hidden="--tray" in args)
    except Exception:
        # без консоли трассировку иначе никто бы не увидел
        util.notify("StreamVault не запустился:\n\n" + traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
