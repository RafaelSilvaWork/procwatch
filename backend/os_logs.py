import logging

import win32evtlog

logger = logging.getLogger(__name__)


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
                
            # Tenta extrair a mensagem descritiva do evento
            try:
                msg = win32evtlog.GetEventLogMessage(event)
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