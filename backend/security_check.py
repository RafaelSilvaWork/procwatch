"""Verificação simples de processos que se passam por processos do sistema
Windows rodando de um local diferente do esperado - sinal comum de malware
disfarçado (ex.: um "svchost.exe" falso rodando de AppData\\Temp)."""

import os

# Nomes de processos do sistema que só deveriam existir dentro de
# %WINDIR%\System32 ou %WINDIR%\SysWOW64.
SYSTEM_PROCESS_NAMES = {
    "svchost.exe", "explorer.exe", "winlogon.exe", "csrss.exe",
    "services.exe", "lsass.exe", "smss.exe", "wininit.exe",
    "spoolsv.exe", "dwm.exe", "taskhostw.exe", "conhost.exe",
    "rundll32.exe",
}

_WINDIR = os.environ.get("WINDIR", r"C:\Windows")
_SYSTEM32_DIR = os.path.normcase(os.path.join(_WINDIR, "System32"))
_SYSWOW64_DIR = os.path.normcase(os.path.join(_WINDIR, "SysWOW64"))
_WINDIR_DIR = os.path.normcase(_WINDIR)

# Diretório esperado por nome de processo. A grande maioria mora em
# System32/SysWOW64 - "explorer.exe" é a exceção clássica: sempre rodou
# direto de %WINDIR% (C:\Windows\explorer.exe), nunca de System32.
_EXPECTED_DIRS = {name: (_SYSTEM32_DIR, _SYSWOW64_DIR) for name in SYSTEM_PROCESS_NAMES}
_EXPECTED_DIRS["explorer.exe"] = (_WINDIR_DIR,)


def is_suspicious_path(name: str, exe_path: str) -> bool:
    """True se um processo com nome de sistema conhecido estiver rodando de
    fora do diretório esperado para esse nome. Compara o diretório exato
    (não apenas um prefixo) - senão "C:\\Windows\\Temp\\explorer.exe" passaria
    como legítimo só por começar com "C:\\Windows\\". Processos sem caminho
    legível (ex.: "System", PID 4, que não tem exe de verdade) nunca são
    considerados suspeitos."""
    if not name or not exe_path:
        return False

    expected_dirs = _EXPECTED_DIRS.get(name.lower())
    if expected_dirs is None:
        return False

    actual_dir = os.path.normcase(os.path.dirname(exe_path))
    return actual_dir not in expected_dirs
