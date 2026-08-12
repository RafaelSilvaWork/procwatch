"""Descoberta e monitoramento dos arquivos de log de processos monitorados,
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
    """Coordena o monitoramento dos arquivos de log de vários processos ao
    mesmo tempo. Cada entrada é indexada por "{pid}:{caminho_ou_rotulo}",
    permitindo parar o acompanhamento de um único PID sem afetar os outros
    processos sendo monitorados simultaneamente."""

    def __init__(self):
        self._stop_events: Dict[str, threading.Event] = {}
        self._threads: Dict[str, threading.Thread] = {}

    def watch_process_logs(self, pid: int, on_line: Callable[[str, str], None]) -> List[str]:
        """Para o monitoramento anterior DESTE PID (não afeta outros
        processos monitorados) e passa a monitorar os arquivos de log
        encontrados para ele. `on_line(caminho, linha)` é chamado a cada
        nova linha. Retorna os caminhos monitorados."""
        self.stop_watching_pid(pid)

        log_files = find_log_files(pid)
        for path in log_files:
            self._start_watch(f"{pid}:{path}", monitorar_arquivo, path,
                               lambda linha, p=path: on_line(p, linha))

        return log_files

    def sync_process_logs(self, pid: int, on_line: Callable[[str, str], None]) -> List[str]:
        """Descobre arquivos de log NOVOS abertos pelo processo e passa a
        monitorá-los, sem reiniciar o tail dos que já estão sendo
        acompanhados (ao contrário de watch_process_logs). Útil para pegar
        logs criados/abertos depois da seleção inicial (ex.: rotação diária).
        Retorna todos os caminhos atualmente monitorados para este PID."""
        prefix = f"{pid}:"
        already_watched = {key[len(prefix):] for key in self._stop_events if key.startswith(prefix)}

        for path in find_log_files(pid):
            if path in already_watched:
                continue
            self._start_watch(f"{pid}:{path}", monitorar_arquivo, path,
                               lambda linha, p=path: on_line(p, linha))

        return [key[len(prefix):] for key in self._stop_events if key.startswith(prefix)]

    def launch_and_watch(
        self, path: str,
        on_line: Callable[[int, str, str], None],
        on_exit: Callable[[int, int], None],
    ) -> subprocess.Popen:
        """Abre um executável e passa a acompanhar seu stdout/stderr em
        tempo real (on_line(pid, rótulo, linha)), notificando quando ele
        encerrar (on_exit(pid, código_de_saída)). Não afeta o
        acompanhamento de outros processos já sendo monitorados."""
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

    def _watch_subprocess_output(self, proc: subprocess.Popen, on_line: Callable[[int, str, str], None]) -> None:
        for label, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is None:
                continue
            self._start_watch(f"{proc.pid}:{label}", monitorar_stream, stream,
                               lambda linha, l=label, p=proc.pid: on_line(p, l, linha))

    def _start_watch(self, key: str, target, source, callback) -> None:
        stop_event = threading.Event()
        self._stop_events[key] = stop_event

        thread = threading.Thread(target=target, args=(source, callback, stop_event), daemon=True)
        self._threads[key] = thread
        thread.start()

    def stop_watching_pid(self, pid: int) -> None:
        """Para o monitoramento de logs/saída de um PID específico, sem
        afetar o que está sendo monitorado para outros processos."""
        prefix = f"{pid}:"
        for key in [k for k in self._stop_events if k.startswith(prefix)]:
            self._stop_events[key].set()
            del self._stop_events[key]
            self._threads.pop(key, None)

    def stop_all(self) -> None:
        """Para o monitoramento de todos os arquivos/processos ativos."""
        for stop_event in self._stop_events.values():
            stop_event.set()
        self._stop_events.clear()
        self._threads.clear()

    def get_os_logs(self, limit: int = 50) -> List[Dict]:
        """Retorna logs recentes do sistema operacional (Visualizador de Eventos)."""
        return buscar_logs_sistema(num_registros=limit)
