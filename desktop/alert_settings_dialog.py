"""Diálogo para configurar os limites (thresholds) e palavras-chave de alerta."""

from typing import Dict, List, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox,
    QWidget,
)


class AlertSettingsDialog(QDialog):
    """Permite ajustar os thresholds e as palavras-chave de detecção de
    erro usados pelo AlertEngine em tempo real."""

    def __init__(
        self,
        current_thresholds: Dict[str, float],
        current_keywords: Tuple[List[str], List[str]] = ([], []),
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Configurar Alertas")
        self.setModal(True)
        self.resize(420, 520)

        custom_critical, custom_warning = current_keywords

        layout = QFormLayout(self)

        self.cpu_warning = self._percent_spinbox(current_thresholds['cpu_warning'])
        self.cpu_critical = self._percent_spinbox(current_thresholds['cpu_critical'])
        self.memory_warning = self._percent_spinbox(current_thresholds['memory_warning'])
        self.memory_critical = self._percent_spinbox(current_thresholds['memory_critical'])

        self.sustained_samples = QSpinBox()
        self.sustained_samples.setRange(2, 120)
        self.sustained_samples.setValue(int(current_thresholds['sustained_high_cpu_secs']))

        self.memory_leak_growth = QDoubleSpinBox()
        self.memory_leak_growth.setRange(1.0, 10000.0)
        self.memory_leak_growth.setDecimals(0)
        self.memory_leak_growth.setSuffix(" MB")
        self.memory_leak_growth.setValue(current_thresholds.get('memory_leak_growth_mb', 50.0))

        self.memory_leak_samples = QSpinBox()
        self.memory_leak_samples.setRange(3, 30)
        self.memory_leak_samples.setValue(int(current_thresholds.get('memory_leak_min_samples', 10)))

        layout.addRow("CPU - aviso:", self.cpu_warning)
        layout.addRow("CPU - crítico:", self.cpu_critical)
        layout.addRow("Memória - aviso:", self.memory_warning)
        layout.addRow("Memória - crítico:", self.memory_critical)
        layout.addRow("CPU sustentada (nº de amostras):", self.sustained_samples)
        layout.addRow("Vazamento - crescimento mínimo:", self.memory_leak_growth)
        layout.addRow("Vazamento - nº de amostras:", self.memory_leak_samples)

        self.critical_keywords_list = self._build_keyword_section(
            layout, "Palavras-chave críticas extras:", custom_critical
        )
        self.warning_keywords_list = self._build_keyword_section(
            layout, "Palavras-chave de aviso extras:", custom_warning
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _build_keyword_section(self, layout: QFormLayout, label: str, initial: List[str]) -> QListWidget:
        """Cria uma lista de palavras-chave com campo de adicionar e botão
        de remover, e a encaixa no QFormLayout do diálogo."""
        layout.addRow(QLabel(label))

        keywords_list = QListWidget()
        keywords_list.addItems(initial)
        keywords_list.setMaximumHeight(80)
        layout.addRow(keywords_list)

        add_row = QWidget()
        add_row_layout = QHBoxLayout(add_row)
        add_row_layout.setContentsMargins(0, 0, 0, 0)
        new_keyword_input = QLineEdit()
        new_keyword_input.setPlaceholderText("nova palavra-chave...")
        add_btn = QPushButton("+ Adicionar")
        remove_btn = QPushButton("Remover selecionada")

        def add_keyword():
            text = new_keyword_input.text().strip().lower()
            if text and not keywords_list.findItems(text, Qt.MatchFlag.MatchExactly):
                keywords_list.addItem(text)
                new_keyword_input.clear()

        def remove_keyword():
            for item in keywords_list.selectedItems():
                keywords_list.takeItem(keywords_list.row(item))

        add_btn.clicked.connect(add_keyword)
        new_keyword_input.returnPressed.connect(add_keyword)
        remove_btn.clicked.connect(remove_keyword)

        add_row_layout.addWidget(new_keyword_input)
        add_row_layout.addWidget(add_btn)
        add_row_layout.addWidget(remove_btn)
        layout.addRow(add_row)

        return keywords_list

    def accept(self) -> None:
        if self.cpu_warning.value() >= self.cpu_critical.value():
            QMessageBox.warning(self, "Valores inválidos", "O aviso de CPU deve ser menor que o crítico.")
            return
        if self.memory_warning.value() >= self.memory_critical.value():
            QMessageBox.warning(self, "Valores inválidos", "O aviso de memória deve ser menor que o crítico.")
            return
        super().accept()

    @staticmethod
    def _percent_spinbox(value: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(1.0, 100.0)
        box.setDecimals(0)
        box.setSuffix(" %")
        box.setValue(value)
        return box

    def values(self) -> Dict[str, float]:
        return {
            'cpu_warning': self.cpu_warning.value(),
            'cpu_critical': self.cpu_critical.value(),
            'memory_warning': self.memory_warning.value(),
            'memory_critical': self.memory_critical.value(),
            'sustained_high_cpu_secs': self.sustained_samples.value(),
            'memory_leak_growth_mb': self.memory_leak_growth.value(),
            'memory_leak_min_samples': self.memory_leak_samples.value(),
        }

    def keywords(self) -> Tuple[List[str], List[str]]:
        critical = [self.critical_keywords_list.item(i).text() for i in range(self.critical_keywords_list.count())]
        warning = [self.warning_keywords_list.item(i).text() for i in range(self.warning_keywords_list.count())]
        return critical, warning
