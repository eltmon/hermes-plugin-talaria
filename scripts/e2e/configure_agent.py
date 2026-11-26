from prepare import *
import shutil

shutil.copyfile(SCRIPTS / 'acp_agent.py', ROOT / 'acp_agent.py')
settings_path = ROOT / 't3/userdata/settings.json'
settings = json.loads(settings_path.read_text())
settings['providerInstances']['qa'] = {'driver': 'acpRegistry', 'enabled': True,
    'displayName': 'Talaria QA deterministic agent', 'config': {'source': 'local',
    'commandPath': str(CONTROLLER_PYTHON), 'commandArgs': [str(ROOT / 'acp_agent.py')]}}
settings_path.write_text(json.dumps(settings), encoding='utf-8')
print('Configured disposable local ACP agent; no model credentials.')
