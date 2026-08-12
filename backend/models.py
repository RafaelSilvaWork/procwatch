"""Modelos de dados para LogWatch."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class AlertSeverity(Enum):
    """Níveis de severidade de alerta."""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AlertSource(Enum):
    """Origem do alerta."""
    PROCESS = "Processo"
    SYSTEM = "Sistema"
    APP_LOG = "Log da App"


@dataclass
class ProcessSnapshot:
    """Snapshot de um processo em um ponto no tempo."""
    pid: int
    name: str
    cpu_percent: float
    memory_mb: float
    memory_percent: float
    io_read_mb: float
    io_write_mb: float
    status: str
    timestamp: datetime = field(default_factory=datetime.now)
    num_threads: int = 0
    
    def __post_init__(self):
        if self.cpu_percent < 0 or self.cpu_percent > 100:
            self.cpu_percent = max(0, min(100, self.cpu_percent))
        if self.memory_percent < 0 or self.memory_percent > 100:
            self.memory_percent = max(0, min(100, self.memory_percent))


@dataclass
class AlertEvent:
    """Evento de alerta gerado pelo sistema."""
    title: str
    message: str
    severity: AlertSeverity
    source: AlertSource
    timestamp: datetime = field(default_factory=datetime.now)
    process_pid: Optional[int] = None
    process_name: Optional[str] = None
    extra_data: dict = field(default_factory=dict)
    
    def __str__(self) -> str:
        source_str = f" [{self.source.value}]" if self.source else ""
        return f"[{self.timestamp.strftime('%H:%M:%S')}] [{self.severity.value}]{source_str} {self.title}: {self.message}"


@dataclass
class OSLogEvent:
    """Evento do log do sistema operacional."""
    timestamp: datetime
    source: str
    event_type: str
    event_id: int
    message: str
    computer: str = "localhost"


@dataclass
class AppLogEntry:
    """Entrada de log da aplicação."""
    timestamp: datetime
    level: str
    message: str
    source_file: Optional[str] = None
    line_number: Optional[int] = None
