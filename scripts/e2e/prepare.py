"""Runtime paths supplied by run.py; all mutable state belongs to ROOT."""
from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import time

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parents[1]
ROOT = Path(os.environ['TALARIA_QA_ROOT']).resolve()
ARTIFACTS = ROOT / 'results'
HERMES_SOURCE = Path(os.environ['TALARIA_QA_HERMES_SOURCE']).absolute()
HERMES_PYTHON = Path(os.environ['TALARIA_QA_HERMES_PYTHON']).absolute()
T3_EXE = Path(os.environ['TALARIA_QA_T3_EXE']).resolve()
T3_ENTRY = Path(os.environ['TALARIA_QA_T3_ENTRY']).resolve()
CONTROLLER_PYTHON = Path(os.environ['TALARIA_QA_CONTROLLER_PYTHON']).resolve()


def environment():
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
               'PROCESSOR_ARCHITECTURE', 'NUMBER_OF_PROCESSORS'}
    env = {k: v for k, v in os.environ.items()
           if k.upper() in allowed or k.startswith('TALARIA_QA_')}
    for folder in ('user', 'appdata', 'localappdata', 'temp', 'h', 'tools', 't3', 'work', 'results'):
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    env.update({
        'USERPROFILE': str(ROOT / 'user'), 'HOME': str(ROOT / 'user'),
        'APPDATA': str(ROOT / 'appdata'), 'LOCALAPPDATA': str(ROOT / 'localappdata'),
        'TEMP': str(ROOT / 'temp'), 'TMP': str(ROOT / 'temp'),
        'HERMES_HOME': str(ROOT / 'h'), 'HERMES_RUNTIME_DIR': str(ROOT / 'tools'),
        'HERMES_DISABLE_LAZY_INSTALLS': '1',
        'T3CODE_HOME': str(ROOT / 't3'), 'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONIOENCODING': 'utf-8', 'NO_COLOR': '1', 'CI': '1',
    })
    return env


def t3(args, *, timeout=30):
    env = environment()
    env['ELECTRON_RUN_AS_NODE'] = '1'
    return subprocess.run([str(T3_EXE), str(T3_ENTRY), *args], env=env,
        cwd=ROOT / 'work', capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=timeout,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def stop_owned_server():
    """Refuse to kill a process whose executable/profile do not match."""
    record_path = ARTIFACTS / 'host.private.json'
    if not record_path.exists():
        return
    record = json.loads(record_path.read_text())
    base = Path(record['base_dir']).resolve()
    assert base == (ROOT / 't3').resolve()
    query = f'Get-CimInstance Win32_Process -Filter "ProcessId = {int(record["pid"])}" | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress'
    raw = subprocess.check_output(['powershell', '-NoProfile', '-Command', query], text=True).strip()
    if not raw:
        return
    identity = json.loads(raw)
    assert Path(identity['ExecutablePath']).resolve() == T3_EXE
    assert str(base) in identity['CommandLine'] and '--base-dir' in identity['CommandLine']
    subprocess.run(['taskkill', '/PID', str(record['pid']), '/T', '/F'], capture_output=True, check=True)
