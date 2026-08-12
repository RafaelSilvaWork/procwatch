"""Testes para a leitura do Visualizador de Eventos do Windows
(backend/os_logs.py). win32evtlog/win32evtlogutil são mockados - o que
importa aqui é o mapeamento de tipo de evento, o limite de registros e a
tolerância a falhas (evento binário, log inacessível), não a API do
Windows em si."""

import os
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import win32evtlog

from backend.os_logs import buscar_logs_sistema


def _fake_event(event_type, source="Origem", generated="2026-01-01 00:00:00"):
    return types.SimpleNamespace(EventType=event_type, SourceName=source, TimeGenerated=generated)


class BuscarLogsSistemaTests(unittest.TestCase):
    def test_maps_error_and_warning_types_and_defaults_others_to_informacao(self):
        eventos = [
            _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE),
            _fake_event(win32evtlog.EVENTLOG_WARNING_TYPE),
            _fake_event(win32evtlog.EVENTLOG_INFORMATION_TYPE),
        ]
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=eventos), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value="msg"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_logs_sistema(num_registros=10)

        self.assertEqual([e["tipo"] for e in resultado], ["ERRO", "AVISO", "Informação"])

    def test_respects_num_registros_limit_even_with_more_events_available(self):
        eventos = [_fake_event(win32evtlog.EVENTLOG_INFORMATION_TYPE) for _ in range(50)]
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=eventos), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value="msg"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_logs_sistema(num_registros=3)

        self.assertEqual(len(resultado), 3)

    def test_binary_or_unreadable_message_falls_back_to_placeholder(self):
        eventos = [_fake_event(win32evtlog.EVENTLOG_INFORMATION_TYPE)]
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=eventos), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", side_effect=Exception("binário")), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_logs_sistema(num_registros=10)

        self.assertEqual(resultado[0]["mensagem"], "Mensagem indisponível ou binária.")

    def test_empty_message_falls_back_to_sem_descricao(self):
        eventos = [_fake_event(win32evtlog.EVENTLOG_INFORMATION_TYPE)]
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=eventos), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value=""), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_logs_sistema(num_registros=10)

        self.assertEqual(resultado[0]["mensagem"], "Sem descrição")

    def test_log_open_failure_returns_empty_list_without_raising(self):
        # Ex.: log "Security" sem privilégio de administrador - não pode
        # derrubar o resto do app, só essa aba fica vazia.
        with patch("backend.os_logs.win32evtlog.OpenEventLog", side_effect=Exception("acesso negado")):
            resultado = buscar_logs_sistema(log_type="Security", num_registros=10)

        self.assertEqual(resultado, [])


if __name__ == "__main__":
    unittest.main()
