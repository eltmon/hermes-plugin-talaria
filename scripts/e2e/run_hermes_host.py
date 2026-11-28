from prepare import *
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

env = environment()
log = ARTIFACTS / ('hermes-host-' + ('-'.join(sys.argv[1:]) or 'discovery') + '.log')
with log.open('w', encoding='utf-8') as output:
    process = subprocess.Popen([str(HERMES_PYTHON), '-I', str(SCRIPTS / 'hermes_host.py'), *sys.argv[1:]],
        env=env, cwd=str(ROOT / 'work'), stdout=output, stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        code = process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
        raise
print(log.read_text(encoding='utf-8')[-6500:])
sys.exit(code)
