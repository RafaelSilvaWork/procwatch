"""Descoberta e monitoramento dos arquivos de log de um processo selecionado,
e lançamento/acompanhamento de um novo processo pelo próprio LogWatch."""

import subprocess
import sys
import threading
from typing import Callable, Dict, List

import psutil

from .file_tail import monitorar_arquivo, monitorar_stream
from .os_logs import buscar_logs_sistema

LOG_EXTENSIONS = (".log", ".txt", ".out", ".err")


def find_log_files(pid: int) -> List[str]:
    """Lista arquivos abertos pelo processo que parecem ser logs."""
    try:
        open_files = psutil.Process(pid).open_files()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []

    return [
        f.path for f in open_files
        if f.path.lower().endswith(LOG_EXTENSIONS) or "log" in f.path.lower()
    ]


class LogWatchApp:
    """Coordena o monitoramento dos arquivos de log de um processo."""

    def __init__(self):
        self._stop_events: Dict[str, threading.Event] = {}
        self._threads: Dict[str, threading.Thread] = {}

    def watch_process_logs(self, pid: int, on_line: Callable[[str, str], None]) -> List[str]:
        """Para o monitoramento anterior e passa a monitorar os arquivos de
        log encontrados para o PID informado. Retorna os caminhos monitorados.
        `on_line(caminho, linha)` é chamado a cada nova linha."""
        self.stop_all()

        log_files = find_log_files(pid)
        for path in log_files:
            stop_event = threading.Event()
            self._stop_events[path] = stop_event

            thread = threading.Thread(
                target=monitorar_arquivo,
                args=(path, lambda linha, p=path: on_line(p, linha), stop_event),
                daemon=True,
            )
            self._threads[path] = thread
            thread.start()

        return log_files

    def sync_process_logs(self, pid: int, on_line: Callable[[str, str], None]) -> List[str]:
        """Descobre arquivos de log NOVOS abertos pelo processo e passa a
        monitorá-los, sem reiniciar o tail dos que já estão sendo
        acompanhados (ao contrário de watch_process_logs). Útil para pegar
        logs criados/abertos depois da seleção inicial (ex.: rotação diária).
        Retorna todos os caminhos atualmente monitorados para este processo."""
        for path in find_log_files(pid):
            if path in self._stop_events:
                continue

            stop_event = threading.Event()
            self._stop_events[path] = stop_event

            thread = threading.Thread(
                target=monitorar_arquivo,
                args=(path, lambda linha, p=path: on_line(p, linha), stop_event),
                daemon=True,
            )
            self._threads[path] = thread
            thread.start()

        return list(self._stop_events.keys())

    def launch_and_watch(
        self, path: str,
        on_line: Callable[[str, str], None],
        on_exit: Callable[[int, int], None],
    ) -> subprocess.Popen:
        """Abre um executável e passa a acompanhar seu stdout/stderr em
        tempo real (on_line(rótulo, linha)), notificando quando ele
        encerrar (on_exit(pid, código_de_saída))."""
        self.stop_all()

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        proc = subprocess.Popen(
            [path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )

        self._watch_subprocess_output(proc, on_line)

        def _wait():
            code = proc.wait()
            on_exit(proc.pid, code)

        threading.Thread(target=_wait, daemon=True).start()
        return proc

    def _watch_subprocess_output(self, proc: subprocess.Popen, on_line: Callable[[str, str], None]) -> None:
        for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is None:
                continue

            stop_event = threading.Event()
            key = f"pid:{proc.pid}:{label}"
            self._stop_events[key] = stop_event

            thread = threading.Thread(
                target=monitorar_stream,
                args=(stream, lambda linha, l=label: on_line(l, linha), stop_event),
                daemon=True,
            )
            self._threads[key] = thread
            thread.start()

    def stop_all(self) -> None:
        """Para o monitoramento de todos os arquivos ativos."""
        for stop_event in self._stop_events.values():
            stop_event.set()
        self._stop_events.clear()
        self._threads.clear()

    def get_os_logs(self, limit: int = 50) -> List[Dict]:
        """Retorna logs recentes do sistema operacional (Visualizador de Eventos)."""
        return buscar_logs_sistema(num_registros=limit)
