"""Testes para a detecção de processos com nome de sistema rodando de
local suspeito (backend/security_check.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.security_check import is_suspicious_path


class SecurityCheckTests(unittest.TestCase):
    def test_svchost_in_system32_is_not_suspicious(self):
        self.assertFalse(is_suspicious_path('svchost.exe', r'C:\Windows\System32\svchost.exe'))

    def test_svchost_in_syswow64_is_not_suspicious(self):
        self.assertFalse(is_suspicious_path('svchost.exe', r'C:\Windows\SysWOW64\svchost.exe'))

    def test_svchost_in_temp_is_suspicious(self):
        self.assertTrue(is_suspicious_path(
            'svchost.exe', r'C:\Users\alguem\AppData\Local\Temp\svchost.exe'
        ))

    def test_explorer_directly_in_windir_is_not_suspicious(self):
        # explorer.exe é a exceção: sempre roda de C:\Windows, não de System32
        self.assertFalse(is_suspicious_path('explorer.exe', r'C:\Windows\explorer.exe'))

    def test_explorer_in_system32_is_suspicious(self):
        # mesmo System32 seria errado pra explorer.exe - o esperado é
        # exatamente C:\Windows
        self.assertTrue(is_suspicious_path('explorer.exe', r'C:\Windows\System32\explorer.exe'))

    def test_explorer_in_subdirectory_of_windir_is_suspicious(self):
        # prefixo "C:\Windows\" sozinho não basta - tem que ser o diretório exato
        self.assertTrue(is_suspicious_path('explorer.exe', r'C:\Windows\Temp\explorer.exe'))

    def test_case_insensitive_path_comparison(self):
        self.assertFalse(is_suspicious_path('SVCHOST.EXE', r'c:\windows\system32\SVCHOST.EXE'))

    def test_name_outside_watchlist_is_never_suspicious(self):
        self.assertFalse(is_suspicious_path('notepad.exe', r'C:\Users\alguem\Downloads\notepad.exe'))

    def test_empty_exe_path_is_not_suspicious(self):
        # processos sem caminho legível (ex.: "System", PID 4) não têm como
        # ser avaliados - não devem gerar falso-positivo
        self.assertFalse(is_suspicious_path('svchost.exe', ''))

    def test_empty_name_is_not_suspicious(self):
        self.assertFalse(is_suspicious_path('', r'C:\Windows\System32\svchost.exe'))


if __name__ == "__main__":
    unittest.main()
