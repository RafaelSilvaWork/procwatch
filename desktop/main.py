import csv
import logging
import sys
import os
import time
from datetime import datetime
from typing import List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import psutil
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QTextEdit, QPushButton, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QComboBox, QSpinBox, QDialog, QFileDialog, QMessageBox
)
from PyQt6.QtCore import QThread, QObject, pyqtSignal, Qt
from PyQt6.QtGui import QColor, QFont

from backend.app import LogWatchApp
from backend.logging_config import setup_logging
from backend.models import ProcessSnapshot, AlertEvent, AlertSeverity, AlertSource
from backend.process_monitor import ProcessMonitor
from backend.alert_engine import AlertEngine
from desktop.alert_settings_dialog import AlertSettingsDialog
from desktop.app_settings import (
    load_thresholds, load_window_geometry, save_thresholds, save_window_geometry,
)
from desktop.process_list_dialog import ProcessListDialog
from desktop.theme import ALERT_COLORS, COLOR_ACCENT, STYLESHEET

logger = logging.getLogger(__name__)


class ProcessMonitorThread(QThread):
    """Thread para listar processos disponíveis."""
    processes_updated = pyqtSignal(list)
    
    def __init__(self, interval: float = 2.0, max_processes: int = 200):
        super().__init__()
        self.monitor = ProcessMonitor(update_interval=interval, max_processes=max_processes)
        self.running = True
    
    def run(self):
        self.monitor.start(lambda snapshots: self.processes_updated.emit(snapshots))
        
        while self.running:
            time.sleep(0.1)
    
    def stop(self):
        self.running = False
        self.monitor.stop()


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

        self.setup_ui()

        self.process_monitor_thread.processes_updated.connect(self.on_processes_updated)
        # Entrega os snapshots direto para a thread do worker (conexão em fila,
        # já que o worker mora em outra thread) - não passa pela UI.
        self.process_monitor_thread.processes_updated.connect(self.alert_worker.check_processes)
        self.alert_worker.alert_triggered.connect(self.on_alert_triggered)
        self.log_line_received.connect(self._append_filtered_log)

        self.process_monitor_thread.start()
        self.alert_thread.start()
    
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

        refresh_btn = QPushButton("🔄 Atualizar Lista")
        refresh_btn.clicked.connect(self.refresh_process_list)
        header_layout.addWidget(refresh_btn)
        
        header_layout.addStretch()
        main_layout.addLayout(header_layout)
        
        # ─── TABS ───
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # Aba 1: Lista de Processos
        self.tab_process_list = QWidget()
        self.setup_tab_process_list()
        self.tabs.addTab(self.tab_process_list, "📊 Todos os Processos")
        
        # Aba 2: Processo Selecionado
        self.tab_selected_process = QWidget()
        self.setup_tab_selected_process()
        self.tabs.addTab(self.tab_selected_process, "🎯 Processo Selecionado")
        
        # Aba 3: Logs Filtrados
        self.tab_filtered_logs = QWidget()
        self.setup_tab_filtered_logs()
        self.tabs.addTab(self.tab_filtered_logs, "📝 Logs do Processo")
        
        # Aba 4: Alertas
        self.tab_alerts = QWidget()
        self.setup_tab_alerts()
        self.tabs.addTab(self.tab_alerts, "🚨 Alertas")

        # ─── BARRA DE STATUS ───
        self.statusBar().showMessage("Iniciando monitoramento...")

    def setup_tab_process_list(self):
        """Aba com lista de TODOS os processos."""
        layout = QVBoxLayout(self.tab_process_list)
        
        info = QLabel("📌 Clique em um processo abaixo para monitorar seus logs")
        info.setStyleSheet("font-weight: bold;")
        layout.addWidget(info)

        self.table_all_processes = QTableWidget()
        self.table_all_processes.setColumnCount(5)
        self.table_all_processes.setHorizontalHeaderLabels([
            "PID", "Nome", "CPU %", "Memória (MB)", "Memória %"
        ])
        self.table_all_processes.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_all_processes.itemClicked.connect(self.on_process_clicked)
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
        layout.addLayout(controls_layout)

        self.text_alerts = QTextEdit()
        self.text_alerts.setReadOnly(True)
        self.text_alerts.setObjectName("alertsLog")
        layout.addWidget(self.text_alerts)
    
    # ─── SLOTS (funções) ───
    
    def on_processes_updated(self, snapshots: List[ProcessSnapshot]):
        """Callback quando processos são listados."""
        self.all_processes = snapshots
        self.update_process_list()
    
    def on_process_clicked(self, item):
        """Quando clica em um processo na tabela."""
        row = item.row()
        if row >= 0 and row < len(self.displayed_processes):
            process = self.displayed_processes[row]
            self.select_process(process)

    def open_process_list_dialog(self):
        """Abre o seletor de processos (estilo Cheat Engine)."""
        dialog = ProcessListDialog(self.all_processes, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_process:
            self.select_process(dialog.selected_process)

    def select_process(self, process: ProcessSnapshot):
        """Seleciona um processo para monitorar."""
        self.selected_process = process
        
        # Atualizar label
        self.selected_process_label.setText(
            f"✓ Monitorando: {process.name} (PID: {process.pid})"
        )
        
        # Atualizar detalhes
        details = f"""
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
        self.selected_process_details.setText(details)

        # Atualizar resumo no header
        self.selected_process_summary.setText(f"{process.name} (PID {process.pid})")

        # Trocar para os arquivos de log deste processo
        self._start_process_log_watch(process)

        # Mudar para aba "Processo Selecionado"
        self.tabs.setCurrentIndex(1)

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
    
    def refresh_process_list(self):
        """Atualiza a lista de processos."""
        # Ordenar por CPU
        self.displayed_processes = sorted(self.all_processes, key=lambda p: p.cpu_percent, reverse=True)

        # Atualizar tabela, reaproveitando células existentes em vez de
        # recriá-las (evita relayout repetido com resize mode Stretch)
        table = self.table_all_processes
        table.setUpdatesEnabled(False)
        table.setRowCount(len(self.displayed_processes))
        for row, process in enumerate(self.displayed_processes):
            values = (
                str(process.pid),
                process.name,
                f"{process.cpu_percent:.1f}",
                f"{process.memory_mb:.1f}",
                f"{process.memory_percent:.1f}",
            )
            for col, value in enumerate(values):
                item = table.item(row, col)
                if item is None:
                    table.setItem(row, col, QTableWidgetItem(value))
                else:
                    item.setText(value)
        table.setUpdatesEnabled(True)

        selected = (
            f" | Monitorando: {self.selected_process.name} (PID {self.selected_process.pid})"
            if self.selected_process else ""
        )
        self.statusBar().showMessage(
            f"Processos: {len(self.displayed_processes)} "
            f"| Última atualização: {datetime.now().strftime('%H:%M:%S')}{selected}"
        )

    def update_process_list(self):
        """Atualiza apenas o display."""
        self.refresh_process_list()
    
    def on_alert_triggered(self, alert: AlertEvent):
        """Callback quando um alerta é disparado."""
        self.add_alert_to_display(alert)
        
        if alert.severity == AlertSeverity.CRITICAL:
            self.tabs.setCurrentIndex(3)  # Ir para aba de alertas
    
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
        self.selected_process_label.setText(f"Processo {name} (PID {pid}) finalizado.")
        self.selected_process_summary.setText("Nenhum")
        self.selected_process = None
        self.statusBar().showMessage(f"Processo {name} (PID {pid}) finalizado.", 5000)

    def closeEvent(self, event):
        """Ao fechar a janela."""
        save_window_geometry(self.saveGeometry())
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
