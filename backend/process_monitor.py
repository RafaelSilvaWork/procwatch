"""Monitoramento de processos em tempo real - OTIMIZADO."""

import logging
import psutil
import threading
import time
from typing import Callable, Dict, List, Optional
from datetime import datetime
from .models import ProcessSnapshot

logger = logging.getLogger(__name__)


def snapshot_from_pid(pid: int) -> Optional[ProcessSnapshot]:
    """Constrói um ProcessSnapshot para um PID específico imediatamente,
    sem esperar o próximo ciclo periódico do ProcessMonitor. Útil logo
    após lançar um processo novo."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            memory_info = proc.memory_info()
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
            )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


class ProcessMonitor:
    """Monitor de processos com atualização a cada 1 segundo (OTIMIZADO)."""
    
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

    def request_refresh(self) -> None:
        """Interrompe a espera do loop e força uma nova coleta imediatamente."""
        self._wake_event.set()

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
        while self._running:
            try:
                snapshots = self._collect_all_processes()
                
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
                 'status', 'num_threads']
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

            # Ordenar por CPU e pegar apenas TOP N
            basic_data.sort(
                key=lambda x: x[1].get('cpu_percent') or 0.0,
                reverse=True
            )
            basic_data = basic_data[:self.max_processes]

            for pid, info in basic_data:
                memory_info = info.get('memory_info')
                memory_mb = memory_info.rss / (1024 * 1024) if memory_info else 0.0

                snapshot = ProcessSnapshot(
                    pid=pid,
                    name=info.get('name') or "unknown",
                    cpu_percent=info.get('cpu_percent') or 0.0,
                    memory_mb=memory_mb,
                    memory_percent=info.get('memory_percent') or 0.0,
                    io_read_mb=0.0,
                    io_write_mb=0.0,
                    status=info.get('status') or "unknown",
                    num_threads=info.get('num_threads') or 0
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
