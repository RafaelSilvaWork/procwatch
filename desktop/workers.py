"""Workers de fundo da janela principal - rodam em threads próprias para não
bloquear a UI (listagem de processos e checagem do engine de alertas)."""

import logging
import threading
from typing import List

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from backend.alert_engine import AlertEngine
from backend.models import AlertEvent, AlertSource, ProcessSnapshot
from backend.process_monitor import ProcessMonitor, get_system_stats

logger = logging.getLogger(__name__)


class ProcessMonitorThread(QThread):
    """Thread para listar processos disponíveis."""
    processes_updated = pyqtSignal(list)
    system_stats_updated = pyqtSignal(object)  # SystemStats

    def __init__(self, interval: float = 2.0, max_processes: int = 200):
        super().__init__()
        self.monitor = ProcessMonitor(update_interval=interval, max_processes=max_processes)
        self._stop_event = threading.Event()

    def _on_snapshots(self, snapshots):
        self.processes_updated.emit(snapshots)
        try:
            self.system_stats_updated.emit(get_system_stats())
        except Exception:
            logger.exception("Erro ao coletar estatísticas do sistema")

    def run(self):
        self.monitor.start(self._on_snapshots)
        # O trabalho de verdade roda na thread própria do ProcessMonitor;
        # esta thread só precisa existir até stop() ser chamado. Um Event
        # bloqueia sem consumir CPU, ao contrário de um time.sleep(0.1) em
        # loop (que acordava ~10x/s à toa pela vida inteira do app).
        self._stop_event.wait()

    def stop(self):
        self.monitor.stop()
        self._stop_event.set()

    def request_refresh(self):
        self.monitor.request_refresh()

    def pin_pid(self, pid: int):
        self.monitor.pin_pid(pid)

    def unpin_pid(self, pid: int):
        self.monitor.unpin_pid(pid)


class AlertWorker(QObject):
    """Roda o engine de alertas. Vive em sua própria thread via moveToThread,
    para que a checagem de thresholds não bloqueie a UI."""
    alert_triggered = pyqtSignal(AlertEvent)

    def __init__(self):
        super().__init__()
        self.engine = AlertEngine()
        self.engine.subscribe(self.alert_triggered.emit)

    def check_processes(self, snapshots: List[ProcessSnapshot]):
        self.engine.check_processes(snapshots)

    def check_log(self, message: str, source: AlertSource = AlertSource.APP_LOG):
        self.engine.check_log_entry(message, source)

    def check_process_exit(self, name: str, pid: int, exit_code: int):
        self.engine.check_process_exit(name, pid, exit_code)
