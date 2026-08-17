"""Testes para backend/app.py - o orquestrador que liga a descoberta de
arquivos de log, o tail (file_tail) e o lançamento de processos. É o
código mais arriscado do backend: mistura threads, subprocess e resolução
de caminho, sem nenhuma cobertura até aqui."""

import os
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psutil

from backend.app import ProcWatchApp, find_log_files


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _open_file(path):
    return types.SimpleNamespace(path=path)


class FindLogFilesTests(unittest.TestCase):
    def test_matches_files_by_extension_or_log_in_path(self):
        arquivos = [
            _open_file(r"C:\app\saida.log"),
            _open_file(r"C:\app\config.txt"),
            _open_file(r"C:\app\registro_de_log_customizado.dat"),
            _open_file(r"C:\app\dados.bin"),
        ]
        with patch("backend.app.psutil.Process") as MockProcess:
            MockProcess.return_value.open_files.return_value = arquivos
            resultado = find_log_files(1234)

        self.assertEqual(
            resultado,
            [r"C:\app\saida.log", r"C:\app\config.txt", r"C:\app\registro_de_log_customizado.dat"],
        )

    def test_matching_is_case_insensitive(self):
        with patch("backend.app.psutil.Process") as MockProcess:
            MockProcess.return_value.open_files.return_value = [_open_file(r"C:\app\SAIDA.LOG")]
            resultado = find_log_files(1234)
        self.assertEqual(resultado, [r"C:\app\SAIDA.LOG"])

    def test_process_gone_returns_empty_list(self):
        with patch("backend.app.psutil.Process", side_effect=psutil.NoSuchProcess(1234)):
            self.assertEqual(find_log_files(1234), [])

    def test_access_denied_returns_empty_list(self):
        with patch("backend.app.psutil.Process", side_effect=psutil.AccessDenied(1234)):
            self.assertEqual(find_log_files(1234), [])


class _RecordingTail:
    """Substitui monitorar_arquivo/monitorar_stream nos testes: só registra
    a chamada (sem bloquear em loop) - a lógica de tail em si já tem
    cobertura própria em test_file_tail.py. O que importa aqui é SE e COM
    QUE argumentos o ProcWatchApp inicia o acompanhamento."""

    def __init__(self):
        self.calls = []
        self.event = threading.Event()

    def __call__(self, source, callback, stop_event):
        self.calls.append((source, callback, stop_event))
        self.event.set()


class WatchProcessLogsTests(unittest.TestCase):
    def test_starts_one_watch_per_log_file_and_returns_their_paths(self):
        tail = _RecordingTail()
        app = ProcWatchApp()
        received = []

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log", r"C:\app\b.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            resultado = app.watch_process_logs(111, lambda path, linha: received.append((path, linha)))

        self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))
        self.assertEqual(resultado, [r"C:\app\a.log", r"C:\app\b.log"])
        self.assertEqual({key for key in app._stop_events}, {"111:C:\\app\\a.log", "111:C:\\app\\b.log"})

    def test_callback_forwards_path_and_line_to_on_line(self):
        tail = _RecordingTail()
        app = ProcWatchApp()
        received = []

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda path, linha: received.append((path, linha)))
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 1))

            _source, callback, _stop_event = tail.calls[0]
            callback("uma linha de log")

        self.assertEqual(received, [(r"C:\app\a.log", "uma linha de log")])

    def test_restarting_the_same_pid_stops_the_previous_watch(self):
        tail = _RecordingTail()
        app = ProcWatchApp()

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 1))
            primeiro_stop_event = tail.calls[0][2]

            app.watch_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))

        self.assertTrue(primeiro_stop_event.is_set(), "watch anterior do mesmo PID deveria ter sido parado")


class SyncProcessLogsTests(unittest.TestCase):
    def test_does_not_restart_already_watched_files(self):
        tail = _RecordingTail()
        app = ProcWatchApp()

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 1))

            app.sync_process_logs(111, lambda *_: None)  # mesmo arquivo, de novo
            time.sleep(0.1)  # dá chance de uma segunda chamada indevida acontecer

        self.assertEqual(len(tail.calls), 1, "arquivo já monitorado não deveria reiniciar o tail")

    def test_starts_watching_newly_discovered_files_only(self):
        tail = _RecordingTail()
        app = ProcWatchApp()

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 1))

        with patch("backend.app.find_log_files", return_value=[r"C:\app\a.log", r"C:\app\b.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            resultado = app.sync_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))

        self.assertEqual(tail.calls[1][0], r"C:\app\b.log")
        self.assertEqual(set(resultado), {r"C:\app\a.log", r"C:\app\b.log"})


class LaunchAndWatchTests(unittest.TestCase):
    def _fake_popen(self, pid=555, exit_code=0, stdout=True, stderr=True):
        proc = MagicMock()
        proc.pid = pid
        proc.stdout = MagicMock() if stdout else None
        proc.stderr = MagicMock() if stderr else None
        proc.wait.return_value = exit_code
        return proc

    def test_launches_subprocess_with_expected_flags_and_no_console_window(self):
        app = ProcWatchApp()
        proc = self._fake_popen()

        with patch("backend.app.subprocess.Popen", return_value=proc) as MockPopen, \
             patch("backend.app.ProcWatchApp._watch_subprocess_output"):
            app.launch_and_watch(r"C:\app\ferramenta.exe", lambda *_: None, lambda *_: None)

        _args, kwargs = MockPopen.call_args
        self.assertEqual(_args[0], [r"C:\app\ferramenta.exe"])
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertTrue(kwargs["text"])
        if sys.platform == "win32":
            self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_watches_both_stdout_and_stderr(self):
        tail = _RecordingTail()
        app = ProcWatchApp()
        proc = self._fake_popen(pid=555)

        with patch("backend.app.subprocess.Popen", return_value=proc), \
             patch("backend.app.monitorar_stream", tail):
            app.launch_and_watch(r"C:\app\ferramenta.exe", lambda *_: None, lambda *_: None)

        self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))
        self.assertEqual({key for key in app._stop_events}, {"555:stdout", "555:stderr"})

    def test_skips_watching_a_stream_that_is_none(self):
        tail = _RecordingTail()
        app = ProcWatchApp()
        proc = self._fake_popen(pid=555, stderr=False)

        with patch("backend.app.subprocess.Popen", return_value=proc), \
             patch("backend.app.monitorar_stream", tail):
            app.launch_and_watch(r"C:\app\ferramenta.exe", lambda *_: None, lambda *_: None)

        self.assertTrue(_wait_until(lambda: len(tail.calls) == 1))
        self.assertEqual(set(app._stop_events), {"555:stdout"})

    def test_calls_on_exit_with_pid_and_exit_code_when_process_ends(self):
        app = ProcWatchApp()
        proc = self._fake_popen(pid=555, exit_code=1)
        resultado = []

        with patch("backend.app.subprocess.Popen", return_value=proc), \
             patch("backend.app.ProcWatchApp._watch_subprocess_output"):
            app.launch_and_watch(r"C:\app\ferramenta.exe", lambda *_: None, lambda pid, code: resultado.append((pid, code)))

        self.assertTrue(_wait_until(lambda: len(resultado) == 1))
        self.assertEqual(resultado, [(555, 1)])


class StopTests(unittest.TestCase):
    def test_stop_watching_pid_only_affects_that_pid(self):
        tail = _RecordingTail()
        app = ProcWatchApp()

        with patch("backend.app.find_log_files", side_effect=[[r"C:\a.log"], [r"C:\b.log"]]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda *_: None)
            app.watch_process_logs(222, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))

        stop_event_111 = app._stop_events["111:C:\\a.log"]
        app.stop_watching_pid(111)

        self.assertTrue(stop_event_111.is_set())
        self.assertNotIn("111:C:\\a.log", app._stop_events)
        self.assertIn("222:C:\\b.log", app._stop_events)

    def test_stop_all_clears_every_watch(self):
        tail = _RecordingTail()
        app = ProcWatchApp()

        with patch("backend.app.find_log_files", return_value=[r"C:\a.log", r"C:\b.log"]), \
             patch("backend.app.monitorar_arquivo", tail):
            app.watch_process_logs(111, lambda *_: None)
            self.assertTrue(_wait_until(lambda: len(tail.calls) == 2))

        stop_events = list(app._stop_events.values())
        app.stop_all()

        self.assertTrue(all(e.is_set() for e in stop_events))
        self.assertEqual(app._stop_events, {})
        self.assertEqual(app._threads, {})


class GetOsLogsTests(unittest.TestCase):
    def test_delegates_to_buscar_logs_sistema_with_limit(self):
        app = ProcWatchApp()
        with patch("backend.app.buscar_logs_sistema", return_value=[{"tipo": "ERRO"}]) as mock_buscar:
            resultado = app.get_os_logs(limit=7)

        mock_buscar.assert_called_once_with(num_registros=7)
        self.assertEqual(resultado, [{"tipo": "ERRO"}])


if __name__ == "__main__":
    unittest.main()
