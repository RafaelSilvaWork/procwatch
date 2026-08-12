"""Persistência de preferências do usuário (thresholds de alerta, geometria
da janela) entre execuções, usando QSettings em formato .ini local."""

import os
from typing import Dict, Optional

from PyQt6.QtCore import QByteArray, QSettings

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logwatch.ini"
)

_THRESHOLD_KEYS = (
    'cpu_warning', 'cpu_critical',
    'memory_warning', 'memory_critical',
    'sustained_high_cpu_secs',
)


def _settings() -> QSettings:
    return QSettings(_SETTINGS_PATH, QSettings.Format.IniFormat)


def save_thresholds(thresholds: Dict[str, float]) -> None:
    settings = _settings()
    settings.beginGroup("alerts")
    for key in _THRESHOLD_KEYS:
        if key in thresholds:
            settings.setValue(key, thresholds[key])
    settings.endGroup()


def load_thresholds() -> Dict[str, float]:
    settings = _settings()
    settings.beginGroup("alerts")
    result = {}
    for key in _THRESHOLD_KEYS:
        if key in settings.childKeys():
            try:
                result[key] = float(settings.value(key))
            except (TypeError, ValueError):
                pass
    settings.endGroup()
    return result


def save_window_geometry(geometry: QByteArray) -> None:
    _settings().setValue("window/geometry", geometry)


def load_window_geometry() -> Optional[QByteArray]:
    return _settings().value("window/geometry")
