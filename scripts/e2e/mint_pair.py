from prepare import *

result = t3(['auth', 'pairing', 'create', '--base-dir', str(ROOT / 't3'),
    '--scope', 'orchestration:read', '--scope', 'orchestration:operate',
    '--scope', 'filesystem:read', '--scope', 'filesystem:write',
    '--ttl', '1h', '--label', 'Talaria disposable QA', '--json'])
if result.returncode:
    raise RuntimeError(result.stderr[-1200:])
data = json.loads(result.stdout)
(ARTIFACTS / 'pairing.private.json').write_text(json.dumps(data), encoding='utf-8')
print(json.dumps({'pairing_created': True, 'fields': list(data)}))
