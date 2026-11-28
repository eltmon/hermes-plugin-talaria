from prepare import *
import httpx

env = environment()
ARTIFACTS.mkdir(parents=True, exist_ok=True)
env['ELECTRON_RUN_AS_NODE'] = '1'
env['DO_NOT_TRACK'] = '1'
env['GIT_CONFIG_NOSYSTEM'] = '1'
with socket.socket() as reservation:
    reservation.bind(('127.0.0.1', 0))
    port = reservation.getsockname()[1]
base = ROOT / 't3'
(base / 'userdata').mkdir(exist_ok=True)
settings = {'providerInstances': {name: {'driver': name, 'enabled': False, 'config': {}}
    for name in ('codex', 'claudeAgent', 'cursor', 'grok', 'fx', 'opencode', 'antigravity', 'kiro', 'pi', 'muse')}}
# The Claude instance's driver name differs from its instance slug.
settings['providerInstances']['claudeAgent']['driver'] = 'claudeAgent'
(base / 'userdata/settings.json').write_text(json.dumps(settings), encoding='utf-8')
subprocess.run(['git', 'init', str(ROOT / 'work')], env=env, capture_output=True, check=True)
stdout = (ARTIFACTS / 't3-stdout.private.log').open('w', encoding='utf-8')
stderr = (ARTIFACTS / 't3-stderr.private.log').open('w', encoding='utf-8')
command = [str(T3_EXE), str(T3_ENTRY), 'serve', '--base-dir', str(base),
           '--host', '127.0.0.1', '--port', str(port), '--no-browser', str(ROOT / 'work')]
process = subprocess.Popen(command, env=env, cwd=ROOT / 'work', stdout=stdout,
    stderr=stderr, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
record = {'pid': process.pid, 'port': port, 'base_dir': str(base),
          'url': f'http://127.0.0.1:{port}', 'command': command}
(ARTIFACTS / 'host.private.json').write_text(json.dumps(record), encoding='utf-8')
deadline = time.monotonic() + 60
try:
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f'T3 exited with code {process.returncode}; inspect private logs')
        try:
            descriptor = httpx.get(record['url'] + '/.well-known/t3/environment', timeout=2).json()
            if 'environmentId' in descriptor:
                record['descriptor'] = descriptor
                (ARTIFACTS / 'host.private.json').write_text(json.dumps(record), encoding='utf-8')
                print(json.dumps({'ready': True, 'pid': process.pid, 'port': port,
                    'server_version': descriptor.get('serverVersion'),
                    'protocol': descriptor.get('orchestrationProtocolVersion')}))
                break
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(.2)
    else:
        raise TimeoutError('T3 readiness deadline exceeded')
except BaseException:
    process.terminate()
    process.wait(timeout=10)
    raise
