from prepare import *
import httpx


def restart():
    record_path = ARTIFACTS / 'host.private.json'
    record = json.loads(record_path.read_text())
    base = Path(record['base_dir']).resolve()
    assert base == (ROOT / 't3').resolve()
    query = f'Get-CimInstance Win32_Process -Filter "ProcessId = {int(record["pid"])}" | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress'
    identity = json.loads(subprocess.check_output(['powershell', '-NoProfile', '-Command', query], text=True))
    assert Path(identity['ExecutablePath']).resolve() == T3_EXE.resolve()
    assert str(base) in identity['CommandLine'] and '--base-dir' in identity['CommandLine']
    subprocess.run(['taskkill', '/PID', str(record['pid']), '/T', '/F'], capture_output=True, check=True)
    env = environment()
    env.update(ELECTRON_RUN_AS_NODE='1', DO_NOT_TRACK='1', GIT_CONFIG_NOSYSTEM='1')
    with (ARTIFACTS / 't3-stdout.private.log').open('a') as out, (ARTIFACTS / 't3-stderr.private.log').open('a') as err:
        child = subprocess.Popen(record['command'], env=env, cwd=ROOT / 'work', stdout=out, stderr=err,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    record['pid'] = child.pid
    record_path.write_text(json.dumps(record))
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        assert child.poll() is None, 'T3 exited during restart'
        try:
            response = httpx.get(record['url'] + '/.well-known/t3/environment', timeout=1)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(.1)
    raise TimeoutError('Restart readiness deadline exceeded')
