"""Opt-in Windows acceptance against installed Hermes and T3, using fresh homes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'hermes-source', 'hermes-python', 't3-exe', 't3-entry'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('This harness currently supports Windows process/profile isolation only.')
    if args.root.exists():
        parser.error('--root must be a new directory; existing profiles are refused.')
    for name in ('hermes_source', 'hermes_python', 't3_exe', 't3_entry'):
        # The T3 entry can be a virtual path inside Electron ASAR.
        if name != 't3_entry' and not getattr(args, name).exists():
            parser.error(f'{name} does not exist')
    args.root.mkdir(parents=True)
    for name, value in vars(args).items():
        os.environ['TALARIA_QA_' + name.upper()] = str(value.absolute())
    os.environ['TALARIA_QA_CONTROLLER_PYTHON'] = sys.executable
    from prepare import ROOT, REPO, ARTIFACTS, SCRIPTS, environment, stop_owned_server, t3
    environment()
    config = {'plugins': {'enabled': ['t3code'], 'isolation': 'in_process', 'entries': {
        't3code': {'allow_gateway_injection': True, 'settings': {'allow_gateway_injection': True}}}},
        'telemetry': {'enabled': False}}
    (ROOT / 'h/config.yaml').write_text(json.dumps(config))
    files = subprocess.check_output(['git', '-C', str(REPO), 'ls-files', '-z']).decode().split('\0')
    hashes = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
              for name in filter(None, files) if name in ('__init__.py', 'plugin.yaml', 'pyproject.toml')
              or name.startswith(('talaria/', 'scripts/e2e/'))}
    report = {'checks': [], 'payload_sha256': hashes, 'account_credentials_inherited': False,
              'model_peers': 'deterministic local HTTP/SSE and ACP fixtures', 'success': False}
    report['hermes_source_commit'] = subprocess.check_output(
        ['git', '-C', str(args.hermes_source), 'rev-parse', 'HEAD'], text=True).strip()
    def script(name, *arguments, timeout=70):
        subprocess.run([sys.executable, str(SCRIPTS / name), *arguments],
                       env=environment(), check=True, timeout=timeout)
    def command(arguments):
        result = t3(arguments)
        if result.returncode:
            raise RuntimeError('T3 CLI command failed; no credentials printed')
        return result
    def revoke_all():
        rows = json.loads(command(['auth', 'session', 'list', '--base-dir', str(ROOT / 't3'), '--json']).stdout)
        for row in rows:
            command(['auth', 'session', 'revoke', row['sessionId'], '--base-dir', str(ROOT / 't3')])
    try:
        script('start_t3.py')
        descriptor = json.loads((ARTIFACTS / 'host.private.json').read_text())['descriptor']
        report['t3_version'] = descriptor.get('serverVersion')
        report['orchestration_protocol'] = descriptor.get('orchestrationProtocolVersion')
        command(['project', 'add', '--base-dir', str(ROOT / 't3'), '--title', 'Talaria QA', str(ROOT / 'work')])
        script('configure_agent.py')
        script('mint_pair.py')
        for mode in ('doctor', 'tool-discovery', 'pair', 'files', 'tasks', 'controls', 'routing', 'reconnect', 'agent-loop'):
            script('run_hermes_host.py', mode)
            report['checks'].append(mode)
        revoke_all()
        script('run_hermes_host.py', 'auth-failure')
        report['checks'].append('auth-failure')
        report['success'] = True
    finally:
        try:
            if (ARTIFACTS / 'host.private.json').exists():
                revoke_all()
        finally:
            stop_owned_server()
            report['server_stopped'] = True
            (ARTIFACTS / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps({'success': report['success'], 'checks': report['checks'], 'evidence': str(ARTIFACTS)}))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    main()
