"""Engine de alertas - detecta erros críticos em processos e logs."""

import logging
import os
import threading
from typing import Callable, Dict, List, Optional, Set
from collections import defaultdict

from .models import ProcessSnapshot, AlertEvent, AlertSeverity, AlertSource

logger = logging.getLogger(__name__)

# Processos do Windows que rotineiramente usam CPU alta (50-100%) como
# parte normal de operação (indexação, verificação de atualizações,
# antivírus, hospedagem de serviços) - alertar sobre eles é só ruído.
IGNORED_CPU_ALERT_PROCESS_NAMES = {
    "system", "system idle process",
    "svchost.exe",
    "msmpeng.exe", "nissrv.exe",  # Windows Defender
    "searchindexer.exe", "searchprotocolhost.exe", "searchfilterhost.exe",
    "trustedinstaller.exe", "tiworker.exe",  # Windows Update / servicing
    "dwm.exe", "wmiprvse.exe", "compattelrunner.exe",
}


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
        # Condições de processo (CPU/memória/zumbi/suspeito) atualmente
        # "em alerta" - dispara só na borda de subida (quando a condição
        # passa a ser verdadeira) e some quando ela deixa de ser, para
        # poder disparar de novo numa próxima vez. Evita o alerta repetir
        # a cada ciclo enquanto a condição continuar (ex.: CPU alta
        # sustentada por minutos não deveria virar um alerta novo a cada
        # 30s - só um, na hora que começou).
        self._active_conditions: Set[str] = set()
        # Usado só por check_log_entry e pela tendência de memória, que são
        # eventos pontuais (não uma condição contínua) - aí faz sentido
        # suprimir por um tempo em vez de por transição de estado.
        self._suppressed_alerts: Set[str] = set()
        # Palavras-chave extras definidas pelo usuário, além das de fábrica
        # (CRITICAL_ERROR_KEYWORDS / WARNING_ERROR_KEYWORDS).
        self._custom_critical_keywords: List[str] = []
        self._custom_warning_keywords: List[str] = []
        # PID do próprio ProcWatch - nunca alerta sobre si mesmo.
        self._self_pid = os.getpid()
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
            suffix = f"_{pid}"
            self._active_conditions = {c for c in self._active_conditions if not c.endswith(suffix)}

    def _suppress_for(self, alert_id: str, seconds: float) -> None:
        """Marca um alerta como suprimido por um tempo. Usa daemon=True para
        não impedir o processo de encerrar enquanto o timer está pendente."""
        self._suppressed_alerts.add(alert_id)
        timer = threading.Timer(seconds, lambda: self._suppressed_alerts.discard(alert_id))
        timer.daemon = True
        timer.start()

    def _check_condition(self, alert_id: str, condition_met: bool, build_event: Callable[[], AlertEvent]) -> None:
        """Dispara o alerta só na borda de subida (quando a condição passa
        de falsa pra verdadeira) - não repete enquanto ela continuar
        verdadeira. Volta a poder disparar assim que a condição deixar de
        ser verdadeira e for atendida de novo depois."""
        if condition_met:
            if alert_id not in self._active_conditions:
                self._active_conditions.add(alert_id)
                self._emit_alert(build_event())
        else:
            self._active_conditions.discard(alert_id)
    
    def _check_single_process(self, snapshot: ProcessSnapshot) -> None:
        pid = snapshot.pid

        if pid == self._self_pid:
            return  # nunca alerta sobre o próprio processo do ProcWatch

        self._process_history[pid].append(snapshot)
        if len(self._process_history[pid]) > self.HISTORY_SIZE:
            self._process_history[pid].pop(0)
        history = self._process_history[pid]

        # Processos do Windows que rotineiramente têm CPU alta como parte
        # normal de operação (indexação, antivírus, updates...) não entram
        # nas checagens de CPU - continuam valendo para memória/zumbi/etc.
        ignore_cpu = snapshot.name.lower() in IGNORED_CPU_ALERT_PROCESS_NAMES

        # CPU Crítica
        self._check_condition(
            f"cpu_critical_{pid}",
            (not ignore_cpu) and snapshot.cpu_percent >= self.thresholds['cpu_critical'],
            lambda: AlertEvent(
                title=f"CPU Crítica - {snapshot.name}",
                message=f"Processo {snapshot.name} (PID {pid}) usando {snapshot.cpu_percent:.1f}% de CPU",
                severity=AlertSeverity.CRITICAL,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name,
                extra_data={'cpu_percent': snapshot.cpu_percent}
            )
        )

        # CPU Sustentada Alta
        sustained_samples = int(self.thresholds['sustained_high_cpu_secs'])
        avg_cpu = 0.0
        is_sustained = False
        if not ignore_cpu and len(history) >= sustained_samples:
            recent_cpu = [s.cpu_percent for s in history[-sustained_samples:]]
            avg_cpu = sum(recent_cpu) / len(recent_cpu)
            is_sustained = self.thresholds['cpu_warning'] <= avg_cpu < self.thresholds['cpu_critical']

        self._check_condition(
            f"cpu_sustained_{pid}",
            is_sustained,
            lambda: AlertEvent(
                title=f"CPU Elevada Sustentada - {snapshot.name}",
                message=f"{snapshot.name} (PID {pid}) com CPU média de {avg_cpu:.1f}% nas últimas {sustained_samples} amostras",
                severity=AlertSeverity.WARNING,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name,
                extra_data={'avg_cpu': avg_cpu}
            )
        )

        # Memória Crítica / Aviso (avaliadas sempre, não como if/elif - senão
        # a condição não avaliada fica "presa" ativa em _active_conditions)
        is_mem_critical = snapshot.memory_percent >= self.thresholds['memory_critical']
        is_mem_warning = (not is_mem_critical) and snapshot.memory_percent >= self.thresholds['memory_warning']

        self._check_condition(
            f"mem_critical_{pid}",
            is_mem_critical,
            lambda: AlertEvent(
                title=f"Memória Crítica - {snapshot.name}",
                message=f"{snapshot.name} (PID {pid}) usando {snapshot.memory_mb:.1f}MB ({snapshot.memory_percent:.1f}% da RAM total)",
                severity=AlertSeverity.CRITICAL,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name,
                extra_data={'memory_mb': snapshot.memory_mb, 'memory_percent': snapshot.memory_percent}
            )
        )
        self._check_condition(
            f"mem_warning_{pid}",
            is_mem_warning,
            lambda: AlertEvent(
                title=f"Memória Elevada - {snapshot.name}",
                message=f"{snapshot.name} (PID {pid}) usando {snapshot.memory_mb:.1f}MB ({snapshot.memory_percent:.1f}% da RAM)",
                severity=AlertSeverity.WARNING,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name,
                extra_data={'memory_mb': snapshot.memory_mb, 'memory_percent': snapshot.memory_percent}
            )
        )

        self._check_memory_trend(snapshot, history)

        # Processo Zumbi
        self._check_condition(
            f"zombie_{pid}",
            snapshot.status == 'zombie',
            lambda: AlertEvent(
                title=f"Processo Zumbi - {snapshot.name}",
                message=f"{snapshot.name} (PID {pid}) está em estado zumbi (possível vazamento de recursos)",
                severity=AlertSeverity.ERROR,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name
            )
        )

        # Processo com nome de sistema rodando de local inesperado -
        # sinal comum de malware disfarçado (ex.: "svchost.exe" fora de
        # System32). Sempre CRITICAL, independente de CPU/memória.
        self._check_condition(
            f"suspicious_path_{pid}",
            snapshot.is_suspicious_path,
            lambda: AlertEvent(
                title=f"Processo Suspeito - {snapshot.name}",
                message=(
                    f"{snapshot.name} (PID {pid}) tem nome de processo do sistema "
                    f"mas está rodando de {snapshot.exe_path or 'um local desconhecido'} "
                    f"- possível disfarce de malware."
                ),
                severity=AlertSeverity.CRITICAL,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=snapshot.name,
                extra_data={'exe_path': snapshot.exe_path}
            )
        )

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
        """Erros críticos de verdade (fatal, crash, panic, corrupção de
        memória...) sempre alertam. Palavras-chave genéricas de aviso
        ('error', 'failed', 'timeout'...) pegam texto demais que não afeta
        o funcionamento de nada - por isso não alertam mais por padrão,
        só as que o usuário escolheu explicitamente adicionar."""
        message_lower = message.lower()
        with self._lock:
            critical_keywords = self.CRITICAL_ERROR_KEYWORDS + self._custom_critical_keywords
            warning_keywords = list(self._custom_warning_keywords)

        for keyword in critical_keywords:
            if keyword in message_lower:
                alert_id = f"critical_log_{hash(message) % 10000}"
                if alert_id not in self._suppressed_alerts:
                    self._emit_alert(
                        AlertEvent(
                            title="Erro Crítico Detectado",
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
                            title="Aviso de Erro",
                            message=f"{source.value}: {message[:100]}...",
                            severity=AlertSeverity.WARNING,
                            source=source,
                            extra_data={'log_message': message}
                        )
                    )
                    self._suppress_for(alert_id, 300)

    def check_process_exit(self, name: str, pid: int, exit_code: int) -> None:
        """Processo lançado/monitorado pelo ProcWatch encerrou. Ao contrário
        de check_log_entry, é um evento estruturado (não depende de
        casamento de palavra-chave em texto livre), então continua
        alertando mesmo com as palavras-chave genéricas de aviso
        desativadas - um processo que você está de fato acompanhando
        crashar é exatamente o tipo de coisa que afeta o funcionamento."""
        if exit_code == 0:
            return

        alert_id = f"process_exit_{pid}_{exit_code}"
        if alert_id in self._suppressed_alerts:
            return

        self._emit_alert(
            AlertEvent(
                title=f"Processo Encerrado com Erro - {name}",
                message=f"{name} (PID {pid}) encerrou com código de saída {exit_code}",
                severity=AlertSeverity.WARNING,
                source=AlertSource.PROCESS,
                process_pid=pid,
                process_name=name,
                extra_data={'exit_code': exit_code}
            )
        )
        self._suppress_for(alert_id, 60)

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
            # Também libera as condições ativas: se o usuário limpou os
            # alertas manualmente, uma condição que continuar verdadeira
            # deve poder notificar de novo, em vez de ficar presa como "já
            # avisado" para sempre.
            self._active_conditions.clear()
