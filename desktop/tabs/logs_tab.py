"""Aba "Logs": tail combinado (em tempo real) de todos os processos
monitorados, prefixado e colorido por processo de origem."""

import os

from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout

from desktop.theme import COLOR_ACCENT


class LogsTabMixin:
    """Depende de atributos/métodos definidos em ProcWatchMainWindow:
    self.tab_filtered_logs, self.monitored_pids, self._monitored_names,
    self._pid_log_colors, self.log_watch_app."""

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

    def _rescan_process_logs(self):
        """Chamado periodicamente: pega arquivos de log novos de TODOS os
        processos monitorados, sem reiniciar o tail dos que já estão sendo
        lidos."""
        for pid in list(self.monitored_pids):
            self.log_watch_app.sync_process_logs(
                pid,
                lambda path, linha, p=pid: self.log_line_received.emit(p, path, linha),
            )

    def _update_logs_tab_label(self):
        if self.monitored_pids:
            nomes = ", ".join(self._monitored_names.get(pid, str(pid)) for pid in self.monitored_pids)
            self.filtered_logs_label.setText(f"Logs em tempo real — {nomes}")
        else:
            self.filtered_logs_label.setText("Logs do processo (em tempo real)")

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

    def export_filtered_logs(self):
        """Exporta o conteúdo atual da aba de logs para um arquivo de texto."""
        path, _ = QFileDialog.getSaveFileName(self, "Exportar Logs", "logs.txt", "Texto (*.txt)")
        if not path:
            return

        with open(path, "w", encoding="utf-8") as f:
            f.write(self.filtered_logs_text.toPlainText())

        self.statusBar().showMessage(f"Logs exportados para {path}", 5000)
