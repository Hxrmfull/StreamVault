# -*- coding: utf-8 -*-
# Двойной щелчок открывает окно StreamVault без консоли.
# С ключом --tray программа стартует свёрнутой в трей: так её запускает Windows.
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from streamvault import util  # noqa: E402

try:
    from streamvault.ui.app import run
    code = run(start_hidden="--tray" in sys.argv[1:])
except Exception:
    # без консоли трассировку иначе никто бы не увидел
    util.notify("StreamVault не запустился:\n\n" + traceback.format_exc()[-1500:])
    code = 1
raise SystemExit(code)
