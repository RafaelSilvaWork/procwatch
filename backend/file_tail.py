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


def monitorar_stream(stream, callback, stop_event=None):
    """Lê linhas de um stream (stdout/stderr de um subprocesso) em tempo
    real, até o stream fechar (processo encerrou) ou stop_event ser setado.

    Ao contrário de monitorar_arquivo, não precisa de polling: readline()
    bloqueia até chegar uma linha nova ou o pipe fechar.
    """
    try:
        for linha in iter(stream.readline, ""):
            if stop_event is not None and stop_event.is_set():
                break
            callback(linha.rstrip("\n"))
    except (ValueError, OSError):
        pass  # stream fechado enquanto líamos (processo encerrado abruptamente)
    finally:
        try:
            stream.close()
        except Exception:
            pass