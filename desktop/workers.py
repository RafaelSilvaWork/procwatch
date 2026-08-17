"""Workers de fundo da janela principal - rodam em threads próprias para não
bloquear a UI (listagem de processos e checagem do engine de alertas)."""

import logging
import threading
from datetime import datetime
from typing import Dict, List

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from backend.alert_engine import AlertEngine
from backend.models import AlertEvent, AlertSource, ProcessSnapshot
from backend.os_logs import buscar_erros_criticos_do_processo
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
        # PID -> horário em que passou a ser monitorado. Usado pra ignorar,
        # na correlação com o Visualizador de Eventos, falhas antigas de
        # antes da execução atual (um crash de ontem não é notícia hoje).
        self._monitor_start: Dict[int, datetime] = {}

    def check_processes(self, snapshots: List[ProcessSnapshot]):
        self.engine.check_processes(snapshots)

    def check_log(self, message: str, source: AlertSource = AlertSource.APP_LOG):
        self.engine.check_log_entry(message, source)

    def check_process_exit(self, name: str, pid: int, exit_code: int):
        self.engine.check_process_exit(name, pid, exit_code)

    def track_monitoring_start(self, pid: int):
        self._monitor_start[pid] = datetime.now()

    def stop_tracking(self, pid: int):
        self._monitor_start.pop(pid, None)

    def check_os_log_events(self, monitored: Dict[int, str]):
        """Correlaciona o Visualizador de Eventos do Windows com cada
        processo monitorado que tem um horário de início rastreado. Roda na
        thread própria do worker - ler o log de eventos pode ser lento o
        bastante pra não fazer isso na thread da UI."""
        for pid, name in monitored.items():
            desde = self._monitor_start.get(pid)
            if desde is None:
                continue
            try:
                eventos = buscar_erros_criticos_do_processo(name, desde=desde)
            except Exception:
                logger.exception("Erro ao checar eventos do Windows para %s (PID %s)", name, pid)
                continue
            for evento in eventos:
                self.engine.check_os_log_correlation(
                    name, pid, evento["origem"], evento["data"], evento["mensagem"]
                )
