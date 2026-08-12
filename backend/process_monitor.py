"""Monitoramento de processos em tempo real - OTIMIZADO."""

import logging
import psutil
import threading
import time
from typing import Callable, Dict, List, Optional, Set
from datetime import datetime
from .models import ProcessSnapshot, SystemStats
from .security_check import SYSTEM_PROCESS_NAMES, is_suspicious_path
from .window_utils import get_pids_with_visible_window

logger = logging.getLogger(__name__)


def get_system_stats() -> SystemStats:
    """Uso total do sistema (não por processo)."""
    vm = psutil.virtual_memory()
    return SystemStats(
        cpu_percent=psutil.cpu_percent(interval=None),
        memory_percent=vm.percent,
        memory_used_gb=vm.used / (1024 ** 3),
        memory_total_gb=vm.total / (1024 ** 3),
    )


def snapshot_from_pid(pid: int) -> Optional[ProcessSnapshot]:
    """Constrói um ProcessSnapshot para um PID específico imediatamente,
    sem esperar o próximo ciclo periódico do ProcessMonitor. Útil logo
    após lançar um processo novo."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            memory_info = proc.memory_info()
            try:
                exe_path = proc.exe()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                exe_path = ""
            return ProcessSnapshot(
                pid=pid,
                name=proc.name(),
                cpu_percent=proc.cpu_percent(interval=None),
                memory_mb=memory_info.rss / (1024 * 1024),
                memory_percent=proc.memory_percent(),
                io_read_mb=0.0,
                io_write_mb=0.0,
                status=proc.status(),
                num_threads=proc.num_threads(),
                exe_path=exe_path,
                create_time=proc.create_time(),
                is_suspicious_path=is_suspicious_path(proc.name(), exe_path),
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


class ProcessMonitor:
    """Monitor de processos com atualização a cada 1 segundo (OTIMIZADO)."""

    # psutil.cpu_percent(interval=None) divide o tempo de CPU consumido pelo
    # tempo de parede desde a última amostra do mesmo processo. Se duas
    # coletas acontecerem quase juntas (ex.: usuário clica "Atualizar Lista"
    # logo após um ciclo natural), esse denominador fica perto de zero e
    # qualquer atividade normal de CPU produz uma razão inflada - que o
    # ProcessSnapshot então arredonda para exatamente 100.0%, gerando
    # falsos alertas de "CPU crítica" em processos que não têm nada de
    # errado. Por isso nunca deixamos duas coletas reais acontecerem mais
    # perto que MIN_COLLECT_INTERVAL, mesmo com request_refresh() em rajada.
    MIN_COLLECT_INTERVAL = 1.0

    # Quais janelas estão abertas muda bem menos vezes que CPU/memória -
    # não vale a pena enumerar todas as janelas do Windows a cada coleta.
    WINDOW_SCAN_EVERY_N_CYCLES = 3

    def __init__(self, update_interval: float = 1.0, max_processes: int = 100):
        """
        Args:
            update_interval: Intervalo de coleta em segundos
            max_processes: Máximo de processos para coletar (reduz carga)
        """
        self.update_interval = update_interval
        self.max_processes = max_processes
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._callback: Optional[Callable[[List[ProcessSnapshot]], None]] = None
        self._lock = threading.Lock()
        self._cached_snapshots: List[ProcessSnapshot] = []
        self._wake_event = threading.Event()
        self._pinned_pids: Set[int] = set()
        self._window_pids_cache: Set[int] = set()
        self._collect_count = 0

    def request_refresh(self) -> None:
        """Interrompe a espera do loop e força uma nova coleta imediatamente."""
        self._wake_event.set()

    def pin_pid(self, pid: int) -> None:
        """Garante que este PID nunca seja cortado do corte TOP N por CPU -
        para os processos que o usuário está monitorando explicitamente não
        sumirem silenciosamente da coleta se a CPU deles cair (e outros
        200+ processos tiverem CPU maior naquele instante)."""
        with self._lock:
            self._pinned_pids.add(pid)

    def unpin_pid(self, pid: int) -> None:
        with self._lock:
            self._pinned_pids.discard(pid)

    def start(self, on_update: Callable[[List[ProcessSnapshot]], None]) -> None:
        if self._running:
            return
        
        self._running = True
        self._callback = on_update
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
    
    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
    
    def _monitor_loop(self) -> None:
        """Loop principal de monitoramento (executa em thread separada)."""
        last_collect = 0.0
        while self._running:
            elapsed = time.monotonic() - last_collect
            if elapsed < self.MIN_COLLECT_INTERVAL:
                time.sleep(self.MIN_COLLECT_INTERVAL - elapsed)

            try:
                snapshots = self._collect_all_processes()
                last_collect = time.monotonic()

                with self._lock:
                    self._cached_snapshots = snapshots

                if self._callback:
                    self._callback(snapshots)
            except Exception:
                logger.exception("Erro ao coletar processos")

            self._wake_event.wait(self.update_interval)
            self._wake_event.clear()
    
    def _collect_all_processes(self) -> List[ProcessSnapshot]:
        """
        Coleta dados dos processos de forma otimizada.
        - Pega apenas TOP N processos (padrão 100)
        - Remove operações pesadas como io_counters()
        """
        snapshots: List[ProcessSnapshot] = []
        
        try:
            # Coletar tudo em uma única varredura (evita reabrir cada processo depois)
            basic_data = []
            for proc in psutil.process_iter(
                ['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info',
                 'status', 'num_threads', 'create_time']
            ):
                try:
                    info = proc.info
                    # No Windows, o "System Idle Process" (PID 0) reporta
                    # cpu_percent como % de tempo OCIOSO, não de uso real -
                    # ele fica sempre no topo da lista (100% quando a
                    # máquina está tranquila) e não é um processo de verdade.
                    if proc.pid == 0 or (info.get('name') or "").lower() == "system idle process":
                        continue
                    basic_data.append((proc.pid, info))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            # Verifica se algum processo com nome de sistema (svchost.exe,
            # explorer.exe, ...) está rodando de fora de System32/SysWOW64 -
            # sinal comum de malware disfarçado. Só chama .exe() (mais cara)
            # para esse subconjunto pequeno, não para todos os processos.
            suspicious_pids: Set[int] = set()
            for pid, info in basic_data:
                name = (info.get('name') or "").lower()
                if name not in SYSTEM_PROCESS_NAMES:
                    continue
                try:
                    exe_path = psutil.Process(pid).exe()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    exe_path = ""
                info['exe'] = exe_path
                if is_suspicious_path(info.get('name') or "", exe_path):
                    suspicious_pids.add(pid)

            # Ordenar por CPU e pegar apenas TOP N
            basic_data.sort(
                key=lambda x: x[1].get('cpu_percent') or 0.0,
                reverse=True
            )
            top_data = basic_data[:self.max_processes]

            with self._lock:
                pinned_pids = set(self._pinned_pids)

            # PIDs fixados e processos suspeitos nunca podem ser cortados do
            # TOP N - um processo disfarçado com CPU baixa não pode
            # simplesmente sumir da coleta sem ser detectado.
            present_pids = {pid for pid, _ in top_data}
            missing_pids = (pinned_pids | suspicious_pids) - present_pids
            if missing_pids:
                extra = [entry for entry in basic_data if entry[0] in missing_pids]
                top_data = top_data + extra

            basic_data = top_data

            if self._collect_count % self.WINDOW_SCAN_EVERY_N_CYCLES == 0:
                self._window_pids_cache = get_pids_with_visible_window()
            self._collect_count += 1
            window_pids = self._window_pids_cache

            for pid, info in basic_data:
                memory_info = info.get('memory_info')
                memory_mb = memory_info.rss / (1024 * 1024) if memory_info else 0.0
                name = info.get('name') or "unknown"
                exe_path = info.get('exe') or ""

                snapshot = ProcessSnapshot(
                    pid=pid,
                    name=name,
                    cpu_percent=info.get('cpu_percent') or 0.0,
                    memory_mb=memory_mb,
                    memory_percent=info.get('memory_percent') or 0.0,
                    io_read_mb=0.0,
                    io_write_mb=0.0,
                    status=info.get('status') or "unknown",
                    num_threads=info.get('num_threads') or 0,
                    has_window=pid in window_pids,
                    exe_path=exe_path,
                    create_time=info.get('create_time') or 0.0,
                    is_suspicious_path=pid in suspicious_pids,
                )
                snapshots.append(snapshot)
        
        except Exception:
            logger.exception("Erro na coleta de processos")
        
        return snapshots
    
    def get_process_by_name(self, name: str) -> List[ProcessSnapshot]:
        """Busca processos pelo nome."""
        name_lower = name.lower()
        matching = []
        
        with self._lock:
            for snapshot in self._cached_snapshots:
                if snapshot.name.lower() == name_lower:
                    matching.append(snapshot)
        
        return matching
    
    def get_top_processes(self, by: str = 'cpu', limit: int = 10) -> List[ProcessSnapshot]:
        """Retorna os principais processos ordenados por recurso."""
        with self._lock:
            snapshots = list(self._cached_snapshots)
        
        if by == 'cpu':
            sorted_procs = sorted(snapshots, key=lambda s: s.cpu_percent, reverse=True)
        elif by == 'memory':
            sorted_procs = sorted(snapshots, key=lambda s: s.memory_mb, reverse=True)
        elif by == 'io_write':
            sorted_procs = sorted(snapshots, key=lambda s: s.io_write_mb, reverse=True)
        elif by == 'io_read':
            sorted_procs = sorted(snapshots, key=lambda s: s.io_read_mb, reverse=True)
        else:
            sorted_procs = snapshots
        
        return sorted_procs[:limit]
    
    def get_all_processes(self) -> List[ProcessSnapshot]:
        """Retorna cache de processos (sem refrescar)."""
        with self._lock:
            return list(self._cached_snapshots)
