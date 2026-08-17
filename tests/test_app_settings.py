"""Testes para desktop/app_settings.py - persistência de thresholds,
palavras-chave e geometria da janela entre execuções. Único módulo da
camada desktop/ que dá pra testar sem subir a interface gráfica (QSettings
funciona sem QApplication), e onde uma regressão silenciosa faria o
usuário perder configuração salva sem perceber."""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtCore import QByteArray

from desktop.app_settings import (
    load_custom_keywords, load_thresholds, load_tray_notice_dismissed, load_window_geometry,
    save_custom_keywords, save_thresholds, save_tray_notice_dismissed, save_window_geometry,
)


class AppSettingsTestCase(unittest.TestCase):
    """Cada teste usa seu próprio arquivo .ini temporário - nunca o
    procwatch.ini real do projeto, e nenhum teste vaza estado pro outro."""

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".ini")
        os.close(fd)
        os.remove(path)  # QSettings deve criar do zero, não achar um arquivo vazio
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        patcher = patch("desktop.app_settings._SETTINGS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)


class ThresholdsTests(AppSettingsTestCase):
    def test_round_trips_known_keys(self):
        save_thresholds({"cpu_warning": 80.0, "cpu_critical": 95.0})
        self.assertEqual(load_thresholds(), {"cpu_warning": 80.0, "cpu_critical": 95.0})

    def test_ignores_keys_outside_the_known_set(self):
        save_thresholds({"cpu_warning": 80.0, "campo_desconhecido": 123})
        self.assertEqual(load_thresholds(), {"cpu_warning": 80.0})

    def test_returns_empty_dict_when_nothing_saved_yet(self):
        self.assertEqual(load_thresholds(), {})

    def test_corrupted_value_in_ini_is_skipped_not_crashed(self):
        # Simula um .ini editado a mão (ou corrompido) com um valor que não
        # dá pra converter pra float - load_thresholds não pode quebrar por
        # causa de UM valor ruim, só ignorar aquela chave.
        save_thresholds({"cpu_warning": 80.0, "cpu_critical": 95.0})
        with patch("desktop.app_settings.QSettings.value", side_effect=lambda key: "não-é-numero"):
            resultado = load_thresholds()
        self.assertEqual(resultado, {})


class CustomKeywordsTests(AppSettingsTestCase):
    def test_round_trips_multiple_keywords(self):
        save_custom_keywords(["fatal", "crash"], ["retry", "timeout"])
        self.assertEqual(load_custom_keywords(), (["fatal", "crash"], ["retry", "timeout"]))

    def test_single_keyword_is_not_flattened_into_characters(self):
        # QSettings (.ini) devolve uma lista de 1 item como string pura em
        # vez de lista de 1 elemento - sem a normalização, load_custom_keywords
        # devolveria ['f','a','t','a','l'] em vez de ['fatal'].
        save_custom_keywords(["fatal"], [])
        self.assertEqual(load_custom_keywords(), (["fatal"], []))

    def test_returns_empty_lists_when_nothing_saved_yet(self):
        self.assertEqual(load_custom_keywords(), ([], []))


class WindowGeometryTests(AppSettingsTestCase):
    def test_round_trips_geometry_bytes(self):
        geometry = QByteArray(b"\x01\x02\x03\x04")
        save_window_geometry(geometry)
        self.assertEqual(bytes(load_window_geometry()), bytes(geometry))

    def test_returns_none_when_nothing_saved_yet(self):
        self.assertIsNone(load_window_geometry())


class TrayNoticeDismissedTests(AppSettingsTestCase):
    def test_defaults_to_false_when_nothing_saved_yet(self):
        self.assertFalse(load_tray_notice_dismissed())

    def test_round_trips_true(self):
        save_tray_notice_dismissed(True)
        self.assertTrue(load_tray_notice_dismissed())

    def test_round_trips_false_explicitly(self):
        save_tray_notice_dismissed(True)
        save_tray_notice_dismissed(False)
        self.assertFalse(load_tray_notice_dismissed())

    def test_string_true_from_ini_format_is_treated_as_true(self):
        # QSettings em formato .ini as vezes devolve "true"/"false" como
        # string em vez de bool nativo - load_tray_notice_dismissed precisa
        # tratar os dois casos.
        with patch("desktop.app_settings.QSettings.value", return_value="true"):
            self.assertTrue(load_tray_notice_dismissed())

    def test_string_false_from_ini_format_is_treated_as_false(self):
        with patch("desktop.app_settings.QSettings.value", return_value="false"):
            self.assertFalse(load_tray_notice_dismissed())


if __name__ == "__main__":
    unittest.main()
