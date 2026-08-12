"""Gráfico leve de histórico (CPU % / Memória %), desenhado manualmente
com QPainter - sem depender de nenhuma biblioteca de gráficos."""

from collections import deque
from typing import Deque

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import QWidget

from desktop.theme import COLOR_ACCENT, COLOR_PANEL_BG, COLOR_TEXT_BRIGHT

_GRID_COLOR = "#333333"


class HistoryChartWidget(QWidget):
    """Desenha CPU% e Memória% dos últimos N pontos numa escala fixa 0-100,
    já que ambos os valores já vêm como porcentagem."""

    def __init__(self, max_points: int = 150, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self._max_points = max_points
        self._cpu_history: Deque[float] = deque(maxlen=max_points)
        self._mem_history: Deque[float] = deque(maxlen=max_points)

    def add_point(self, cpu_percent: float, memory_percent: float) -> None:
        self._cpu_history.append(max(0.0, min(cpu_percent, 100.0)))
        self._mem_history.append(max(0.0, min(memory_percent, 100.0)))
        self.update()

    def clear_history(self) -> None:
        self._cpu_history.clear()
        self._mem_history.clear()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(COLOR_PANEL_BG))

        w, h = self.width(), self.height()
        margin_top = 18  # espaço pra legenda

        painter.setPen(QPen(QColor(_GRID_COLOR), 1))
        for frac in (0.25, 0.5, 0.75, 1.0):
            y = margin_top + (h - margin_top) * (1 - frac)
            painter.drawLine(0, int(y), w, int(y))

        self._draw_series(painter, self._cpu_history, QColor(COLOR_TEXT_BRIGHT), margin_top)
        self._draw_series(painter, self._mem_history, QColor(COLOR_ACCENT), margin_top)

        painter.setPen(QColor(COLOR_TEXT_BRIGHT))
        painter.drawText(4, 12, "— CPU %")
        painter.setPen(QColor(COLOR_ACCENT))
        painter.drawText(70, 12, "— Memória %")

    def _draw_series(self, painter: QPainter, history: Deque[float], color: QColor, margin_top: int) -> None:
        if len(history) < 2:
            return

        w, h = self.width(), self.height() - margin_top
        n = len(history)
        step = w / max(n - 1, 1)

        points = [
            QPointF(i * step, margin_top + h - (value / 100.0) * h)
            for i, value in enumerate(history)
        ]
        painter.setPen(QPen(color, 2))
        for p1, p2 in zip(points, points[1:]):
            painter.drawLine(p1, p2)
