import csv
import logging
import sys
import os
import threading
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psutil
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QTextEdit, QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QSpinBox, QDialog, QFileDialog, QMessageBox,
    QListWidget, QListWidgetItem, QSystemTrayIcon, QMenu, QStyle, QLineEdit,
    QButtonGroup, QCheckBox
)
from PyQt6.QtCore import QThread, QObject, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont

from backend.app import ProcWatchApp
from backend.logging_config import setup_logging
from backend.models import ProcessSnapshot, AlertEvent, AlertSeverity, AlertSource, SystemStats
from backend.process_monitor import ProcessMonitor, get_system_stats, snapshot_from_pid
from backend.alert_engine import AlertEngine
from desktop.alert_settings_dialog import AlertSettingsDialog
from desktop.app_settings import (
    load_custom_keywords, load_thresholds, load_tray_notice_dismissed, load_window_geometry,
    save_custom_keywords, save_thresholds, save_tray_notice_dismissed, save_window_geometry,
)
from desktop.history_chart import HistoryChartWidget
from desktop.process_list_dialog import ProcessListDialog
from desktop.theme import ALERT_COLORS, COLOR_ACCENT, COLOR_TEXT_BRIGHT, STYLESHEET

logger = logging.getLogger(__name__)

_NUMERIC_COLUMNS = {0, 2, 3, 4}  # PID, CPU %, Memória (MB), Memória %
_HISTORY_MAX_POINTS = 150

# Paleta de identificação de processo no log combinado - reaproveita tokens
# já existentes (nenhuma cor nova). Usada só como identificador visual por
# processo (rotação por ordem de adição), não como indicador de severidade -
# é o mesmo padrão de "uma cor por stream" usado por viewers de log
# multi-fonte (ex.: docker compose logs).
_LOG_PREFIX_PALETTE = [
    COLOR_TEXT_BRIGHT, COLOR_ACCENT, ALERT_COLORS["INFO"],
    ALERT_COLORS["ERROR"], ALERT_COLORS["CRITICAL"],
]


class NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem que ordena numericamente em vez de como texto
    (senão "10.0" viria antes de "9.0" na ordenação por clique no cabeçalho)."""

    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


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


class ProcWatchMainWindow(QMainWindow):
    """Janela principal do ProcWatch - monitora múltiplos processos ao
    mesmo tempo (CPU/memória/histórico/logs/alertas)."""

    log_line_received = pyqtSignal(int, str, str)  # (pid, caminho/rótulo, linha)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProcWatch | Monitor de Processos e Logs")

        # Monitor roda a cada 2s (menos pesado)
        self.process_monitor_thread = ProcessMonitorThread(interval=2.0, max_processes=200)

        # Worker de alertas rodando em thread própria
        self.alert_thread = QThread()
        self.alert_worker = AlertWorker()
        self.alert_worker.moveToThread(self.alert_thread)

        saved_thresholds = load_thresholds()
        if saved_thresholds:
            self.alert_worker.engine.update_thresholds(saved_thresholds)
            logger.info("Thresholds de alerta carregados de procwatch.ini: %s", saved_thresholds)

        saved_critical_kw, saved_warning_kw = load_custom_keywords()
        if saved_critical_kw or saved_warning_kw:
            self.alert_worker.engine.set_custom_keywords(saved_critical_kw, saved_warning_kw)
            logger.info("Palavras-chave customizadas carregadas: crit=%s aviso=%s", saved_critical_kw, saved_warning_kw)

        # Monitoramento dos arquivos de log/stdout dos processos monitorados
        self.log_watch_app = ProcWatchApp()

        # Processos monitorados (vários ao mesmo tempo)
        self.monitored_pids: List[int] = []
        self.active_pid: Optional[int] = None
        self.active_process: Optional[ProcessSnapshot] = None
        self.process_history: Dict[int, Deque[Tuple[float, float]]] = {}
        self._monitored_names: Dict[int, str] = {}
        self._pid_log_colors: Dict[int, str] = {}

        self.all_processes: List[ProcessSnapshot] = []
        self.displayed_processes: List[ProcessSnapshot] = []
        self._pid_to_process: dict = {}
        self._alert_counts = {"CRITICAL": 0, "ERROR": 0, "WARNING": 0, "INFO": 0}
        self._table_filters: dict = {}
        self._closing = False

        self.setup_ui()

        # Precisa rodar DEPOIS do setup_ui(): setar a geometria antes dos
        # widgets existirem faz o QMainWindowLayout sobrescrever o tamanho
        # pedido pelo hint de tamanho do conteúdo assim que as abas/tabelas
        # são adicionadas (a janela acabava ocupando quase a tela inteira).
        saved_geometry = load_window_geometry()
        if saved_geometry:
            self.restoreGeometry(saved_geometry)
            self._clamp_geometry_to_screen()
        else:
            self._apply_default_geometry()

        self.process_monitor_thread.processes_updated.connect(self.on_processes_updated)
        # Entrega os snapshots direto para a thread do worker (conexão em fila,
        # já que o worker mora em outra thread) - não passa pela UI.
        self.process_monitor_thread.processes_updated.connect(self.alert_worker.check_processes)
        self.process_monitor_thread.system_stats_updated.connect(self.on_system_stats_updated)
        self.alert_worker.alert_triggered.connect(self.on_alert_triggered)
        self.log_line_received.connect(self._append_filtered_log)

        self.process_monitor_thread.start()
        self.alert_thread.start()

        # Rechecar periodicamente se os processos monitorados abriram
        # arquivos de log novos (ex.: rotação diária), sem reiniciar o
        # tail dos que já estão sendo acompanhados.
        self.log_rescan_timer = QTimer(self)
        self.log_rescan_timer.timeout.connect(self._rescan_process_logs)
        self.log_rescan_timer.start(5_000)

    def _apply_default_geometry(self):
        """Tamanho proporcional à tela atual (75%), centralizado - em vez de
        um tamanho fixo que pode ficar grande demais ou pequeno demais
        dependendo da resolução do monitor."""
        screen = QApplication.primaryScreen()
        if screen is None:
            self.setGeometry(100, 100, 1400, 800)
            return

        available = screen.availableGeometry()
        width = int(available.width() * 0.75)
        height = int(available.height() * 0.75)
        x = available.x() + (available.width() - width) // 2
        y = available.y() + (available.height() - height) // 2
        self.setGeometry(x, y, width, height)

    def _clamp_geometry_to_screen(self):
        """Se a geometria salva de uma sessão anterior não couber mais na
        tela atual (monitor trocado, resolução diferente) ou estiver
        colada em quase toda a área disponível, volta para o tamanho
        proporcional padrão em vez de deixar a janela cobrindo a tela
        inteira ou ficar fora dela.

        O Windows já recorta sozinho uma geometria absurdamente grande
        (ex.: 3x o tamanho da tela) para caber na tela - por isso não basta
        checar se ela "excede" a área disponível, pois nesse ponto ela já
        foi cortada para pouco menos que 100% da tela, e ainda assim não é
        o tamanho proporcional que queremos."""
        screen = QApplication.primaryScreen()
        if screen is None:
            return

        available = screen.availableGeometry()
        frame = self.frameGeometry()
        too_big = (frame.width() >= available.width() * 0.95
                   or frame.height() >= available.height() * 0.95)
        off_screen = not available.intersects(frame)
        if too_big or off_screen:
            self._apply_default_geometry()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)

        # ─── HEADER ───
        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("📋 Processos monitorados:"))

        self.monitored_summary_label = QLabel("Nenhum")
        self.monitored_summary_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold;")
        header_layout.addWidget(self.monitored_summary_label)

        # Ações primárias (escolher o que monitorar) agrupadas à esquerda;
        # espaçamento extra separa da ação utilitária (atualizar), que não
        # tem o mesmo peso de decisão - evita que as três leiam como um
        # bloco único de importância igual.
        select_btn = QPushButton("🎯 Selecionar Processo...")
        select_btn.clicked.connect(self.open_process_list_dialog)
        header_layout.addWidget(select_btn)

        launch_btn = QPushButton("▶️ Abrir e Monitorar...")
        launch_btn.clicked.connect(self.open_and_monitor_process)
        header_layout.addWidget(launch_btn)

        header_layout.addSpacing(24)

        refresh_btn = QPushButton("🔄 Atualizar Lista")
        refresh_btn.clicked.connect(self.request_process_refresh)
        header_layout.addWidget(refresh_btn)

        header_layout.addStretch()
        main_layout.addLayout(header_layout)

        # ─── TABS ───
        # Ordem por frequência real de uso (não alfabética nem de
        # implementação): escolher processo -> acompanhar -> ler log ->
        # reagir a alerta. "Sistema" é visão passiva, fica por último.
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Aba 0: Processos (Aplicativos/Todos combinados com um alternador,
        # em vez de duas abas para a mesma tabela com um filtro diferente)
        self.tab_processes = QWidget()
        self.setup_tab_processes()
        self.tabs.addTab(self.tab_processes, "📊 Processos")

        # Aba 1: Processos monitorados
        self.tab_selected_process = QWidget()
        self.setup_tab_selected_process()
        self.tabs.addTab(self.tab_selected_process, "🎯 Monitorados")

        # Aba 2: Logs
        self.tab_filtered_logs = QWidget()
        self.setup_tab_filtered_logs()
        self.tabs.addTab(self.tab_filtered_logs, "📝 Logs")

        # Aba 3: Alertas
        self.tab_alerts = QWidget()
        self.setup_tab_alerts()
        self.tabs.addTab(self.tab_alerts, "🚨 Alertas")

        # Aba 4: Visão geral do sistema (CPU/memória totais da máquina)
        self.tab_system = QWidget()
        self.setup_tab_system()
        self.tabs.addTab(self.tab_system, "💻 Sistema")

        # ─── BARRA DE STATUS ───
        self.statusBar().showMessage("Iniciando monitoramento...")

        self._setup_tray_icon()

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

        layout.addWidget(QLabel("Histórico (CPU % / Memória %):"))
        self.system_history_chart = HistoryChartWidget(max_points=_HISTORY_MAX_POINTS)
        layout.addWidget(self.system_history_chart)

    def _build_process_table(self) -> QTableWidget:
        """Cria uma QTableWidget no formato usado pelas abas de processos."""
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["PID", "Nome", "CPU %", "Memória (MB)", "Memória %"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSortingEnabled(True)
        table.itemClicked.connect(self.on_process_clicked)
        return table

    def _build_filter_input(self, table: QTableWidget) -> QLineEdit:
        """Campo de busca por nome para uma tabela de processos."""
        filter_input = QLineEdit()
        filter_input.setPlaceholderText("🔎 Filtrar por nome...")
        filter_input.textChanged.connect(lambda text, t=table: self._apply_table_filter(t, text))
        return filter_input

    def setup_tab_processes(self):
        """Aba única de processos, com alternador "Aplicativos / Todos" em
        vez de duas abas separadas para a mesma tabela com um filtro
        diferente - a distinção de escopo vira uma escolha dentro do
        contexto, não uma decisão de navegação (Lei de Hick)."""
        layout = QVBoxLayout(self.tab_processes)

        scope_row = QHBoxLayout()
        scope_row.addWidget(QLabel("Mostrar:"))

        self.scope_apps_btn = QPushButton("🖥️ Aplicativos")
        self.scope_apps_btn.setCheckable(True)
        self.scope_apps_btn.setChecked(True)
        self.scope_apps_btn.setToolTip("Só processos com janela visível")

        self.scope_all_btn = QPushButton("📌 Todos")
        self.scope_all_btn.setCheckable(True)
        self.scope_all_btn.setToolTip("Todos os processos, incluindo tarefas em segundo plano")

        # Reaproveita a cor de destaque já existente para marcar qual dos
        # dois está ativo - sem essa regra, um QPushButton "checked" não
        # tem nenhuma distinção visual no tema atual.
        segmented_style = f"""
            QPushButton:checkable {{ background-color: #2a2a2a; }}
            QPushButton:checkable:checked {{
                background-color: #333333;
                border: 1px solid {COLOR_ACCENT};
                color: {COLOR_ACCENT};
            }}
        """
        self.scope_apps_btn.setStyleSheet(segmented_style)
        self.scope_all_btn.setStyleSheet(segmented_style)

        self.process_scope_group = QButtonGroup(self)
        self.process_scope_group.setExclusive(True)
        self.process_scope_group.addButton(self.scope_apps_btn)
        self.process_scope_group.addButton(self.scope_all_btn)
        self.process_scope_group.buttonClicked.connect(lambda _btn: self.refresh_process_list())

        scope_row.addWidget(self.scope_apps_btn)
        scope_row.addWidget(self.scope_all_btn)
        scope_row.addStretch()
        layout.addLayout(scope_row)

        info = QLabel("Clique num processo para monitorá-lo")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        self.table_processes = self._build_process_table()
        self.processes_filter_input = self._build_filter_input(self.table_processes)
        layout.addWidget(self.processes_filter_input)
        layout.addWidget(self.table_processes)

    def setup_tab_selected_process(self):
        """Aba com a lista de processos monitorados simultaneamente e os
        detalhes + histórico do que está ativo (selecionado na lista)."""
        outer_layout = QVBoxLayout(self.tab_selected_process)
        split_layout = QHBoxLayout()

        # ─── Lista de monitorados (esquerda) ───
        list_widget_container = QWidget()
        list_widget_container.setMaximumWidth(260)
        list_layout = QVBoxLayout(list_widget_container)
        list_layout.setContentsMargins(0, 0, 0, 0)

        list_layout.addWidget(QLabel("Processos monitorados:"))
        self.monitored_list_widget = QListWidget()
        self.monitored_list_widget.currentItemChanged.connect(self._on_monitored_selection_changed)
        # Affordance explícita: sem isso, nada sinaliza que clicar num item
        # troca o painel de detalhes e o gráfico à direita. Borda de
        # destaque reaproveita COLOR_ACCENT (já existente); os cinzas são
        # os mesmos já usados no hover padrão de QPushButton do tema.
        self.monitored_list_widget.setStyleSheet(f"""
            QListWidget::item {{ padding: 6px; border: 1px solid transparent; }}
            QListWidget::item:hover {{ background-color: #333333; }}
            QListWidget::item:selected {{
                background-color: #333333;
                border: 2px solid {COLOR_ACCENT};
            }}
        """)
        self.monitored_list_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.monitored_list_widget.setToolTip("Clique para ver os detalhes deste processo")
        list_layout.addWidget(self.monitored_list_widget)

        self.stop_monitor_btn = QPushButton("🚫 Parar de Monitorar")
        self.stop_monitor_btn.clicked.connect(self.stop_monitoring_active_process)
        self.stop_monitor_btn.setEnabled(False)
        list_layout.addWidget(self.stop_monitor_btn)

        split_layout.addWidget(list_widget_container)

        # ─── Detalhes do processo ativo (direita) ───
        details_container = QWidget()
        details_layout = QVBoxLayout(details_container)
        details_layout.setContentsMargins(0, 0, 0, 0)

        self.selected_process_label = QLabel("Nenhum processo monitorado ainda")
        self.selected_process_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 14pt; font-weight: bold;")
        details_layout.addWidget(self.selected_process_label)

        # Estado vazio como convite à ação, não um beco sem saída - some
        # assim que o primeiro processo é monitorado.
        self.empty_state_hint_btn = QPushButton("🎯 Selecionar Processo...")
        self.empty_state_hint_btn.clicked.connect(self.open_process_list_dialog)
        details_layout.addWidget(self.empty_state_hint_btn)

        self.selected_process_details = QTextEdit()
        self.selected_process_details.setReadOnly(True)
        self.selected_process_details.setStyleSheet("font-size: 11pt;")
        details_layout.addWidget(self.selected_process_details)

        details_layout.addWidget(QLabel("Histórico (CPU % / Memória %):"))
        self.history_chart = HistoryChartWidget(max_points=_HISTORY_MAX_POINTS)
        details_layout.addWidget(self.history_chart)

        self.terminate_btn = QPushButton("🛑 Finalizar Processo")
        self.terminate_btn.clicked.connect(self.terminate_active_process)
        self.terminate_btn.setEnabled(False)
        details_layout.addWidget(self.terminate_btn)

        split_layout.addWidget(details_container)
        outer_layout.addLayout(split_layout)

    def setup_tab_filtered_logs(self):
        """Aba com os logs (em tempo real) de todos os processos monitorados,
        combinados num único painel, prefixados por processo."""
        layout = QVBoxLayout(self.tab_filtered_logs)

        self.filtered_logs_label = QLabel("Logs do processo (em tempo real)")
        layout.addWidget(self.filtered_logs_label)

        self.filtered_logs_text = QTextEdit()
        self.filtered_logs_text.setReadOnly(True)
        self.filtered_logs_text.setStyleSheet("font-size: 10pt;")
        self.filtered_logs_text.document().setMaximumBlockCount(5000)
        layout.addWidget(self.filtered_logs_text)

        buttons_row = QHBoxLayout()
        clear_btn = QPushButton("🗑️ Limpar Logs")
        clear_btn.clicked.connect(self.filtered_logs_text.clear)
        buttons_row.addWidget(clear_btn)

        export_btn = QPushButton("💾 Exportar Logs...")
        export_btn.clicked.connect(self.export_filtered_logs)
        buttons_row.addWidget(export_btn)
        buttons_row.addStretch()
        layout.addLayout(buttons_row)

    def setup_tab_alerts(self):
        """Aba de alertas."""
        layout = QVBoxLayout(self.tab_alerts)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Filtrar por:"))
        self.severity_filter = QComboBox()
        self.severity_filter.addItems(["Todos", "CRITICAL", "ERROR", "WARNING", "INFO"])
        self.severity_filter.currentTextChanged.connect(self.on_severity_filter_changed)
        controls_layout.addWidget(self.severity_filter)

        settings_btn = QPushButton("⚙️ Configurar Alertas...")
        settings_btn.clicked.connect(self.open_alert_settings_dialog)
        controls_layout.addWidget(settings_btn)

        export_btn = QPushButton("💾 Exportar Alertas...")
        export_btn.clicked.connect(self.export_alerts)
        controls_layout.addWidget(export_btn)

        clear_btn = QPushButton("🗑️ Limpar Alertas")
        clear_btn.clicked.connect(self.clear_alerts)
        controls_layout.addWidget(clear_btn)
        controls_layout.addStretch()

        self.alert_counts_label = QLabel()
        self._update_alert_counts_label()
        controls_layout.addWidget(self.alert_counts_label)
        layout.addLayout(controls_layout)

        self.text_alerts = QTextEdit()
        self.text_alerts.setReadOnly(True)
        self.text_alerts.setObjectName("alertsLog")
        self.text_alerts.document().setMaximumBlockCount(3000)
        layout.addWidget(self.text_alerts)

    # ─── BANDEJA DO SISTEMA ───

    def _setup_tray_icon(self):
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if not self._tray_available:
            logger.warning("Bandeja do sistema não disponível neste ambiente.")
            return

        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        self.tray_icon.setToolTip("ProcWatch")

        tray_menu = QMenu()
        show_action = tray_menu.addAction("Mostrar ProcWatch")
        show_action.triggered.connect(self._restore_from_tray)
        quit_action = tray_menu.addAction("Sair")
        quit_action.triggered.connect(self._quit)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_from_tray()

    def _restore_from_tray(self):
        self.showNormal()
        self.activateWindow()

    def _quit(self):
        self._closing = True
        self.close()

    # ─── SLOTS (funções) ───

    def on_processes_updated(self, snapshots: List[ProcessSnapshot]):
        """Callback quando processos são listados."""
        self.all_processes = snapshots
        self.update_process_list()

    def on_system_stats_updated(self, stats: SystemStats):
        """Callback com o uso total de CPU/memória da máquina."""
        self.system_cpu_label.setText(f"CPU: {stats.cpu_percent:.1f}%")
        self.system_memory_label.setText(
            f"Memória: {stats.memory_used_gb:.1f} GB / {stats.memory_total_gb:.1f} GB "
            f"({stats.memory_percent:.1f}%)"
        )
        self.system_history_chart.add_point(stats.cpu_percent, stats.memory_percent)

    def on_process_clicked(self, item):
        """Quando clica em um processo em qualquer uma das tabelas
        (Aplicativos ou Todos os Processos).

        Busca o processo pelo PID guardado na célula (não pelo índice da
        linha) - a tabela pode estar ordenada por qualquer coluna (clique no
        cabeçalho), então a posição da linha não corresponde mais à ordem de
        self.displayed_processes."""
        table = item.tableWidget()
        pid_item = table.item(item.row(), 0)
        if pid_item is None:
            return
        process = self._pid_to_process.get(pid_item.data(Qt.ItemDataRole.UserRole))
        if process is not None:
            self.add_monitored_process(process)

    def open_process_list_dialog(self):
        """Abre o seletor de processos (estilo Cheat Engine)."""
        dialog = ProcessListDialog(self.all_processes, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_process:
            self.add_monitored_process(dialog.selected_process)

    def request_process_refresh(self):
        """Força uma nova varredura imediata (não só reexibe o último snapshot)."""
        self.process_monitor_thread.request_refresh()
        self.statusBar().showMessage("Atualizando lista de processos...", 2000)

    def _rescan_process_logs(self):
        """Chamado periodicamente: pega arquivos de log novos de TODOS os
        processos monitorados, sem reiniciar o tail dos que já estão sendo
        lidos."""
        for pid in list(self.monitored_pids):
            self.log_watch_app.sync_process_logs(
                pid,
                lambda path, linha, p=pid: self.log_line_received.emit(p, path, linha),
            )

    @staticmethod
    def _format_uptime(create_time: float) -> str:
        if not create_time:
            return "desconhecido"
        elapsed = max(0, datetime.now().timestamp() - create_time)
        days, rest = divmod(int(elapsed), 86400)
        hours, rest = divmod(rest, 3600)
        minutes, seconds = divmod(rest, 60)
        if days:
            return f"{days}d {hours}h {minutes}min"
        if hours:
            return f"{hours}h {minutes}min"
        if minutes:
            return f"{minutes}min {seconds}s"
        return f"{seconds}s"

    @classmethod
    def _process_details_text(cls, process: ProcessSnapshot) -> str:
        aviso_suspeito = ""
        if process.is_suspicious_path:
            aviso_suspeito = (
                "\n🚨 ATENÇÃO: nome de processo do sistema rodando de local"
                " inesperado - possível disfarce de malware!\n"
            )

        exe_linha = f"Executável:        {process.exe_path}\n" if process.exe_path else ""

        return f"""
🔍 INFORMAÇÕES DO PROCESSO SELECIONADO
═══════════════════════════════════════
{aviso_suspeito}
Nome:              {process.name}
PID:               {process.pid}
Status:            {process.status}
Em execução há:    {cls._format_uptime(process.create_time)}
{exe_linha}
💻 RECURSOS:
CPU:               {process.cpu_percent:.1f}%
Memória:           {process.memory_mb:.1f} MB
Memória (% Total): {process.memory_percent:.1f}%
Threads:           {process.num_threads}

⏱️  Atualizado em:   {process.timestamp.strftime('%H:%M:%S')}

📌 Os logs deste processo aparecem em tempo real na aba "Logs"
"""

    # ─── PROCESSOS MONITORADOS (múltiplos ao mesmo tempo) ───

    def add_monitored_process(self, process: ProcessSnapshot, skip_log_watch: bool = False):
        """Adiciona um processo à lista de monitorados (ou só o ativa, se já
        estiver lá) e o exibe na aba "Processos Monitorados"."""
        pid = process.pid
        is_new = pid not in self.monitored_pids

        if is_new:
            self.monitored_pids.append(pid)
            self.process_monitor_thread.pin_pid(pid)
            self.process_history[pid] = deque(maxlen=_HISTORY_MAX_POINTS)
            self._monitored_names[pid] = process.name
            self._assign_log_color(pid)

            item = QListWidgetItem(f"{process.name} (PID {pid})")
            item.setData(Qt.ItemDataRole.UserRole, pid)
            self.monitored_list_widget.addItem(item)

            if not skip_log_watch:
                self._start_process_log_watch(process)

            self._update_monitored_summary()
            self._update_logs_tab_label()

        self._set_active_pid(pid, process)
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_selected_process))

    def _set_active_pid(self, pid: int, process: Optional[ProcessSnapshot] = None):
        """Troca qual processo monitorado está sendo exibido nos detalhes e
        no gráfico de histórico (todos continuam sendo monitorados)."""
        self.active_pid = pid
        self.active_process = process or self._pid_to_process.get(pid)
        self.terminate_btn.setEnabled(True)
        self.stop_monitor_btn.setEnabled(True)
        self.empty_state_hint_btn.hide()
        if self.active_process is not None:
            self.selected_process_label.setText(
                f"✓ Monitorando: {self.active_process.name} (PID: {self.active_process.pid})"
            )
            self.selected_process_details.setText(self._process_details_text(self.active_process))

        self.monitored_list_widget.blockSignals(True)
        for i in range(self.monitored_list_widget.count()):
            item = self.monitored_list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == pid:
                self.monitored_list_widget.setCurrentItem(item)
                break
        self.monitored_list_widget.blockSignals(False)

        self.history_chart.clear_history()
        for cpu, mem in self.process_history.get(pid, []):
            self.history_chart.add_point(cpu, mem)

    def _on_monitored_selection_changed(self, current, _previous):
        if current is None:
            return
        pid = current.data(Qt.ItemDataRole.UserRole)
        self._set_active_pid(pid, self._pid_to_process.get(pid))

    def stop_monitoring_active_process(self):
        """Remove o processo ativo da lista de monitorados (não o finaliza -
        só para de acompanhar logs/alertas/histórico dele)."""
        if self.active_pid is None:
            return
        self._stop_monitoring_pid(self.active_pid)

    def _stop_monitoring_pid(self, pid: int):
        if pid in self.monitored_pids:
            self.monitored_pids.remove(pid)
        self.process_monitor_thread.unpin_pid(pid)
        self.log_watch_app.stop_watching_pid(pid)
        self.process_history.pop(pid, None)
        self._monitored_names.pop(pid, None)
        self._pid_log_colors.pop(pid, None)

        for i in range(self.monitored_list_widget.count()):
            item = self.monitored_list_widget.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == pid:
                self.monitored_list_widget.takeItem(i)
                break

        if pid == self.active_pid:
            if self.monitored_pids:
                next_pid = self.monitored_pids[-1]
                self._set_active_pid(next_pid, self._pid_to_process.get(next_pid))
            else:
                self.active_pid = None
                self.active_process = None
                self.terminate_btn.setEnabled(False)
                self.stop_monitor_btn.setEnabled(False)
                self.empty_state_hint_btn.show()
                self.selected_process_label.setText("Nenhum processo monitorado ainda")
                self.selected_process_details.clear()
                self.history_chart.clear_history()

        self._update_monitored_summary()
        self._update_logs_tab_label()

    def _assign_log_color(self, pid: int) -> str:
        """Atribui uma cor de identificação (rotação por ordem de adição)
        a um processo monitorado, usada só para agrupar visualmente suas
        linhas no log combinado - não indica severidade."""
        if pid not in self._pid_log_colors:
            idx = len(self._pid_log_colors) % len(_LOG_PREFIX_PALETTE)
            self._pid_log_colors[pid] = _LOG_PREFIX_PALETTE[idx]
        return self._pid_log_colors[pid]

    def _update_monitored_summary(self):
        if not self.monitored_pids:
            self.monitored_summary_label.setText("Nenhum")
            return
        nomes = ", ".join(self._monitored_names.get(pid, str(pid)) for pid in self.monitored_pids)
        self.monitored_summary_label.setText(f"{len(self.monitored_pids)} — {nomes}")

    def _update_logs_tab_label(self):
        if self.monitored_pids:
            nomes = ", ".join(self._monitored_names.get(pid, str(pid)) for pid in self.monitored_pids)
            self.filtered_logs_label.setText(f"Logs em tempo real — {nomes}")
        else:
            self.filtered_logs_label.setText("Logs do processo (em tempo real)")

    def open_and_monitor_process(self):
        """Abre um executável escolhido pelo usuário e passa a monitorá-lo:
        processo, stdout/stderr em tempo real (mais confiável que achar um
        arquivo de log logo na inicialização) e alerta se ele encerrar com
        erro. Não afeta os outros processos já monitorados."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir e Monitorar", "", "Executáveis (*.exe);;Todos os arquivos (*.*)"
        )
        if not path:
            return

        try:
            proc = self.log_watch_app.launch_and_watch(
                path,
                lambda pid, label, linha: self.log_line_received.emit(pid, label, linha),
                self._on_launched_process_exit,
            )
        except OSError as e:
            QMessageBox.warning(self, "Erro ao abrir", f"Não foi possível abrir o executável:\n{e}")
            return

        logger.info("Processo lançado pelo ProcWatch: %s (PID %s)", path, proc.pid)

        snapshot = snapshot_from_pid(proc.pid)
        if snapshot is None:
            # Processo já encerrou antes de conseguirmos ler seus dados
            # (comum para executáveis muito rápidos) - a saída/código de
            # saída ainda chegam via _on_launched_process_exit.
            self.statusBar().showMessage(
                f"Processo lançado e encerrado rapidamente (PID {proc.pid}) — veja o resultado na aba de Logs.",
                5000,
            )
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_filtered_logs))
            return

        self._monitored_names[proc.pid] = snapshot.name
        # log_watch_app já está acompanhando o stdout/stderr (launch_and_watch);
        # skip_log_watch evita que add_monitored_process reinicie esse tail
        # tentando descobrir arquivos de log (watch_process_logs pararia o
        # mesmo PID que acabamos de começar a observar).
        self.add_monitored_process(snapshot, skip_log_watch=True)
        self.statusBar().showMessage(f"Processo lançado: {os.path.basename(path)} (PID {proc.pid})", 5000)

    def _on_launched_process_exit(self, pid: int, code: int):
        """Chamado (em thread de fundo) quando um processo lançado pelo
        ProcWatch encerra - independentemente do que estiver ativo na tela
        no momento."""
        name = self._monitored_names.get(pid, f"PID {pid}")

        self.log_line_received.emit(pid, "processo", f"[ProcWatch] {name} encerrou (código de saída {code})")

        if code == 0:
            logger.info("%s encerrou normalmente (código 0).", name)
        else:
            logger.warning("%s encerrou com erro (código %s).", name, code)
            self.alert_worker.check_process_exit(name, pid, code)

    def _start_process_log_watch(self, process: ProcessSnapshot):
        """Descobre e passa a monitorar em tempo real os arquivos de log
        abertos por este processo (sem afetar outros processos monitorados)."""
        log_files = self.log_watch_app.watch_process_logs(
            process.pid,
            lambda path, linha, p=process.pid: self.log_line_received.emit(p, path, linha),
        )
        self._update_logs_tab_label()
        if not log_files:
            logger.info("Nenhum arquivo de log encontrado para %s (PID %s).", process.name, process.pid)

    def _append_filtered_log(self, pid: int, path: str, linha: str):
        """Recebe uma nova linha de log (já marshalled para a thread da UI).

        O prefixo é colorido por processo (agrupamento visual pré-atento -
        princípio de similaridade de Gestalt) pra separar rapidamente
        quem escreveu o quê quando vários processos estão monitorados ao
        mesmo tempo, sem precisar ler o texto inteiro de cada linha."""
        nome = self._monitored_names.get(pid, f"PID {pid}")
        origem = os.path.basename(path)
        color = self._pid_log_colors.get(pid, COLOR_ACCENT)

        linha_escapada = (
            linha.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        prefixo = f'<span style="color:{color}; font-weight:bold;">[{nome} | {origem}]</span>'
        self.filtered_logs_text.insertHtml(f"{prefixo} {linha_escapada}<br>")

    def _row_color(self, process: ProcessSnapshot, thresholds: dict) -> QColor:
        if process.is_suspicious_path:
            return QColor(ALERT_COLORS["CRITICAL"])
        if (process.cpu_percent >= thresholds.get('cpu_critical', 95.0)
                or process.memory_percent >= thresholds.get('memory_critical', 90.0)):
            return QColor(ALERT_COLORS["CRITICAL"])
        if (process.cpu_percent >= thresholds.get('cpu_warning', 80.0)
                or process.memory_percent >= thresholds.get('memory_warning', 75.0)):
            return QColor(ALERT_COLORS["WARNING"])
        return QColor(COLOR_TEXT_BRIGHT)

    def _populate_table(self, table: QTableWidget, processes: List[ProcessSnapshot], thresholds: dict):
        """Preenche uma tabela de processos, reaproveitando células existentes
        em vez de recriá-las (evita relayout repetido com resize mode Stretch).
        Ordenação desligada durante o update: senão o Qt reordena linhas no
        meio do loop e a atualização por índice (row, col) corrompe os dados.
        setSortingEnabled(True) no final reaplica a ordenação que o usuário
        tinha escolhido (se alguma)."""
        table.setUpdatesEnabled(False)
        table.setSortingEnabled(False)
        table.setRowCount(len(processes))
        for row, process in enumerate(processes):
            values = (
                str(process.pid),
                process.name,
                f"{process.cpu_percent:.1f}",
                f"{process.memory_mb:.1f}",
                f"{process.memory_percent:.1f}",
            )
            row_color = self._row_color(process, thresholds)

            for col, value in enumerate(values):
                item = table.item(row, col)
                if item is None:
                    item = NumericTableWidgetItem(value) if col in _NUMERIC_COLUMNS else QTableWidgetItem(value)
                    item.setForeground(row_color)
                    table.setItem(row, col, item)
                else:
                    # Só grava se realmente mudou - senão o Qt dispara
                    # dataChanged/repaint à toa (comum: processo parado em
                    # 0.0% CPU, ciclo após ciclo, sem nada de novo).
                    if item.text() != value:
                        item.setText(value)
                    if item.foreground().color() != row_color:
                        item.setForeground(row_color)

            table.item(row, 0).setData(Qt.ItemDataRole.UserRole, process.pid)
        table.setSortingEnabled(True)
        self._refilter_table(table)
        table.setUpdatesEnabled(True)

    def _apply_table_filter(self, table: QTableWidget, text: str):
        self._table_filters[table] = text.strip().lower()
        self._refilter_table(table)

    def _refilter_table(self, table: QTableWidget):
        """Reaplica o filtro de nome atual da tabela - precisa rodar depois
        de todo _populate_table, já que as linhas são reconstruídas por
        índice a cada ciclo (o filtro anterior não sobrevive sozinho)."""
        text = self._table_filters.get(table, "")
        for row in range(table.rowCount()):
            name_item = table.item(row, 1)
            name = name_item.text().lower() if name_item else ""
            table.setRowHidden(row, bool(text) and text not in name)

    def _refresh_monitored_processes_display(self):
        """A cada ciclo: acumula histórico de TODOS os processos monitorados
        (mesmo os que não estão em exibição agora) e atualiza o painel de
        detalhes/gráfico apenas do que está ativo."""
        for pid in list(self.monitored_pids):
            fresh = self._pid_to_process.get(pid)
            if fresh is None:
                continue  # processo não apareceu neste ciclo (ex.: acabou de encerrar)

            self._monitored_names[pid] = fresh.name
            history = self.process_history.setdefault(pid, deque(maxlen=_HISTORY_MAX_POINTS))
            history.append((fresh.cpu_percent, fresh.memory_percent))

            if pid == self.active_pid:
                self.active_process = fresh
                self.selected_process_label.setText(f"✓ Monitorando: {fresh.name} (PID: {fresh.pid})")
                self.selected_process_details.setText(self._process_details_text(fresh))
                self.history_chart.add_point(fresh.cpu_percent, fresh.memory_percent)

    def refresh_process_list(self):
        """Atualiza a tabela de processos (escopo Aplicativos/Todos definido
        pelo alternador) e o painel de monitorados."""
        self.displayed_processes = sorted(self.all_processes, key=lambda p: p.cpu_percent, reverse=True)
        self._pid_to_process = {p.pid: p for p in self.displayed_processes}
        thresholds = self.alert_worker.engine.thresholds

        apps = [p for p in self.displayed_processes if p.has_window]
        scoped = apps if self.scope_apps_btn.isChecked() else self.displayed_processes

        self._populate_table(self.table_processes, scoped, thresholds)
        self._refresh_monitored_processes_display()

        self.statusBar().showMessage(
            f"Aplicativos: {len(apps)} | Processos: {len(self.displayed_processes)} "
            f"| Monitorados: {len(self.monitored_pids)} "
            f"| Última atualização: {datetime.now().strftime('%H:%M:%S')}"
        )

    def update_process_list(self):
        """Atualiza apenas o display."""
        self.refresh_process_list()

    def on_alert_triggered(self, alert: AlertEvent):
        """Callback quando um alerta é disparado."""
        self._alert_counts[alert.severity.value] = self._alert_counts.get(alert.severity.value, 0) + 1
        self._update_alert_counts_label()

        if self._alert_matches_filter(alert):
            self.add_alert_to_display(alert)

        if alert.severity == AlertSeverity.CRITICAL:
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_alerts))
            if getattr(self, '_tray_available', False):
                self.tray_icon.showMessage(
                    alert.title, alert.message, QSystemTrayIcon.MessageIcon.Critical, 6000
                )

    def _alert_matches_filter(self, alert: AlertEvent) -> bool:
        selected = self.severity_filter.currentText()
        return selected == "Todos" or alert.severity.value == selected

    def on_severity_filter_changed(self, _text: str):
        """Re-renderiza os alertas recentes aplicando o filtro selecionado."""
        self.text_alerts.clear()
        alerts = self.alert_worker.engine.get_recent_alerts(limit=1000)  # mais recente primeiro
        for alert in reversed(alerts):  # exibir em ordem cronológica
            if self._alert_matches_filter(alert):
                self.add_alert_to_display(alert)

    def _update_alert_counts_label(self):
        c = self._alert_counts
        self.alert_counts_label.setText(
            f"🔴 {c['CRITICAL']}  🟠 {c['ERROR']}  🟡 {c['WARNING']}  🔵 {c['INFO']}"
        )

    def add_alert_to_display(self, alert: AlertEvent):
        """Adiciona um alerta ao display."""
        color = ALERT_COLORS.get(alert.severity.value, ALERT_COLORS["INFO"])

        html = f'<p style="color: {color};"><b>[{alert.timestamp.strftime("%H:%M:%S")}]</b> '
        html += f'<b>{alert.severity.value}</b> - {alert.title}<br>'
        html += f'<i>{alert.message}</i></p>'

        self.text_alerts.insertHtml(html)

    def clear_alerts(self):
        """Limpa os alertas."""
        self.alert_worker.engine.clear_alerts()
        self.text_alerts.clear()
        self._alert_counts = {"CRITICAL": 0, "ERROR": 0, "WARNING": 0, "INFO": 0}
        self._update_alert_counts_label()

    def open_alert_settings_dialog(self):
        """Abre o diálogo de configuração dos thresholds e palavras-chave de alerta."""
        current_keywords = self.alert_worker.engine.get_custom_keywords()
        dialog = AlertSettingsDialog(self.alert_worker.engine.thresholds, current_keywords, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_thresholds = dialog.values()
            self.alert_worker.engine.update_thresholds(new_thresholds)
            save_thresholds(new_thresholds)

            critical_kw, warning_kw = dialog.keywords()
            self.alert_worker.engine.set_custom_keywords(critical_kw, warning_kw)
            save_custom_keywords(critical_kw, warning_kw)

            logger.info("Configurações de alerta atualizadas: thresholds=%s keywords=%s",
                        new_thresholds, (critical_kw, warning_kw))

    def export_alerts(self):
        """Exporta os alertas recentes para um arquivo CSV."""
        path, _ = QFileDialog.getSaveFileName(self, "Exportar Alertas", "alertas.csv", "CSV (*.csv)")
        if not path:
            return

        alerts = self.alert_worker.engine.get_recent_alerts(limit=1000)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "severidade", "origem", "titulo", "mensagem", "pid", "processo"])
            for alert in alerts:
                writer.writerow([
                    alert.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                    alert.severity.value,
                    alert.source.value,
                    alert.title,
                    alert.message,
                    alert.process_pid or "",
                    alert.process_name or "",
                ])

        self.statusBar().showMessage(f"Alertas exportados para {path}", 5000)

    def export_filtered_logs(self):
        """Exporta o conteúdo atual da aba de logs para um arquivo de texto."""
        path, _ = QFileDialog.getSaveFileName(self, "Exportar Logs", "logs.txt", "Texto (*.txt)")
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(self.filtered_logs_text.toPlainText())

        self.statusBar().showMessage(f"Logs exportados para {path}", 5000)

    def terminate_active_process(self):
        """Finaliza o processo atualmente ativo, após confirmação."""
        if self.active_pid is None:
            QMessageBox.information(self, "Nenhum processo", "Selecione um processo monitorado primeiro.")
            return

        pid = self.active_pid
        name = self._monitored_names.get(pid, f"PID {pid}")

        reply = QMessageBox.question(
            self,
            "Confirmar finalização",
            f"Finalizar o processo {name} (PID {pid})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            proc = psutil.Process(pid)
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except psutil.TimeoutExpired:
                proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.warning("Falha ao finalizar processo %s (PID %s): %s", name, pid, e)
            QMessageBox.warning(self, "Erro", f"Não foi possível finalizar o processo:\n{e}")
            return

        logger.info("Processo %s (PID %s) finalizado pelo usuário.", name, pid)
        self._stop_monitoring_pid(pid)
        self.statusBar().showMessage(f"Processo {name} (PID {pid}) finalizado.", 5000)

    def _show_tray_first_close_dialog(self):
        """Explica, de forma que fique registrada (modal, não um toast que
        some em 4s), que fechar a janela minimiza pro segundo plano em vez
        de encerrar - uma mudança real de modelo mental que merece uma
        decisão consciente do usuário, não uma notificação perecível
        (heurística de Nielsen: controle e liberdade do usuário)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("ProcWatch continua rodando")
        box.setText(
            "Fechar esta janela não encerra o ProcWatch — ele continua monitorando em "
            "segundo plano, acessível pelo ícone na bandeja do sistema.\n\n"
            "Para encerrar de verdade, clique com o botão direito no ícone da bandeja "
            "e escolha \"Sair\"."
        )
        dont_ask_checkbox = QCheckBox("Não mostrar esta mensagem novamente")
        box.setCheckBox(dont_ask_checkbox)
        box.exec()
        if dont_ask_checkbox.isChecked():
            save_tray_notice_dismissed(True)

    def closeEvent(self, event):
        """Ao fechar a janela: minimiza para a bandeja (se disponível) em
        vez de encerrar, para o monitoramento continuar em segundo plano.
        Só encerra de verdade via menu da bandeja ("Sair")."""
        if getattr(self, '_tray_available', False) and not self._closing:
            event.ignore()
            if not load_tray_notice_dismissed():
                self._show_tray_first_close_dialog()
            else:
                self.tray_icon.showMessage(
                    "ProcWatch",
                    "Continua monitorando em segundo plano. Clique com o botão direito no ícone da bandeja para sair.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            self.hide()
            return

        save_window_geometry(self.saveGeometry())
        self.log_rescan_timer.stop()
        self.process_monitor_thread.stop()
        self.log_watch_app.stop_all()
        self.alert_thread.quit()
        self.alert_thread.wait()
        logger.info("ProcWatch encerrado.")
        if getattr(self, '_tray_available', False):
            self.tray_icon.hide()
        event.accept()


def main() -> int:
    setup_logging()
    logger.info("Iniciando ProcWatch.")

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = ProcWatchMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
