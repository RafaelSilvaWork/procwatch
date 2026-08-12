"""Testes para a detecção de processos com janela visível
(backend/window_utils.py). win32gui/win32process são mockados - o que
importa aqui é a lógica de filtragem, não a API do Windows em si."""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.window_utils import get_pids_with_visible_window


def _enum_windows_with(hwnds):
    """Fábrica de um substituto para win32gui.EnumWindows que chama o
    callback uma vez pra cada hwnd da lista, como a API real faz."""
    def _fake_enum_windows(callback, extra):
        for hwnd in hwnds:
            callback(hwnd, extra)
    return _fake_enum_windows


class GetPidsWithVisibleWindowTests(unittest.TestCase):
    def test_visible_window_with_title_is_included(self):
        with patch("backend.window_utils.win32gui.EnumWindows", _enum_windows_with([1])), \
             patch("backend.window_utils.win32gui.IsWindowVisible", return_value=True), \
             patch("backend.window_utils.win32gui.GetWindowText", return_value="Bloco de Notas"), \
             patch("backend.window_utils.win32process.GetWindowThreadProcessId", return_value=(0, 4321)):
            self.assertEqual(get_pids_with_visible_window(), {4321})

    def test_visible_window_without_title_is_excluded(self):
        # Muitas janelas auxiliares do sistema são visíveis mas não têm
        # título - não são "aplicativos" do ponto de vista do usuário.
        with patch("backend.window_utils.win32gui.EnumWindows", _enum_windows_with([1])), \
             patch("backend.window_utils.win32gui.IsWindowVisible", return_value=True), \
             patch("backend.window_utils.win32gui.GetWindowText", return_value=""), \
             patch("backend.window_utils.win32process.GetWindowThreadProcessId", return_value=(0, 4321)):
            self.assertEqual(get_pids_with_visible_window(), set())

    def test_hidden_window_with_title_is_excluded(self):
        with patch("backend.window_utils.win32gui.EnumWindows", _enum_windows_with([1])), \
             patch("backend.window_utils.win32gui.IsWindowVisible", return_value=False), \
             patch("backend.window_utils.win32gui.GetWindowText", return_value="Janela escondida"), \
             patch("backend.window_utils.win32process.GetWindowThreadProcessId", return_value=(0, 4321)):
            self.assertEqual(get_pids_with_visible_window(), set())

    def test_multiple_windows_of_same_process_dedupe_to_one_pid(self):
        with patch("backend.window_utils.win32gui.EnumWindows", _enum_windows_with([1, 2])), \
             patch("backend.window_utils.win32gui.IsWindowVisible", return_value=True), \
             patch("backend.window_utils.win32gui.GetWindowText", return_value="Aba"), \
             patch("backend.window_utils.win32process.GetWindowThreadProcessId", return_value=(0, 999)):
            self.assertEqual(get_pids_with_visible_window(), {999})

    def test_error_reading_one_window_does_not_abort_the_whole_scan(self):
        """Uma janela problemática (ex.: destruída entre EnumWindows e a
        leitura de suas propriedades) não pode derrubar a detecção das
        demais - só essa janela é ignorada."""
        def _flaky_get_window_text(hwnd):
            if hwnd == 1:
                raise Exception("janela já não existe mais")
            return "OK"

        with patch("backend.window_utils.win32gui.EnumWindows", _enum_windows_with([1, 2])), \
             patch("backend.window_utils.win32gui.IsWindowVisible", return_value=True), \
             patch("backend.window_utils.win32gui.GetWindowText", side_effect=_flaky_get_window_text), \
             patch("backend.window_utils.win32process.GetWindowThreadProcessId", return_value=(0, 777)):
            self.assertEqual(get_pids_with_visible_window(), {777})

    def test_enum_windows_failure_returns_empty_set_without_raising(self):
        with patch("backend.window_utils.win32gui.EnumWindows", side_effect=Exception("acesso negado")):
            self.assertEqual(get_pids_with_visible_window(), set())


if __name__ == "__main__":
    unittest.main()
