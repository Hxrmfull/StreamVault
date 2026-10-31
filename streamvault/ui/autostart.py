# -*- coding: utf-8 -*-
"""
Запуск вместе с Windows: строка в разделе Run реестра текущего пользователя.

Права администратора не нужны, другие пользователи компьютера не затронуты.
Выключается тем же переключателем или в диспетчере задач на вкладке
«Автозагрузка».
"""

import os
import sys

from ..i18n import tr

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "StreamVault"
APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LAUNCHER = os.path.join(APP_DIR, "StreamVault.pyw")


def _winreg():
    import winreg
    return winreg


def command():
    """pythonw — чтобы при входе в Windows не мелькала консоль; --tray — сразу в трей."""
    if getattr(sys, "frozen", False):
        # exe окна и так без консоли
        return '"%s" --tray' % os.path.abspath(sys.executable)
    folder = os.path.dirname(sys.executable)
    pythonw = os.path.join(folder, "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return '"%s" "%s" --tray' % (pythonw, LAUNCHER)


def is_enabled(winreg=None):
    if os.name != "nt" and winreg is None:
        return False
    winreg = winreg or _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled, winreg=None):
    if os.name != "nt" and winreg is None:
        raise OSError(tr("запуск вместе с системой настраивается только в Windows"))
    winreg = winreg or _winreg()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
