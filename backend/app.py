"""Descoberta e monitoramento dos arquivos de log de um processo selecionado."""

import threading
from typing import Callable, Dict, List

import psutil

from .file_tail import monitorar_arquivo
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

    def stop_all(self) -> None:
        """Para o monitoramento de todos os arquivos ativos."""
        for stop_event in self._stop_events.values():
            stop_event.set()
        self._stop_events.clear()
        self._threads.clear()

    def get_os_logs(self, limit: int = 50) -> List[Dict]:
        """Retorna logs recentes do sistema operacional (Visualizador de Eventos)."""
        return buscar_logs_sistema(num_registros=limit)
