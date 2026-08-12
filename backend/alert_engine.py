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
        'memory_leak_growth_mb': 50.0,
        'memory_leak_min_samples': 10,
    }

    # Janela de amostras usada tanto pela CPU sustentada quanto pela
    # detecção de tendência de memória.
    HISTORY_SIZE = 30
    
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
        # Palavras-chave extras definidas pelo usuário, além das de fábrica
        # (CRITICAL_ERROR_KEYWORDS / WARNING_ERROR_KEYWORDS).
        self._custom_critical_keywords: List[str] = []
        self._custom_warning_keywords: List[str] = []
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

    def set_custom_keywords(self, critical: List[str], warning: List[str]) -> None:
        """Substitui as palavras-chave extras definidas pelo usuário (as de
        fábrica continuam sempre ativas, independente disso)."""
        with self._lock:
            self._custom_critical_keywords = [k.strip().lower() for k in critical if k.strip()]
            self._custom_warning_keywords = [k.strip().lower() for k in warning if k.strip()]

    def get_custom_keywords(self) -> tuple:
        with self._lock:
            return list(self._custom_critical_keywords), list(self._custom_warning_keywords)
    
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
        if len(self._process_history[pid]) > self.HISTORY_SIZE:
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

        self._check_memory_trend(snapshot, history)

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

    def _check_memory_trend(self, snapshot: ProcessSnapshot, history: List[ProcessSnapshot]) -> None:
        """Detecta memória que só cresce ao longo da janela de histórico,
        sem nunca cair - sintoma clássico de vazamento. Diferente do
        threshold instantâneo: aqui o processo pode estar bem abaixo do
        limite de memória, mas crescendo continuamente."""
        pid = snapshot.pid
        min_samples = int(self.thresholds.get('memory_leak_min_samples', 10))
        if len(history) < min_samples:
            return

        window = history[-min_samples:]
        growth = window[-1].memory_mb - window[0].memory_mb
        deltas = [b.memory_mb - a.memory_mb for a, b in zip(window, window[1:])]
        # tolera pequenas oscilações (ruído de medição), mas exige que a
        # maioria das amostras não tenha diminuído
        non_decreasing_ratio = sum(1 for d in deltas if d >= -0.5) / len(deltas)

        if (growth >= self.thresholds.get('memory_leak_growth_mb', 50.0)
                and non_decreasing_ratio >= 0.8):
            alert_id = f"memory_leak_{pid}"
            if alert_id not in self._suppressed_alerts:
                self._emit_alert(
                    AlertEvent(
                        title=f"Possível Vazamento de Memória - {snapshot.name}",
                        message=(
                            f"{snapshot.name} (PID {pid}) cresceu {growth:.1f}MB "
                            f"nas últimas {min_samples} amostras, sem liberar"
                        ),
                        severity=AlertSeverity.WARNING,
                        source=AlertSource.PROCESS,
                        process_pid=pid,
                        process_name=snapshot.name,
                        extra_data={'growth_mb': growth, 'samples': min_samples}
                    )
                )
                self._suppress_for(alert_id, 300)

    def check_log_entry(self, message: str, source: AlertSource = AlertSource.APP_LOG) -> None:
        message_lower = message.lower()
        with self._lock:
            critical_keywords = self.CRITICAL_ERROR_KEYWORDS + self._custom_critical_keywords
            warning_keywords = self.WARNING_ERROR_KEYWORDS + self._custom_warning_keywords

        for keyword in critical_keywords:
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

        for keyword in warning_keywords:
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
