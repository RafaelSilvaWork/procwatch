import logging
import time
import os

logger = logging.getLogger(__name__)


def monitorar_arquivo(caminho_arquivo, callback, stop_event=None):
    """
    Lê novas linhas adicionadas a um arquivo de log em tempo real.

    stop_event: threading.Event opcional - quando setado, encerra o loop
    (sem ele, monitora para sempre, como antes).
    """
    if not os.path.exists(caminho_arquivo):
        logger.warning("Arquivo não encontrado: %s", caminho_arquivo)
        return

    with open(caminho_arquivo, "r", encoding="utf-8", errors="ignore") as f:
        # Vai para o final do arquivo para ler apenas os novos registros
        f.seek(0, os.SEEK_END)

        while stop_event is None or not stop_event.is_set():
            linha = f.readline()
            if not linha:
                time.sleep(0.5) # Aguarda novas entradas
                continue

            # Envia a linha lida para a função de tratamento (callback)
            callback(linha.strip())