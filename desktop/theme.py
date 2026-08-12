"""Tema visual centralizado do LogWatch (dark)."""

COLOR_BG = "#121212"
COLOR_PANEL_BG = "#1e1e1e"
COLOR_TEXT = "#00aa00"
COLOR_TEXT_BRIGHT = "#00ff00"
COLOR_ACCENT = "#ffaa00"

COLOR_ALERT_CRITICAL = "#ff4444"
COLOR_ALERT_ERROR = "#ff6644"
COLOR_ALERT_WARNING = "#ffaa00"
COLOR_ALERT_INFO = "#44aaff"

ALERT_COLORS = {
    "CRITICAL": COLOR_ALERT_CRITICAL,
    "ERROR": COLOR_ALERT_ERROR,
    "WARNING": COLOR_ALERT_WARNING,
    "INFO": COLOR_ALERT_INFO,
}

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {COLOR_BG};
    color: {COLOR_TEXT};
    font-family: Consolas;
}}

QTabBar::tab {{
    background: {COLOR_PANEL_BG};
    color: {COLOR_TEXT};
    padding: 6px 12px;
}}

QTabBar::tab:selected {{
    background: #2a2a2a;
    color: {COLOR_TEXT_BRIGHT};
}}

QPushButton {{
    background-color: #2a2a2a;
    color: {COLOR_TEXT_BRIGHT};
    border: 1px solid #444444;
    padding: 5px 10px;
}}

QPushButton:hover {{
    background-color: #333333;
}}

QTableWidget, QTextEdit {{
    background-color: {COLOR_PANEL_BG};
    color: {COLOR_TEXT_BRIGHT};
    gridline-color: #333333;
    font-family: Consolas;
}}

QComboBox, QSpinBox, QDoubleSpinBox, QLineEdit {{
    background-color: {COLOR_PANEL_BG};
    color: {COLOR_TEXT_BRIGHT};
    border: 1px solid #444444;
    padding: 2px;
}}

QStatusBar {{
    background-color: {COLOR_PANEL_BG};
    color: {COLOR_TEXT};
}}

QTextEdit#alertsLog {{
    background-color: #1a1a1a;
    color: #ffffff;
    font-size: 10pt;
}}
"""
