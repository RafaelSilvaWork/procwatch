"""Detecta processos com janela visível no Windows - a diferença entre um
'aplicativo aberto' (o que o usuário reconheceria: Chrome, Bloco de Notas,
etc.) e um processo de fundo comum (serviço, helper, tarefa do sistema)."""

import logging
from typing import Set

import win32gui
import win32process

logger = logging.getLogger(__name__)


def get_pids_with_visible_window() -> Set[int]:
    """PIDs que possuem ao menos uma janela top-level visível e com título."""
    pids: Set[int] = set()

    def _callback(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                pids.add(pid)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        logger.exception("Erro ao enumerar janelas")

    return pids
