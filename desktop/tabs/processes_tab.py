"""Aba "Processos": tabela com todos os processos (ou só aplicativos),
filtro por nome, e clique para começar a monitorar."""

from datetime import datetime
from typing import List

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from backend.models import ProcessSnapshot
from desktop.tabs.constants import _NUMERIC_COLUMNS
from desktop.theme import ALERT_COLORS, COLOR_ACCENT, COLOR_TEXT_BRIGHT


class NumericTableWidgetItem(QTableWidgetItem):
    """QTableWidgetItem que ordena numericamente em vez de como texto
    (senão "10.0" viria antes de "9.0" na ordenação por clique no cabeçalho)."""

    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


class ProcessesTabMixin:
    """Depende de atributos/métodos definidos em ProcWatchMainWindow:
    self.tab_processes, self.all_processes, self._pid_to_process,
    self.displayed_processes, self.alert_worker, self.process_monitor_thread,
    self.statusBar(), self.monitored_pids, self.add_monitored_process,
    self._refresh_monitored_processes_display."""

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
            self.add_monitored_process(process)

    def request_process_refresh(self):
        """Força uma nova varredura imediata (não só reexibe o último snapshot)."""
        self.process_monitor_thread.request_refresh()
        self.statusBar().showMessage("Atualizando lista de processos...", 2000)

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
