"""Testes para a leitura do Visualizador de Eventos do Windows
(backend/os_logs.py). win32evtlog/win32evtlogutil são mockados - o que
importa aqui é o mapeamento de tipo de evento, o limite de registros e a
tolerância a falhas (evento binário, log inacessível), não a API do
Windows em si."""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import win32evtlog

from backend.os_logs import buscar_erros_criticos_do_processo, buscar_logs_sistema


def _fake_event(event_type, source="Origem", generated=None, message=None):
    return types.SimpleNamespace(
        EventType=event_type, SourceName=source,
        TimeGenerated=generated or datetime(2026, 1, 1, 12, 0, 0),
        _message=message,
    )


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


def _by_message(events_por_msg):
    """SafeFormatMessage fake que devolve uma mensagem diferente por
    evento, casada por identidade do objeto - necessário quando um teste
    precisa de vários eventos com conteúdo diferente na mesma chamada."""
    return lambda event, _log_type=None: events_por_msg[id(event)]


class BuscarErrosCriticosDoProcessoTests(unittest.TestCase):
    def test_matches_error_from_crash_related_source_mentioning_the_process(self):
        evento = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error")
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[evento]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage",
                   return_value="Faulting application name: jogo.exe, faulting module: MSVCP140.dll"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0]["origem"], "Application Error")

    def test_ignores_non_error_severity_even_from_a_crash_related_source(self):
        # Um WARNING de "Application Error" (raro, mas possível) não é o
        # crash que estamos procurando.
        evento = _fake_event(win32evtlog.EVENTLOG_WARNING_TYPE, source="Application Error")
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[evento]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value="jogo.exe travou"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(resultado, [])

    def test_ignores_error_from_a_source_unrelated_to_crashes(self):
        # Um app pode logar seu próprio ERRO no log de Aplicativo por
        # qualquer motivo interno - isso não é "o Windows detectou uma
        # falha real", é ruído genérico que check_log_entry já cobre.
        evento = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="MinhaAppCustomizada")
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[evento]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value="jogo.exe: erro genérico"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(resultado, [])

    def test_ignores_crash_event_from_an_unrelated_process(self):
        evento = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error")
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[evento]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage",
                   return_value="Faulting application name: outroprograma.exe"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(resultado, [])

    def test_name_matching_is_case_insensitive(self):
        evento = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error")
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[evento]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage",
                   return_value="Faulting application name: JOGO.EXE"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(len(resultado), 1)

    def test_stops_at_events_older_than_desde_cutoff(self):
        agora = datetime(2026, 1, 1, 12, 0, 0)
        # Leitura é EVENTLOG_BACKWARDS_READ - mais recente primeiro.
        recente = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error",
                               generated=agora, message="Faulting application name: jogo.exe (novo)")
        antigo = _fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error",
                              generated=agora - timedelta(hours=2), message="Faulting application name: jogo.exe (antigo)")

        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=[recente, antigo]), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage",
                   side_effect=_by_message({id(recente): recente._message, id(antigo): antigo._message})), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe", desde=agora - timedelta(minutes=30))

        self.assertEqual(len(resultado), 1)
        self.assertIn("novo", resultado[0]["mensagem"])

    def test_respects_num_registros_limit(self):
        eventos = [_fake_event(win32evtlog.EVENTLOG_ERROR_TYPE, source="Application Error") for _ in range(10)]
        with patch("backend.os_logs.win32evtlog.OpenEventLog", return_value=object()), \
             patch("backend.os_logs.win32evtlog.ReadEventLog", return_value=eventos), \
             patch("backend.os_logs.win32evtlogutil.SafeFormatMessage", return_value="jogo.exe falhou"), \
             patch("backend.os_logs.win32evtlog.CloseEventLog"):
            resultado = buscar_erros_criticos_do_processo("jogo.exe", num_registros=3)

        self.assertEqual(len(resultado), 3)

    def test_log_open_failure_returns_empty_list_without_raising(self):
        with patch("backend.os_logs.win32evtlog.OpenEventLog", side_effect=Exception("acesso negado")):
            resultado = buscar_erros_criticos_do_processo("jogo.exe")

        self.assertEqual(resultado, [])


if __name__ == "__main__":
    unittest.main()
