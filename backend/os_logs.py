import logging

import win32evtlog
import win32evtlogutil

logger = logging.getLogger(__name__)

# Fontes que indicam uma falha real de funcionamento (crash reportado pelo
# Windows Error Reporting, exceção não tratada do .NET, dependência/
# manifesto ausente via SideBySide) - não qualquer entrada genérica que um
# app escreva por conta própria no log de Aplicativo. É a diferença entre
# "algo crucial quebrou" e ruído.
CRASH_RELATED_SOURCES = {
    "application error", "application hang", "windows error reporting",
    ".net runtime", "sidebyside", "side-by-side",
}


def buscar_erros_criticos_do_processo(nome_executavel, desde=None, log_type="Application", num_registros=200):
    """Procura no Visualizador de Eventos do Windows por falhas REAIS de um
    executável específico - crash, travamento reportado pelo Windows,
    dependência ausente - e não qualquer log genérico que mencione o nome
    do processo. `desde` (datetime), se informado, ignora eventos
    anteriores a esse horário - evita re-alertar sobre uma falha antiga,
    de antes da execução atual ter começado.

    Retorna uma lista de dicts (origem/data/mensagem), mais recente primeiro.
    """
    nome_lower = nome_executavel.lower()
    encontrados = []

    server = 'localhost'
    try:
        hand = win32evtlog.OpenEventLog(server, log_type)
        flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
        events = win32evtlog.ReadEventLog(hand, flags, 0)

        for event in events:
            if len(encontrados) >= num_registros:
                break
            if event.EventType != win32evtlog.EVENTLOG_ERROR_TYPE:
                continue
            if (event.SourceName or "").lower() not in CRASH_RELATED_SOURCES:
                continue

            # Leitura é EVENTLOG_BACKWARDS_READ (mais recente primeiro) -
            # assim que um evento é mais antigo que o corte, todo o resto
            # também será, então dá pra parar em vez de continuar lendo.
            if desde is not None and event.TimeGenerated < desde:
                break

            try:
                msg = win32evtlogutil.SafeFormatMessage(event, log_type)
            except Exception:
                msg = ""
            msg = str(msg).strip() if msg else ""

            if nome_lower not in msg.lower():
                continue

            encontrados.append({
                "origem": event.SourceName,
                "data": str(event.TimeGenerated),
                "mensagem": msg or "Sem descrição",
            })

        win32evtlog.CloseEventLog(hand)
    except Exception:
        logger.exception("Erro ao correlacionar eventos do Windows para %s", nome_executavel)

    return encontrados


def buscar_logs_sistema(log_type="Application", num_registros=20):
    """
    Busca os últimos registros de log do Windows (Application ou System).
    """
    server = 'localhost' # Computador local
    try:
        # Abre o log de eventos do Windows
        hand = win32evtlog.OpenEventLog(server, log_type)
        flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
        
        eventos = []
        # Lê os eventos em lotes
        events = win32evtlog.ReadEventLog(hand, flags, 0)
        
        contador = 0
        for event in events:
            if contador >= num_registros:
                break
            
            # Filtramos por tipos relevantes (ex: Erros ou Avisos se necessário, ou trazemos geral)
            # event.EventType pode ser win32evtlog.EVENTLOG_ERROR_TYPE, etc.
            
            tipo_str = "Informação"
            if event.EventType == win32evtlog.EVENTLOG_ERROR_TYPE:
                tipo_str = "ERRO"
            elif event.EventType == win32evtlog.EVENTLOG_WARNING_TYPE:
                tipo_str = "AVISO"
                
            # Tenta extrair a mensagem descritiva do evento. SafeFormatMessage
            # (ao contrário de FormatMessage) já não levanta exceção sozinha
            # em caso de DLL de mensagem ausente/evento binário - o
            # try/except aqui é só uma segunda rede de segurança.
            try:
                msg = win32evtlogutil.SafeFormatMessage(event, log_type)
            except Exception:
                msg = "Mensagem indisponível ou binária."

            evento_info = {
                "origem": event.SourceName,
                "tipo": tipo_str,
                "data": str(event.TimeGenerated),
                "mensagem": str(msg).strip() if msg else "Sem descrição"
            }
            eventos.append(evento_info)
            contador += 1
            
        win32evtlog.CloseEventLog(hand)
        return eventos

    except Exception:
        logger.exception("Erro ao ler logs do Windows (log_type=%s)", log_type)
        return []