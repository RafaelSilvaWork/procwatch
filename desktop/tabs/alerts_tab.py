"""Aba "Alertas": lista filtrável dos alertas disparados pelo engine, com
configuração de thresholds/palavras-chave e exportação para CSV."""

import csv
import logging

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton,
    QSystemTrayIcon, QTextEdit, QVBoxLayout,
)

from backend.models import AlertEvent, AlertSeverity
from desktop.alert_settings_dialog import AlertSettingsDialog
from desktop.app_settings import save_custom_keywords, save_thresholds
from desktop.theme import ALERT_COLORS

logger = logging.getLogger(__name__)


class AlertsTabMixin:
    """Depende de atributos/métodos definidos em ProcWatchMainWindow:
    self.tab_alerts, self.alert_worker, self.tabs, self.tray_icon,
    self._tray_available."""

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
