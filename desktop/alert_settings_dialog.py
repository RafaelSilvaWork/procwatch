"""Diálogo para configurar os limites (thresholds) de alerta."""

from typing import Dict

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QSpinBox,
)


class AlertSettingsDialog(QDialog):
    """Permite ajustar os thresholds usados pelo AlertEngine em tempo real."""

    def __init__(self, current_thresholds: Dict[str, float], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configurar Alertas")
        self.setModal(True)

        layout = QFormLayout(self)

        self.cpu_warning = self._percent_spinbox(current_thresholds['cpu_warning'])
        self.cpu_critical = self._percent_spinbox(current_thresholds['cpu_critical'])
        self.memory_warning = self._percent_spinbox(current_thresholds['memory_warning'])
        self.memory_critical = self._percent_spinbox(current_thresholds['memory_critical'])

        self.sustained_samples = QSpinBox()
        self.sustained_samples.setRange(2, 120)
        self.sustained_samples.setValue(int(current_thresholds['sustained_high_cpu_secs']))

        layout.addRow("CPU - aviso:", self.cpu_warning)
        layout.addRow("CPU - crítico:", self.cpu_critical)
        layout.addRow("Memória - aviso:", self.memory_warning)
        layout.addRow("Memória - crítico:", self.memory_critical)
        layout.addRow("CPU sustentada (nº de amostras):", self.sustained_samples)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

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
        }
