# -*- coding: utf-8 -*-
"""
Каждый модуль импортируется первым, сам по себе. Циклы импорта
(library → archive → live → library) прятались, пока тесты случайно
грузили модули в удачном порядке, и падали при другом.
"""
import os
import pkgutil
import subprocess
import sys
import unittest

import streamvault

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# плагин yt-dlp грузит сам yt-dlp, а __main__ сразу запускает программу
SKIP = ("streamvault.ytdlp_plugins", "streamvault.__main__")


def modules():
    for info in pkgutil.walk_packages(streamvault.__path__, "streamvault."):
        if not info.name.startswith(SKIP):
            yield info.name


class ImportTest(unittest.TestCase):

    def test_every_module_imports_first(self):
        names = list(modules())
        self.assertIn("streamvault.sites", names)
        # один процесс на модуль: в общем процессе уже загруженное прячет цикл
        script = ("import importlib, sys\n"
                  "failed = []\n"
                  "for name in sys.argv[1:]:\n"
                  "    for loaded in [m for m in sys.modules if m.startswith('streamvault')]:\n"
                  "        del sys.modules[loaded]\n"
                  "    try:\n"
                  "        importlib.import_module(name)\n"
                  "    except Exception as error:\n"
                  "        failed.append('%s: %r' % (name, error))\n"
                  "print('\\n'.join(failed))\n")
        result = subprocess.run([sys.executable, "-c", script, *names], cwd=ROOT,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
