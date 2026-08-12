"""Persistência de preferências do usuário (thresholds de alerta, palavras-
chave customizadas, geometria da janela) entre execuções, usando QSettings
em formato .ini local."""

import os
from typing import Dict, List, Optional, Tuple

from PyQt6.QtCore import QByteArray, QSettings

_SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "procwatch.ini"
)

_THRESHOLD_KEYS = (
    'cpu_warning', 'cpu_critical',
    'memory_warning', 'memory_critical',
    'sustained_high_cpu_secs',
    'memory_leak_growth_mb',
    'memory_leak_min_samples',
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


def _normalize_string_list(value) -> List[str]:
    """QSettings (formato .ini) devolve uma lista com 1 item como string
    pura em vez de lista - normaliza os dois casos."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    return list(value)


def save_custom_keywords(critical: List[str], warning: List[str]) -> None:
    settings = _settings()
    settings.beginGroup("keywords")
    settings.setValue("critical", critical)
    settings.setValue("warning", warning)
    settings.endGroup()


def load_custom_keywords() -> Tuple[List[str], List[str]]:
    settings = _settings()
    settings.beginGroup("keywords")
    critical = _normalize_string_list(settings.value("critical"))
    warning = _normalize_string_list(settings.value("warning"))
    settings.endGroup()
    return critical, warning


def save_window_geometry(geometry: QByteArray) -> None:
    _settings().setValue("window/geometry", geometry)


def load_window_geometry() -> Optional[QByteArray]:
    return _settings().value("window/geometry")


def save_tray_notice_dismissed(dismissed: bool) -> None:
    """Se o usuário marcou "não mostrar novamente" no aviso explicando que
    fechar a janela minimiza para a bandeja em vez de encerrar."""
    _settings().setValue("ui/tray_notice_dismissed", dismissed)


def load_tray_notice_dismissed() -> bool:
    value = _settings().value("ui/tray_notice_dismissed", False)
    # QSettings (.ini) pode devolver "true"/"false" como string
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)
