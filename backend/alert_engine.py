"""Engine de alertas - detecta erros críticos em processos e logs."""

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Set
from datetime import datetime, timedelta
from collections import defaultdict

from .models import ProcessSnapshot, AlertEvent, AlertSeverity, AlertSource

logger = logging.getLogger(__name__)


class AlertEngine:
    """Motor de alertas baseado em thresholds e padrões de erros."""
    
    DEFAULT_THRESHOLDS = {
        'cpu_critical': 95.0,
        'cpu_warning': 80.0,
        'memory_critical': 90.0,
        'memory_warning': 75.0,
        'sustained_high_cpu_secs': 10,
    }
    
    CRITICAL_ERROR_KEYWORDS = [
        'fatal', 'critical', 'crash', 'panic', 'segmentation', 'kernel panic',
        'access violation', 'memory corruption', 'stack overflow'
    ]
    
    WARNING_ERROR_KEYWORDS = [
        'error', 'exception', 'failed', 'timeout', 'refused', 'not found',
        'denied', 'invalid', 'corrupt'
    ]
    
    def __init__(self, thresholds: Optional[Dict[str, float]] = None):
        self.thresholds = {**self.DEFAULT_THRESHOLDS, **(thresholds or {})}
        self._alerts: List[AlertEvent] = []
        self._callbacks: List[Callable[[AlertEvent], None]] = []
        self._process_history: Dict[int, List[ProcessSnapshot]] = defaultdict(list)
        self._suppressed_alerts: Set[str] = set()
        # RLock (reentrante): check_processes segura o lock e chama, na mesma
        # thread, _check_single_process -> _emit_alert, que também precisa do
        # lock. Com um Lock comum isso é deadlock garantido no 1º alerta.
        self._lock = threading.RLock()
    
    def subscribe(self, callback: Callable[[AlertEvent], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def update_thresholds(self, new_thresholds: Dict[str, float]) -> None:
        """Atualiza os limites em tempo real (thread-safe)."""
        with self._lock:
            self.thresholds.update(new_thresholds)
    
    def check_processes(self, snapshots: List[ProcessSnapshot]) -> None:
        with self._lock:
            self._prune_dead_processes({s.pid for s in snapshots})
            for snapshot in snapshots:
                self._check_single_process(snapshot)

    def _prune_dead_processes(self, current_pids: Set[int]) -> None:
        """Remove do histórico PIDs que não aparecem mais no snapshot atual
        (processo encerrado) - sem isso, o histórico cresce para sempre."""
        dead_pids = set(self._process_history.keys()) - current_pids
        for pid in dead_pids:
            del self._process_history[pid]

    def _suppress_for(self, alert_id: str, seconds: float) -> None:
        """Marca um alerta como suprimido por um tempo. Usa daemon=True para
        não impedir o processo de encerrar enquanto o timer está pendente."""
        self._suppressed_alerts.add(alert_id)
        timer = threading.Timer(seconds, lambda: self._suppressed_alerts.discard(alert_id))
        timer.daemon = True
        timer.start()
    
    def _check_single_process(self, snapshot: ProcessSnapshot) -> None:
        pid = snapshot.pid
        
        self._process_history[pid].append(snapshot)
        if len(self._process_history[pid]) > 15:
            self._process_history[pid].pop(0)
        
        # CPU Crítica
        if snapshot.cpu_percent >= self.thresholds['cpu_critical']:
            alert_id = f"cpu_critical_{pid}"
            if alert_id not in self._suppressed_alerts:
                self._emit_alert(
                    AlertEvent(
                        title=f"CPU Crítica - {snapshot.name}",
                        message=f"Processo {snapshot.name} (PID {pid}) usando {snapshot.cpu_percent:.1f}% de CPU",
                        severity=AlertSeverity.CRITICAL,
                        source=AlertSource.PROCESS,
                        process_pid=pid,
                        process_name=snapshot.name,
                        extra_data={'cpu_percent': snapshot.cpu_percent}
                    )
                )
                self._suppress_for(alert_id, 30)
        
        # CPU Sustentada Alta
        history = self._process_history[pid]
        if len(history) >= self.thresholds['sustained_high_cpu_secs']:
            recent_cpu = [s.cpu_percent for s in history[-int(self.thresholds['sustained_high_cpu_secs']):]]
            avg_cpu = sum(recent_cpu) / len(recent_cpu)
            
            if avg_cpu >= self.thresholds['cpu_warning'] and avg_cpu < self.thresholds['cpu_critical']:
                alert_id = f"cpu_sustained_{pid}"
                if alert_id not in self._suppressed_alerts:
                    self._emit_alert(
                        AlertEvent(
                            title=f"CPU Elevada Sustentada - {snapshot.name}",
                            message=f"{snapshot.name} (PID {pid}) com CPU média de {avg_cpu:.1f}% nos últimos 10s",
                            severity=AlertSeverity.WARNING,
                            source=AlertSource.PROCESS,
                            process_pid=pid,
                            process_name=snapshot.name,
                            extra_data={'avg_cpu_10s': avg_cpu}
                        )
                    )
                    self._suppress_for(alert_id, 60)
        
        # Memória Crítica
        if snapshot.memory_percent >= self.thresholds['memory_critical']:
            alert_id = f"mem_critical_{pid}"
            if alert_id not in self._suppressed_alerts:
                self._emit_alert(
                    AlertEvent(
                        title=f"Memória Crítica - {snapshot.name}",
                        message=f"{snapshot.name} (PID {pid}) usando {snapshot.memory_mb:.1f}MB ({snapshot.memory_percent:.1f}% da RAM total)",
                        severity=AlertSeverity.CRITICAL,
                        source=AlertSource.PROCESS,
                        process_pid=pid,
                        process_name=snapshot.name,
                        extra_data={'memory_mb': snapshot.memory_mb, 'memory_percent': snapshot.memory_percent}
                    )
                )
                self._suppress_for(alert_id, 30)

        # Memória Aviso
        elif snapshot.memory_percent >= self.thresholds['memory_warning']:
            alert_id = f"mem_warning_{pid}"
            if alert_id not in self._suppressed_alerts:
                self._emit_alert(
                    AlertEvent(
                        title=f"Memória Elevada - {snapshot.name}",
                        message=f"{snapshot.name} (PID {pid}) usando {snapshot.memory_mb:.1f}MB ({snapshot.memory_percent:.1f}% da RAM)",
                        severity=AlertSeverity.WARNING,
                        source=AlertSource.PROCESS,
                        process_pid=pid,
                        process_name=snapshot.name,
                        extra_data={'memory_mb': snapshot.memory_mb, 'memory_percent': snapshot.memory_percent}
                    )
                )
                self._suppress_for(alert_id, 60)

        # Processo Zumbi
        if snapshot.status == 'zombie':
            alert_id = f"zombie_{pid}"
            if alert_id not in self._suppressed_alerts:
                self._emit_alert(
                    AlertEvent(
                        title=f"Processo Zumbi - {snapshot.name}",
                        message=f"{snapshot.name} (PID {pid}) está em estado zumbi (possível vazamento de recursos)",
                        severity=AlertSeverity.ERROR,
                        source=AlertSource.PROCESS,
                        process_pid=pid,
                        process_name=snapshot.name
                    )
                )
                self._suppress_for(alert_id, 120)
    
    def check_log_entry(self, message: str, source: AlertSource = AlertSource.APP_LOG) -> None:
        message_lower = message.lower()
        
        for keyword in self.CRITICAL_ERROR_KEYWORDS:
            if keyword in message_lower:
                alert_id = f"critical_log_{hash(message) % 10000}"
                if alert_id not in self._suppressed_alerts:
                    self._emit_alert(
                        AlertEvent(
                            title=f"Erro Crítico Detectado",
                            message=f"{source.value}: {message[:100]}...",
                            severity=AlertSeverity.CRITICAL,
                            source=source,
                            extra_data={'log_message': message}
                        )
                    )
                    self._suppress_for(alert_id, 60)
                return
        
        for keyword in self.WARNING_ERROR_KEYWORDS:
            if keyword in message_lower:
                alert_id = f"warning_log_{hash(message) % 10000}"
                if alert_id not in self._suppressed_alerts:
                    self._emit_alert(
                        AlertEvent(
                            title=f"Aviso de Erro",
                            message=f"{source.value}: {message[:100]}...",
                            severity=AlertSeverity.WARNING,
                            source=source,
                            extra_data={'log_message': message}
                        )
                    )
                    self._suppress_for(alert_id, 300)
    
    def _emit_alert(self, alert: AlertEvent) -> None:
        with self._lock:
            self._alerts.append(alert)
            if len(self._alerts) > 1000:
                self._alerts.pop(0)
        
        for callback in self._callbacks:
            try:
                callback(alert)
            except Exception:
                logger.exception("Erro ao executar callback de alerta")
    
    def get_recent_alerts(self, limit: int = 50, severity: Optional[AlertSeverity] = None) -> List[AlertEvent]:
        with self._lock:
            alerts = self._alerts[-limit:]
            if severity:
                alerts = [a for a in alerts if a.severity == severity]
            return list(reversed(alerts))
    
    def clear_alerts(self) -> None:
        with self._lock:
            self._alerts.clear()
