"""Constantes compartilhadas entre as abas da janela principal."""

from desktop.theme import ALERT_COLORS, COLOR_ACCENT, COLOR_TEXT_BRIGHT

_NUMERIC_COLUMNS = {0, 2, 3, 4}  # PID, CPU %, Memória (MB), Memória %
_HISTORY_MAX_POINTS = 150

# Paleta de identificação de processo no log combinado - reaproveita tokens
# já existentes (nenhuma cor nova). Usada só como identificador visual por
# processo (rotação por ordem de adição), não como indicador de severidade -
# é o mesmo padrão de "uma cor por stream" usado por viewers de log
# multi-fonte (ex.: docker compose logs).
_LOG_PREFIX_PALETTE = [
    COLOR_TEXT_BRIGHT, COLOR_ACCENT, ALERT_COLORS["INFO"],
    ALERT_COLORS["ERROR"], ALERT_COLORS["CRITICAL"],
]
