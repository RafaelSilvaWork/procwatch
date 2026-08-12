import csv
import logging
import sys
import os
import threading
from datetime import datetime
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psutil
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QTextEdit, QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QSpinBox, QDialog, QFileDialog, QMessageBox
)
from PyQt6.QtCore import QThread, QObject, QTimer, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont

from backend.app import LogWatchApp
from backend.logging_config import setup_logging
from backend.models import ProcessSnapshot, AlertEvent, AlertSeverity, AlertSource
from backend.process_monitor import ProcessMonitor, snapshot_from_pid
from backend.alert_engine import AlertEngine
from desktop.alert_settings_dialog import AlertSettingsDialog
from desktop.app_settings import (
    load_thresholds, load_window_geometry, save_thresholds, save_window_geometry,
)
from desktop.process_list_dialog import ProcessListDialog
from desktop.theme import ALERT_COLORS, COLOR_ACCENT, COLOR_TEXT_BRIGHT, STYLESHEET

logger = logging.getLogger(__name__)

_NUMERIC_COLUMNS = {0, 2, 3, 4}  # PID, CPU %, Memória (MB), Memória %


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
    
    def __init__(self, interval: float = 2.0, max_processes: int = 200):
        super().__init__()
        self.monitor = ProcessMonitor(update_interval=interval, max_processes=max_processes)
        self._stop_event = threading.Event()

    def run(self):
        self.monitor.start(lambda snapshots: self.processes_updated.emit(snapshots))
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

    def set_pinned_pid(self, pid: Optional[int]):
        self.monitor.set_pinned_pid(pid)


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


class LogWatchMainWindow(QMainWindow):
    """Janela principal do LogWatch - Versão com seleção de processo."""

    log_line_received = pyqtSignal(str, str)  # (caminho do arquivo, linha)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("LogWatch v2 | Filtro de Logs por Processo")
        self.setGeometry(100, 100, 1400, 800)

        saved_geometry = load_window_geometry()
        if saved_geometry:
            self.restoreGeometry(saved_geometry)

        # Monitor roda a cada 2s (menos pesado)
        self.process_monitor_thread = ProcessMonitorThread(interval=2.0, max_processes=200)

        # Worker de alertas rodando em thread própria
        self.alert_thread = QThread()
        self.alert_worker = AlertWorker()
        self.alert_worker.moveToThread(self.alert_thread)

        saved_thresholds = load_thresholds()
        if saved_thresholds:
            self.alert_worker.engine.update_thresholds(saved_thresholds)
            logger.info("Thresholds de alerta carregados de logwatch.ini: %s", saved_thresholds)

        # Monitoramento dos arquivos de log do processo selecionado
        self.log_watch_app = LogWatchApp()

        # Processo selecionado
        self.selected_process: Optional[ProcessSnapshot] = None
        self.all_processes: List[ProcessSnapshot] = []
        self.displayed_processes: List[ProcessSnapshot] = []
        self._pid_to_process: dict = {}
        self._alert_counts = {"CRITICAL": 0, "ERROR": 0, "WARNING": 0, "INFO": 0}

        self.setup_ui()

        self.process_monitor_thread.processes_updated.connect(self.on_processes_updated)
        # Entrega os snapshots direto para a thread do worker (conexão em fila,
        # já que o worker mora em outra thread) - não passa pela UI.
        self.process_monitor_thread.processes_updated.connect(self.alert_worker.check_processes)
        self.alert_worker.alert_triggered.connect(self.on_alert_triggered)
        self.log_line_received.connect(self._append_filtered_log)

        self.process_monitor_thread.start()
        self.alert_thread.start()

        # Rechecar periodicamente se o processo selecionado abriu arquivos
        # de log novos (ex.: rotação diária), sem reiniciar o tail dos que
        # já estão sendo acompanhados.
        self.log_rescan_timer = QTimer(self)
        self.log_rescan_timer.timeout.connect(self._rescan_process_logs)
        self.log_rescan_timer.start(5_000)

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        main_layout = QVBoxLayout(central_widget)
        
        # ─── HEADER COM SELECTOR ───
        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel("📋 Processo monitorado:"))

        self.selected_process_summary = QLabel("Nenhum")
        self.selected_process_summary.setStyleSheet(f"color: {COLOR_ACCENT}; font-weight: bold;")
        header_layout.addWidget(self.selected_process_summary)

        select_btn = QPushButton("🎯 Selecionar Processo...")
        select_btn.clicked.connect(self.open_process_list_dialog)
        header_layout.addWidget(select_btn)

        launch_btn = QPushButton("▶️ Abrir e Monitorar...")
        launch_btn.clicked.connect(self.open_and_monitor_process)
        header_layout.addWidget(launch_btn)

        refresh_btn = QPushButton("🔄 Atualizar Lista")
        refresh_btn.clicked.connect(self.request_process_refresh)
        header_layout.addWidget(refresh_btn)
        
        header_layout.addStretch()
        main_layout.addLayout(header_layout)
        
        # ─── TABS ───
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        # Aba 1: Aplicativos (só processos com janela visível)
        self.tab_apps = QWidget()
        self.setup_tab_apps()
        self.tabs.addTab(self.tab_apps, "🖥️ Aplicativos")

        # Aba 2: Todos os processos (inclui tarefas em segundo plano)
        self.tab_process_list = QWidget()
        self.setup_tab_process_list()
        self.tabs.addTab(self.tab_process_list, "📊 Todos os Processos")

        # Aba 3: Processo Selecionado
        self.tab_selected_process = QWidget()
        self.setup_tab_selected_process()
        self.tabs.addTab(self.tab_selected_process, "🎯 Processo Selecionado")

        # Aba 4: Logs Filtrados
        self.tab_filtered_logs = QWidget()
        self.setup_tab_filtered_logs()
        self.tabs.addTab(self.tab_filtered_logs, "📝 Logs do Processo")

        # Aba 5: Alertas
        self.tab_alerts = QWidget()
        self.setup_tab_alerts()
        self.tabs.addTab(self.tab_alerts, "🚨 Alertas")

        # ─── BARRA DE STATUS ───
        self.statusBar().showMessage("Iniciando monitoramento...")

    def _build_process_table(self) -> QTableWidget:
        """Cria uma QTableWidget no formato usado pelas abas de processos."""
        table = QTableWidget()
        table.setColumnCount(5)
        table.setHorizontalHeaderLabels(["PID", "Nome", "CPU %", "Memória (MB)", "Memória %"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSortingEnabled(True)
        table.itemClicked.connect(self.on_process_clicked)
        return table

    def setup_tab_apps(self):
        """Aba só com aplicativos de verdade (processos com janela visível) -
        equivalente à aba "Apps" do Gerenciador de Tarefas do Windows."""
        layout = QVBoxLayout(self.tab_apps)

        info = QLabel("🖥️ Aplicativos com janela aberta - clique para monitorar")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        self.table_apps = self._build_process_table()
        layout.addWidget(self.table_apps)

    def setup_tab_process_list(self):
        """Aba com TODOS os processos, incluindo tarefas em segundo plano."""
        layout = QVBoxLayout(self.tab_process_list)

        info = QLabel("📌 Todos os processos do sistema - clique para monitorar")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        self.table_all_processes = self._build_process_table()
        layout.addWidget(self.table_all_processes)

    def setup_tab_selected_process(self):
        """Aba com informações do processo selecionado."""
        layout = QVBoxLayout(self.tab_selected_process)

        # Info do processo
        self.selected_process_label = QLabel("Nenhum processo selecionado")
        self.selected_process_label.setStyleSheet(f"color: {COLOR_ACCENT}; font-size: 14pt; font-weight: bold;")
        layout.addWidget(self.selected_process_label)

        # Detalhes
        self.selected_process_details = QTextEdit()
        self.selected_process_details.setReadOnly(True)
        self.selected_process_details.setStyleSheet("font-size: 11pt;")
        layout.addWidget(self.selected_process_details)

        terminate_btn = QPushButton("🛑 Finalizar Processo")
        terminate_btn.clicked.connect(self.terminate_selected_process)
        layout.addWidget(terminate_btn)
    
    def setup_tab_filtered_logs(self):
        """Aba com logs filtrados do processo selecionado."""
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
    
    # ─── SLOTS (funções) ───
    
    def on_processes_updated(self, snapshots: List[ProcessSnapshot]):
        """Callback quando processos são listados."""
        self.all_processes = snapshots
        self.update_process_list()
    
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
            self.select_process(process)

    def open_process_list_dialog(self):
        """Abre o seletor de processos (estilo Cheat Engine)."""
        dialog = ProcessListDialog(self.all_processes, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_process:
            self.select_process(dialog.selected_process)

    def request_process_refresh(self):
        """Força uma nova varredura imediata (não só reexibe o último snapshot)."""
        self.process_monitor_thread.request_refresh()
        self.statusBar().showMessage("Atualizando lista de processos...", 2000)

    def _rescan_process_logs(self):
        """Chamado periodicamente: pega arquivos de log novos do processo
        selecionado sem reiniciar o tail dos que já estão sendo lidos."""
        if self.selected_process is None:
            return

        log_files = self.log_watch_app.sync_process_logs(
            self.selected_process.pid,
            lambda path, linha: self.log_line_received.emit(path, linha),
        )
        if log_files:
            nomes = ", ".join(os.path.basename(p) for p in log_files)
            self.filtered_logs_label.setText(f"Logs do processo (em tempo real) — {nomes}")

    @staticmethod
    def _process_details_text(process: ProcessSnapshot) -> str:
        return f"""
🔍 INFORMAÇÕES DO PROCESSO SELECIONADO
═══════════════════════════════════════

Nome:              {process.name}
PID:               {process.pid}
Status:            {process.status}

💻 RECURSOS:
CPU:               {process.cpu_percent:.1f}%
Memória:           {process.memory_mb:.1f} MB
Memória (% Total): {process.memory_percent:.1f}%
Threads:           {process.num_threads}

⏱️  Atualizado em:   {process.timestamp.strftime('%H:%M:%S')}

📌 Os logs deste processo aparecerão em tempo real na aba "Logs do Processo"
"""

    def select_process(self, process: ProcessSnapshot):
        """Seleciona um processo já existente para monitorar."""
        self.selected_process = process
        self.process_monitor_thread.set_pinned_pid(process.pid)
        self.selected_process_label.setText(f"✓ Monitorando: {process.name} (PID: {process.pid})")
        self.selected_process_details.setText(self._process_details_text(process))
        self.selected_process_summary.setText(f"{process.name} (PID {process.pid})")

        # Trocar para os arquivos de log deste processo
        self._start_process_log_watch(process)

        # Mudar para aba "Processo Selecionado"
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_selected_process))

    def open_and_monitor_process(self):
        """Abre um executável escolhido pelo usuário e passa a monitorá-lo:
        processo, stdout/stderr em tempo real (mais confiável que achar um
        arquivo de log logo na inicialização) e alerta se ele encerrar com
        erro."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Abrir e Monitorar", "", "Executáveis (*.exe);;Todos os arquivos (*.*)"
        )
        if not path:
            return

        try:
            proc = self.log_watch_app.launch_and_watch(
                path,
                lambda label, linha: self.log_line_received.emit(label, linha),
                self._on_launched_process_exit,
            )
        except OSError as e:
            QMessageBox.warning(self, "Erro ao abrir", f"Não foi possível abrir o executável:\n{e}")
            return

        logger.info("Processo lançado pelo LogWatch: %s (PID %s)", path, proc.pid)

        self.filtered_logs_text.clear()
        self.filtered_logs_label.setText(
            f"Logs do processo (em tempo real) — stdout/stderr de {os.path.basename(path)}"
        )

        snapshot = snapshot_from_pid(proc.pid)
        if snapshot is None:
            # Processo já encerrou antes de conseguirmos ler seus dados
            # (comum para executáveis muito rápidos) - a saída/código de
            # saída ainda chegam via _on_launched_process_exit.
            self.selected_process_label.setText(
                f"Processo {os.path.basename(path)} (PID {proc.pid}) já encerrou antes de ser inspecionado."
            )
            self.statusBar().showMessage(
                f"Processo lançado e encerrado rapidamente (PID {proc.pid}) — veja o resultado na aba de Logs.",
                5000,
            )
            self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_filtered_logs))
            return

        # Ao contrário de select_process(): não chama _start_process_log_watch,
        # pois isso reiniciaria o tail (stop_all) e derrubaria o
        # acompanhamento de stdout/stderr que acabamos de montar. O rescan
        # periódico (_rescan_process_logs) ainda vai somar arquivos de log
        # que esse processo abrir, sem mexer no que já está sendo lido.
        self.selected_process = snapshot
        self.process_monitor_thread.set_pinned_pid(snapshot.pid)
        self.selected_process_label.setText(
            f"✓ Monitorando (lançado agora): {snapshot.name} (PID: {snapshot.pid})"
        )
        self.selected_process_details.setText(self._process_details_text(snapshot))
        self.selected_process_summary.setText(f"{snapshot.name} (PID {snapshot.pid})")
        self.tabs.setCurrentIndex(self.tabs.indexOf(self.tab_selected_process))
        self.statusBar().showMessage(f"Processo lançado: {os.path.basename(path)} (PID {proc.pid})", 5000)

    def _on_launched_process_exit(self, pid: int, code: int):
        """Chamado (em thread de fundo) quando um processo lançado pelo
        LogWatch encerra - independentemente do que estiver selecionado
        na tela no momento."""
        name = self.selected_process.name if (self.selected_process and self.selected_process.pid == pid) else f"PID {pid}"

        self.log_line_received.emit("processo", f"[LogWatch] {name} encerrou (código de saída {code})")

        if code == 0:
            logger.info("%s encerrou normalmente (código 0).", name)
        else:
            logger.warning("%s encerrou com erro (código %s).", name, code)
            self.alert_worker.check_log(f"{name} exited with error code {code}")

    def _start_process_log_watch(self, process: ProcessSnapshot):
        """Descobre e passa a monitorar em tempo real os arquivos de log
        abertos pelo processo selecionado."""
        self.filtered_logs_text.clear()
        log_files = self.log_watch_app.watch_process_logs(
            process.pid,
            lambda path, linha: self.log_line_received.emit(path, linha),
        )

        if log_files:
            nomes = ", ".join(os.path.basename(p) for p in log_files)
            self.filtered_logs_label.setText(f"Logs do processo (em tempo real) — {nomes}")
        else:
            self.filtered_logs_label.setText(
                "Nenhum arquivo de log encontrado para este processo "
                "(pode exigir permissão de administrador)"
            )

    def _append_filtered_log(self, path: str, linha: str):
        """Recebe uma nova linha de log (já marshalled para a thread da UI)."""
        nome = os.path.basename(path)
        self.filtered_logs_text.append(f"[{nome}] {linha}")
    
    def _row_color(self, process: ProcessSnapshot, thresholds: dict) -> QColor:
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
        table.setUpdatesEnabled(True)

    def _refresh_selected_process_display(self):
        """Atualiza o painel "Processo Selecionado" (CPU/memória/etc.) com o
        snapshot mais recente. Sem isso, o painel fica congelado com os
        dados do instante da seleção para sempre, mesmo com o monitor
        continuando a rodar normalmente."""
        if self.selected_process is None:
            return

        fresh = self._pid_to_process.get(self.selected_process.pid)
        if fresh is None:
            return  # processo não apareceu neste ciclo (ex.: acabou de encerrar)

        self.selected_process = fresh
        self.selected_process_label.setText(f"✓ Monitorando: {fresh.name} (PID: {fresh.pid})")
        self.selected_process_details.setText(self._process_details_text(fresh))

    def refresh_process_list(self):
        """Atualiza as listas de processos (Aplicativos e Todos os Processos)."""
        self.displayed_processes = sorted(self.all_processes, key=lambda p: p.cpu_percent, reverse=True)
        self._pid_to_process = {p.pid: p for p in self.displayed_processes}
        thresholds = self.alert_worker.engine.thresholds

        apps = [p for p in self.displayed_processes if p.has_window]

        self._populate_table(self.table_apps, apps, thresholds)
        self._populate_table(self.table_all_processes, self.displayed_processes, thresholds)
        self._refresh_selected_process_display()

        selected = (
            f" | Monitorando: {self.selected_process.name} (PID {self.selected_process.pid})"
            if self.selected_process else ""
        )
        self.statusBar().showMessage(
            f"Aplicativos: {len(apps)} | Processos: {len(self.displayed_processes)} "
            f"| Última atualização: {datetime.now().strftime('%H:%M:%S')}{selected}"
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
        """Abre o diálogo de configuração dos thresholds de alerta."""
        dialog = AlertSettingsDialog(self.alert_worker.engine.thresholds, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            new_thresholds = dialog.values()
            self.alert_worker.engine.update_thresholds(new_thresholds)
            save_thresholds(new_thresholds)
            logger.info("Thresholds de alerta atualizados: %s", new_thresholds)

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
        """Exporta o conteúdo atual da aba de logs do processo para um arquivo de texto."""
        path, _ = QFileDialog.getSaveFileName(self, "Exportar Logs", "logs.txt", "Texto (*.txt)")
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(self.filtered_logs_text.toPlainText())

        self.statusBar().showMessage(f"Logs exportados para {path}", 5000)

    def terminate_selected_process(self):
        """Finaliza o processo atualmente selecionado, após confirmação."""
        if self.selected_process is None:
            QMessageBox.information(self, "Nenhum processo", "Selecione um processo primeiro.")
            return

        reply = QMessageBox.question(
            self,
            "Confirmar finalização",
            f"Finalizar o processo {self.selected_process.name} (PID {self.selected_process.pid})?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        pid, name = self.selected_process.pid, self.selected_process.name
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
        self.log_watch_app.stop_all()
        self.process_monitor_thread.set_pinned_pid(None)
        self.selected_process_label.setText(f"Processo {name} (PID {pid}) finalizado.")
        self.selected_process_summary.setText("Nenhum")
        self.selected_process = None
        self.statusBar().showMessage(f"Processo {name} (PID {pid}) finalizado.", 5000)

    def closeEvent(self, event):
        """Ao fechar a janela."""
        save_window_geometry(self.saveGeometry())
        self.log_rescan_timer.stop()
        self.process_monitor_thread.stop()
        self.log_watch_app.stop_all()
        self.alert_thread.quit()
        self.alert_thread.wait()
        logger.info("LogWatch encerrado.")
        event.accept()


def main() -> int:
    setup_logging()
    logger.info("Iniciando LogWatch.")

    app = QApplication(sys.argv)
    app.setStyleSheet(STYLESHEET)
    window = LogWatchMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
