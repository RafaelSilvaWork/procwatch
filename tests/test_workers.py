"""Testes para desktop/workers.py - especificamente o rastreamento de
início de monitoramento e a correlação com o Visualizador de Eventos do
Windows no AlertWorker (chamados aqui como métodos Python diretos, sem
precisar de QApplication nem da fila de sinais do Qt)."""

import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from desktop.workers import AlertWorker


class TrackMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.worker = AlertWorker()

    def test_track_monitoring_start_records_a_timestamp(self):
        self.worker.track_monitoring_start(111)
        self.assertIn(111, self.worker._monitor_start)
        self.assertIsInstance(self.worker._monitor_start[111], datetime)

    def test_stop_tracking_removes_the_pid(self):
        self.worker.track_monitoring_start(111)
        self.worker.stop_tracking(111)
        self.assertNotIn(111, self.worker._monitor_start)

    def test_stop_tracking_unknown_pid_does_not_raise(self):
        self.worker.stop_tracking(999)  # nunca foi rastreado


class CheckOsLogEventsTests(unittest.TestCase):
    def setUp(self):
        self.worker = AlertWorker()
        self.received = []
        self.worker.engine.subscribe(self.received.append)

    def test_skips_pid_without_a_tracked_start(self):
        # PID nunca passou por track_monitoring_start - não tem "desde" pra
        # comparar, então não checa (evita alertar sobre todo o histórico).
        with patch("desktop.workers.buscar_erros_criticos_do_processo") as mock_buscar:
            self.worker.check_os_log_events({111: "jogo.exe"})

        mock_buscar.assert_not_called()
        self.assertEqual(self.received, [])

    def test_forwards_found_events_to_the_engine(self):
        self.worker.track_monitoring_start(111)
        evento = {"origem": "Application Error", "data": "2026-01-01 10:00:00", "mensagem": "falha"}

        with patch("desktop.workers.buscar_erros_criticos_do_processo", return_value=[evento]) as mock_buscar:
            self.worker.check_os_log_events({111: "jogo.exe"})

        mock_buscar.assert_called_once()
        self.assertEqual(mock_buscar.call_args.args[0], "jogo.exe")
        self.assertEqual(mock_buscar.call_args.kwargs["desde"], self.worker._monitor_start[111])
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].process_pid, 111)

    def test_error_checking_one_pid_does_not_abort_the_others(self):
        self.worker.track_monitoring_start(111)
        self.worker.track_monitoring_start(222)

        def _flaky(nome, desde=None):
            if nome == "quebra.exe":
                raise Exception("Visualizador de Eventos indisponível")
            return [{"origem": "Application Error", "data": "2026-01-01 10:00:00", "mensagem": "falha"}]

        with patch("desktop.workers.buscar_erros_criticos_do_processo", side_effect=_flaky):
            self.worker.check_os_log_events({111: "quebra.exe", 222: "ok.exe"})

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].process_pid, 222)


if __name__ == "__main__":
    unittest.main()
