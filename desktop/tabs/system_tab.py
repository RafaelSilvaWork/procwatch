"""Aba "Sistema": visão passiva do uso total de CPU/memória da máquina
(não de um processo específico)."""

from PyQt6.QtWidgets import QLabel, QVBoxLayout

from backend.models import SystemStats
from desktop.history_chart import HistoryChartWidget
from desktop.tabs.constants import _HISTORY_MAX_POINTS
from desktop.theme import COLOR_ACCENT, COLOR_TEXT_BRIGHT


class SystemTabMixin:
    """Depende de atributos definidos em ProcWatchMainWindow: self.tab_system."""

    def setup_tab_system(self):
        """Aba de visão geral: uso total de CPU/memória da máquina, não de
        um processo específico."""
        layout = QVBoxLayout(self.tab_system)

        info = QLabel("💻 Uso total do sistema (CPU e memória da máquina, não por processo)")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        self.system_cpu_label = QLabel("CPU: —")
        self.system_cpu_label.setStyleSheet(f"color: {COLOR_TEXT_BRIGHT}; font-size: 13pt; font-weight: bold;")
        layout.addWidget(self.system_cpu_label)

        self.system_memory_label = QLabel("Memória: —")
        self.system_memory_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 13pt; font-weight: bold;")
        layout.addWidget(self.system_memory_label)

        layout.addSpacing(12)
        layout.addWidget(QLabel("Histórico (CPU % / Memória %):"))
        self.system_history_chart = HistoryChartWidget(max_points=_HISTORY_MAX_POINTS)
        # stretch=1: sem isso, o gráfico fica com sua altura mínima (140px)
        # colado no topo e sobra um vão vazio grande até o fim da aba - o
        # único widget desta aba com motivo real para crescer é o gráfico.
        layout.addWidget(self.system_history_chart, 1)

    def on_system_stats_updated(self, stats: SystemStats):
        """Callback com o uso total de CPU/memória da máquina."""
        self.system_cpu_label.setText(f"CPU: {stats.cpu_percent:.1f}%")
        self.system_memory_label.setText(
            f"Memória: {stats.memory_used_gb:.1f} GB / {stats.memory_total_gb:.1f} GB "
            f"({stats.memory_percent:.1f}%)"
        )
        self.system_history_chart.add_point(stats.cpu_percent, stats.memory_percent)
