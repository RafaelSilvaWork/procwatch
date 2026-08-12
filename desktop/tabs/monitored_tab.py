"""Aba "Monitorados": lista de processos acompanhados simultaneamente,
detalhes + histórico do que está ativo, e as ações de abrir/finalizar
processo."""

import logging
import os
from collections import deque
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QFileDialog, QHBoxLayout, QLabel, QListWidgetItem, QListWidget,
    QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)
import psutil

from backend.models import ProcessSnapshot
from backend.process_monitor import snapshot_from_pid
from desktop.history_chart import HistoryChartWidget
from desktop.process_list_dialog import ProcessListDialog
from desktop.tabs.constants import _HISTORY_MAX_POINTS, _LOG_PREFIX_PALETTE
from desktop.theme import COLOR_ACCENT

logger = logging.getLogger(__name__)


class MonitoredTabMixin:
    """Depende de atributos/métodos definidos em ProcWatchMainWindow:
    self.tab_selected_process, self.monitored_pids, self.active_pid,
    self.active_process, self.process_history, self._monitored_names,
    self._pid_log_colors, self._pid_to_process, self.log_watch_app,
    self.log_line_received, self.alert_worker, self.tabs,
    self.tab_selected_process, self._update_logs_tab_label."""

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

    def open_process_list_dialog(self):
        """Abre o seletor de processos (estilo Cheat Engine)."""
        dialog = ProcessListDialog(self.all_processes, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.selected_process:
            self.add_monitored_process(dialog.selected_process)

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
