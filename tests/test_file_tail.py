"""Testes para o tail incremental de arquivo e de stream de processo
(backend/file_tail.py) - o código que mais sofre com casos de borda reais:
arquivo girado, linha cortada, stream fechado no meio da leitura."""

import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from backend.file_tail import monitorar_arquivo, monitorar_stream


def _wait_until(predicate, timeout=2.0, interval=0.05):
    """Espera até `predicate()` ser verdadeiro ou o timeout estourar -
    monitorar_arquivo faz polling a cada 0.5s, então os testes não podem
    depender de um único sleep fixo sem virar flaky."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class MonitorarArquivoTests(unittest.TestCase):
    def test_missing_file_returns_immediately_without_calling_callback(self):
        calls = []
        monitorar_arquivo(os.path.join(tempfile.gettempdir(), "nao-existe-procwatch.log"), calls.append)
        self.assertEqual(calls, [])

    def _start_tail(self, path):
        calls = []
        stop_event = threading.Event()
        thread = threading.Thread(
            target=monitorar_arquivo, args=(path, calls.append, stop_event), daemon=True
        )
        thread.start()
        # Dá tempo do monitor abrir o arquivo e ir pro final ANTES de
        # escrevermos qualquer coisa nova - senão a linha "nova" pode ser
        # escrita antes do seek(SEEK_END) e viraria conteúdo "antigo".
        time.sleep(0.2)
        return calls, stop_event, thread

    def test_does_not_replay_content_written_before_monitoring_started(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            f.write("linha antiga\n")
            path = f.name
        self.addCleanup(os.unlink, path)

        calls, stop_event, thread = self._start_tail(path)
        try:
            with open(path, "a") as f:
                f.write("linha nova\n")

            self.assertTrue(_wait_until(lambda: len(calls) >= 1))
            self.assertEqual(calls, ["linha nova"])
        finally:
            stop_event.set()
            thread.join(timeout=2)

    def test_delivers_multiple_appended_lines_in_order_stripped(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            path = f.name
        self.addCleanup(os.unlink, path)

        calls, stop_event, thread = self._start_tail(path)
        try:
            with open(path, "a") as f:
                f.write("primeira  \nsegunda\n")

            self.assertTrue(_wait_until(lambda: len(calls) >= 2))
            self.assertEqual(calls, ["primeira", "segunda"])
        finally:
            stop_event.set()
            thread.join(timeout=2)

    def test_stop_event_halts_loop_promptly(self):
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".log") as f:
            path = f.name
        self.addCleanup(os.unlink, path)

        calls, stop_event, thread = self._start_tail(path)
        stop_event.set()
        # O loop só checa o evento entre iterações (poll de até 0.5s) -
        # 2s de folga é generoso o bastante pra não ser flaky.
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), "thread não encerrou após stop_event.set()")


class _FakeStream:
    """Substitui stdout/stderr de um subprocesso real nos testes de
    monitorar_stream - readline() imita a semântica de io.TextIOWrapper:
    string vazia sinaliza EOF."""

    def __init__(self, lines, close_raises=False):
        self._lines = list(lines)
        self._raise_on_read_after = None
        self.close_raises = close_raises
        self.closed = False

    def readline(self):
        if self._raise_on_read_after is not None and not self._lines[:self._raise_on_read_after]:
            raise OSError("pipe fechado")
        if not self._lines:
            return ""
        return self._lines.pop(0)

    def close(self):
        self.closed = True
        if self.close_raises:
            raise OSError("já estava fechado")


class MonitorarStreamTests(unittest.TestCase):
    def test_delivers_all_lines_until_eof_and_closes_stream(self):
        stream = _FakeStream(["linha 1\n", "linha 2\n"])
        calls = []

        monitorar_stream(stream, calls.append)

        self.assertEqual(calls, ["linha 1", "linha 2"])
        self.assertTrue(stream.closed)

    def test_stop_event_set_mid_stream_stops_before_remaining_lines(self):
        stream = _FakeStream(["linha 1\n", "linha 2\n", "linha 3\n"])
        stop_event = threading.Event()
        calls = []

        def callback(linha):
            calls.append(linha)
            if linha == "linha 1":
                stop_event.set()

        monitorar_stream(stream, callback, stop_event)

        self.assertEqual(calls, ["linha 1"])
        self.assertTrue(stream.closed)

    def test_stream_error_mid_read_is_swallowed_and_stream_still_closed(self):
        class _RaisingStream(_FakeStream):
            def readline(self):
                if not self._lines:
                    raise ValueError("I/O operation on closed file")
                return self._lines.pop(0)

        stream = _RaisingStream(["linha 1\n"])
        calls = []

        monitorar_stream(stream, calls.append)  # não deve propagar ValueError

        self.assertEqual(calls, ["linha 1"])
        self.assertTrue(stream.closed)

    def test_close_raising_does_not_propagate(self):
        stream = _FakeStream([], close_raises=True)
        monitorar_stream(stream, lambda linha: None)  # não deve propagar


if __name__ == "__main__":
    unittest.main()
