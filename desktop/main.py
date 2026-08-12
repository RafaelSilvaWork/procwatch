import logging
import sys
import os
from typing import Deque, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QPushButton, QLabel, QCheckBox, QMenu, QMessageBox,
    QStyle, QSystemTrayIcon,
)
from PyQt6.QtCore import QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon

from backend.app import ProcWatchApp
from backend.logging_config import setup_logging
from backend.models import ProcessSnapshot
from desktop.app_settings import (
    load_custom_keywords, load_thresholds, load_tray_notice_dismissed, load_window_geometry,
    save_tray_notice_dismissed, save_window_geometry,
)
from desktop.tabs.alerts_tab import AlertsTabMixin
from desktop.tabs.logs_tab import LogsTabMixin
from desktop.tabs.monitored_tab import MonitoredTabMixin
from desktop.tabs.processes_tab import ProcessesTabMixin
from desktop.tabs.system_tab import SystemTabMixin
from desktop.theme import COLOR_ACCENT, STYLESHEET
from desktop.workers import AlertWorker, ProcessMonitorThread

logger = logging.getLogger(__name__)


def _base_dir() -> str:
    """Raiz do projeto (ou do bundle, quando empacotado com PyInstaller) -
    onde procurar recursos como desktop/resources/icon.ico."""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_app_icon() -> QIcon:
    icon_path = os.path.join(_base_dir(), "desktop", "resources", "icon.ico")
    icon = QIcon(icon_path)
    if icon.isNull():
        logger.warning("Ícone do app não encontrado em %s", icon_path)
    return icon


class ProcWatchMainWindow(
    QMainWindow,
    ProcessesTabMixin,
    MonitoredTabMixin,
    LogsTabMixin,
    AlertsTabMixin,
    SystemTabMixin,
):
    """Janela principal do ProcWatch - monitora múltiplos processos ao
    mesmo tempo (CPU/memória/histórico/logs/alertas).

    A UI e os handlers de cada aba vivem em desktop/tabs/*_tab.py (misturados
    aqui via herança múltipla); esta classe cuida só do que atravessa todas
    elas: estado compartilhado, montagem da janela, bandeja e ciclo de vida."""

    log_line_received = pyqtSignal(int, str, str)  # (pid, caminho/rótulo, linha)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ProcWatch | Monitor de Processos e Logs")
        self.setWindowIcon(load_app_icon())

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

    # ─── BANDEJA DO SISTEMA ───

    def _setup_tray_icon(self):
        self._tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if not self._tray_available:
            logger.warning("Bandeja do sistema não disponível neste ambiente.")
            return

        icon = load_app_icon()
        if icon.isNull():
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

    # ─── CICLO DE VIDA ───

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
    app.setWindowIcon(load_app_icon())
    app.setStyleSheet(STYLESHEET)
    window = ProcWatchMainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
