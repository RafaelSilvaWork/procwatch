"""Diálogo de seleção de processo, no estilo do 'Process List' do Cheat Engine."""

from typing import Dict, List, Optional

import psutil
from PyQt6.QtCore import QFileInfo, Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileIconProvider, QHBoxLayout, QLineEdit, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from backend.models import ProcessSnapshot
from backend.window_utils import get_pids_with_visible_window

# Ícones reais de .exe são extraídos via shell do Windows e são lentos;
# carregamos poucos por vez para não travar a UI.
_ICONS_PER_BATCH = 20


class ProcessListDialog(QDialog):
    """Janela modal de seleção de processo (árvore com ícone + PID-nome,
    agrupando instâncias repetidas do mesmo executável, Open/Cancel)."""

    def __init__(self, processes: List[ProcessSnapshot], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Process List")
        self.resize(420, 480)
        self.setModal(True)

        self.processes = processes
        self.selected_process: Optional[ProcessSnapshot] = None
        self._icon_provider = QFileIconProvider()
        self._icon_cache: Dict[str, QIcon] = {}
        self._generic_icon = self._icon_provider.icon(QFileIconProvider.IconType.File)
        self._group_icon = self._icon_provider.icon(QFileIconProvider.IconType.Folder)
        self._icon_queue: List[QTreeWidgetItem] = []

        layout = QVBoxLayout(self)

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filtrar por nome...")
        self.filter_input.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_input)

        self.apps_only_checkbox = QCheckBox("🖥️ Somente aplicativos abertos (com janela)")
        self.apps_only_checkbox.toggled.connect(self._apply_filter)
        layout.addWidget(self.apps_only_checkbox)

        self.tree_widget = QTreeWidget()
        self.tree_widget.setHeaderHidden(True)
        self.tree_widget.setColumnCount(1)
        self.tree_widget.setStyleSheet(
            "QTreeWidget { background-color: #ffffff; color: #000000;"
            " font-family: Consolas; font-size: 9pt; }"
            "QTreeWidget::item { padding: 2px; }"
            "QTreeWidget::item:selected { background-color: #3399ff; color: #ffffff; }"
        )
        self.tree_widget.itemDoubleClicked.connect(lambda item, col: self._open())
        layout.addWidget(self.tree_widget)

        button_row = QHBoxLayout()
        self.open_btn = QPushButton("Open")
        self.open_btn.setDefault(True)
        self.open_btn.clicked.connect(self._open)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(self.open_btn)
        button_row.addWidget(self.cancel_btn)
        layout.addLayout(button_row)

        self.refresh_btn = QPushButton("Atualizar lista")
        self.refresh_btn.clicked.connect(self._refresh)
        layout.addWidget(self.refresh_btn)

        self._populate(self.processes)

    def _populate(self, processes: List[ProcessSnapshot]) -> None:
        """Preenche a árvore instantaneamente com ícone genérico, agrupando
        processos com o mesmo nome; ícones reais vêm depois em segundo plano."""
        self.tree_widget.clear()
        self._icon_queue = []

        groups: Dict[str, List[ProcessSnapshot]] = {}
        for process in processes:
            groups.setdefault(process.name, []).append(process)

        for name in sorted(groups, key=str.lower):
            members = sorted(groups[name], key=lambda p: p.pid)

            if len(members) == 1:
                leaf = self._make_leaf_item(members[0])
                self.tree_widget.addTopLevelItem(leaf)
                self._icon_queue.append(leaf)
                continue

            group_item = QTreeWidgetItem([f"{name} ({len(members)})"])
            group_item.setIcon(0, self._group_icon)
            self.tree_widget.addTopLevelItem(group_item)
            for process in members:
                leaf = self._make_leaf_item(process)
                group_item.addChild(leaf)
                self._icon_queue.append(leaf)

        QTimer.singleShot(0, self._load_next_icon_batch)

    def _make_leaf_item(self, process: ProcessSnapshot) -> QTreeWidgetItem:
        item = QTreeWidgetItem([f"{process.pid:08X}-{process.name}"])
        item.setIcon(0, self._generic_icon)
        item.setData(0, Qt.ItemDataRole.UserRole, process)
        return item

    def _load_next_icon_batch(self) -> None:
        """Carrega os ícones reais em pequenos lotes, sem bloquear a UI."""
        batch, self._icon_queue = self._icon_queue[:_ICONS_PER_BATCH], self._icon_queue[_ICONS_PER_BATCH:]
        for item in batch:
            process = item.data(0, Qt.ItemDataRole.UserRole)
            item.setIcon(0, self._icon_for(process))

        if self._icon_queue:
            QTimer.singleShot(0, self._load_next_icon_batch)

    def _icon_for(self, process: ProcessSnapshot) -> QIcon:
        try:
            exe_path = psutil.Process(process.pid).exe()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            exe_path = ""

        if not exe_path:
            return self._generic_icon

        cached = self._icon_cache.get(exe_path)
        if cached is not None:
            return cached

        icon = self._icon_provider.icon(QFileInfo(exe_path))
        if icon.isNull():
            icon = self._generic_icon

        self._icon_cache[exe_path] = icon
        return icon

    def _matches_filter(self, process: ProcessSnapshot, text_lower: str, apps_only: bool) -> bool:
        return text_lower in process.name.lower() and (not apps_only or process.has_window)

    def _apply_filter(self, *_args) -> None:
        """Mostra/esconde itens já existentes - não recria a árvore nem refaz ícones."""
        text_lower = self.filter_input.text().lower()
        apps_only = self.apps_only_checkbox.isChecked()

        for i in range(self.tree_widget.topLevelItemCount()):
            top = self.tree_widget.topLevelItem(i)
            process = top.data(0, Qt.ItemDataRole.UserRole)

            if process is not None:
                # processo único, sem grupo
                top.setHidden(not self._matches_filter(process, text_lower, apps_only))
                continue

            any_child_visible = False
            for j in range(top.childCount()):
                child = top.child(j)
                child_process = child.data(0, Qt.ItemDataRole.UserRole)
                match = self._matches_filter(child_process, text_lower, apps_only)
                child.setHidden(not match)
                any_child_visible = any_child_visible or match

            top.setHidden(not any_child_visible)
            if text_lower or apps_only:
                top.setExpanded(any_child_visible)

    def _refresh(self) -> None:
        """Faz uma nova varredura leve de processos (pid + nome) direto via psutil."""
        window_pids = get_pids_with_visible_window()
        snapshots = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                snapshots.append(ProcessSnapshot(
                    pid=proc.info['pid'],
                    name=proc.info['name'] or "unknown",
                    cpu_percent=0.0,
                    memory_mb=0.0,
                    memory_percent=0.0,
                    io_read_mb=0.0,
                    io_write_mb=0.0,
                    status="unknown",
                    has_window=proc.info['pid'] in window_pids,
                ))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        self.processes = snapshots
        self._populate(self.processes)
        self._apply_filter()

    def _open(self) -> None:
        item = self.tree_widget.currentItem()
        if item is None:
            return
        process = item.data(0, Qt.ItemDataRole.UserRole)
        if process is None:
            return  # é um grupo - duplo clique já expande/colapsa nativamente
        self.selected_process = process
        self.accept()
