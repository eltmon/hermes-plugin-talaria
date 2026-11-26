"""Actual Hermes imports/discovery, launched with a scrubbed disposable home."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare import ROOT, REPO, ARTIFACTS, SCRIPTS, HERMES_SOURCE as SOURCE
sys.path.insert(0, str(SOURCE))
from hermes_constants import get_hermes_home
assert Path(get_hermes_home()).resolve() == (ROOT / 'h').resolve()
print(json.dumps({'stage': 'isolated_home', 'home': str(get_hermes_home())}), flush=True)

if sys.argv[1:] == ['doctor']:
    from hermes_cli.plugin_dev import doctor_plugin
    report = doctor_plugin(REPO)
    print(json.dumps({'stage': 'doctor', 'report': str(report)}, default=str), flush=True)
    assert not report.findings, report.findings
else:
    destination = ROOT / 'h/plugins/t3code'
    destination.mkdir(parents=True, exist_ok=True)
    files = subprocess.run(['git', '-C', str(REPO), 'ls-files', '-z'],
        capture_output=True, check=True).stdout.decode().split('\0')
    for name in filter(None, files):
        if name == '__init__.py' or name == 'plugin.yaml' or name.startswith('talaria/'):
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / name, target)
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    manager = get_plugin_manager()
    loaded = manager._plugins.get('t3code')
    result = {'stage': 'discovery', 'present': loaded is not None,
              'enabled': loaded.enabled if loaded else None,
              'error': loaded.error if loaded else None,
              'tools': loaded.tools_registered if loaded else []}
    print(json.dumps(result, default=str), flush=True)
    assert loaded and loaded.enabled and not loaded.error and len(loaded.tools_registered) == 14, result
    if sys.argv[1:] == ['pair']:
        import argparse
        private = ARTIFACTS
        pair = json.loads((private / 'pairing.private.json').read_text())
        host = json.loads((private / 'host.private.json').read_text())
        args = argparse.Namespace(t3code_command='login',
            url=host['url'] + '/pair#token=' + pair['credential'], name='qa')
        code = manager._cli_commands['t3code']['handler_fn'](args)
        print(json.dumps({'stage': 'pair', 'exit': code}), flush=True)
        sys.exit(code)
    if sys.argv[1:] == ['files']:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
        from model_tools import handle_function_call
        def call(name, args):
            result = json.loads(handle_function_call(name, args, session_id='qa-a',
                conversation_id='qa-a', enabled_tools=[name], enabled_toolsets=['t3code']))
            return result
        listing = call('t3_list', {})
        environments = call('t3_environments', {})
        assert 'error' not in environments, environments
        if 'error' in listing:
            print(json.dumps({'stage': 'list', 'result': listing}), flush=True)
            sys.exit(1)
        project = next(p for p in listing['projects'] if p['title'] == 'Talaria QA')
        print(json.dumps({'stage': 'list', 'project_count': len(listing['projects'])}), flush=True)
        (ROOT / 'work/input.txt').write_text('talaria real transport input', encoding='utf-8')
        read = call('t3_read_file', {'project_id': project['id'], 'path': 'input.txt'})
        write = call('t3_write_file', {'project_id': project['id'], 'path': 'output.txt', 'contents': 'talaria output'})
        listing_files = call('t3_ls', {'project_id': project['id']})
        searched = call('t3_search', {'project_id': project['id'], 'query': 'talaria'})
        denied = call('t3_write_file', {'project_id': project['id'], 'path': '../escape.txt', 'contents': 'must not write'})
        assert 'error' in denied and not (ROOT / 'escape.txt').exists(), denied
        assert (ROOT / 'work/output.txt').read_text() == 'talaria output'
        report = {'stage': 'files', 'read': read, 'write': write, 'listing': listing_files,
                  'search': searched, 'traversal_rejected': True}
        print(json.dumps(report), flush=True)
        (ARTIFACTS / 'files.json').write_text(json.dumps(report), encoding='utf-8')
        assert all('error' not in r for r in (read, write, listing_files, searched)), report
    if sys.argv[1:] == ['tasks']:
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
        from model_tools import handle_function_call
        def call(name, args):
            result = json.loads(handle_function_call(name, args, session_id='qa-a',
                conversation_id='qa-a', enabled_tools=[name], enabled_toolsets=['t3code']))
            if 'error' in result:
                print(json.dumps({'stage': name, 'result': result}), flush=True)
                raise RuntimeError('Public tool failed: ' + name)
            return result
        listing = call('t3_list', {})
        project = next(p for p in listing['projects'] if p['title'] == 'Talaria QA')
        created = call('t3_new_thread', {'project_id': project['id'], 'title': 'Talaria QA basic',
            'model_selection': {'instance_id': 'qa', 'model': 'qa-model'}})
        thread = created['threadId']
        prompted = call('t3_prompt', {'thread_id': thread, 'text': 'QA_BASIC'})
        print(json.dumps({'stage': 'prompted', 'result': prompted}), flush=True)
        waited = call('t3_wait', {'thread_id': thread, 'timeout': 20, 'interval': 2})
        detail = call('t3_thread', {'thread_id': thread})
        report = {'stage': 'tasks', 'thread_id': thread, 'wait': waited, 'detail': detail}
        print(json.dumps(report), flush=True)
        (ARTIFACTS / 'tasks.json').write_text(json.dumps(report), encoding='utf-8')
        assert waited['status'] == 'settled', waited
        assert any('TALARIA_QA_DONE' in turn.get('agent_output', '') for turn in detail['turns']), detail
    if sys.argv[1:] in (['routing'], ['reconnect']):
        import asyncio
        import time
        from hermes_cli.env_loader import load_hermes_dotenv
        from gateway.session_context import scoped_session_key
        load_hermes_dotenv()
        from model_tools import handle_function_call
        delivered = []
        def receiver(*, session_key, content, plugin_id):
            if session_key not in ('qa:a', 'qa:b'):
                return False
            delivered.append({'session': session_key, 'content': content})
            return True
        manager.set_tui_message_injector('talaria-qa', receiver)
        def call(name, args, route='qa:a'):
            with scoped_session_key(route):
                result = json.loads(handle_function_call(name, args, session_id=route,
                    conversation_id=route, enabled_tools=[name], enabled_toolsets=['t3code']))
            if 'error' in result:
                raise RuntimeError(name + ': ' + result['error'])
            return result
        listing = call('t3_list', {})
        project = next(p for p in listing['projects'] if p['title'] == 'Talaria QA')
        thread = call('t3_new_thread', {'project_id': project['id'], 'title': 'Talaria QA routing',
            'model_selection': {'instance_id': 'qa', 'model': 'qa-model'}})['threadId']
        for route in ('qa:a', 'qa:b'):
            call('t3_watch', {'kind': 'thread', 'thread_id': thread}, route)
        call('t3_prompt', {'thread_id': thread, 'text': 'QA_BASIC'})
        call('t3_wait', {'thread_id': thread, 'timeout': 15})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and {r['session'] for r in delivered} != {'qa:a', 'qa:b'}:
            time.sleep(.05)
        assert {r['session'] for r in delivered} == {'qa:a', 'qa:b'}, delivered
        first_count = len(delivered)
        call('t3_unwatch', {'kind': 'thread', 'thread_id': thread}, 'qa:a')
        if sys.argv[1:] == ['reconnect']:
            sys.path.insert(0, str(SCRIPTS))
            from restart_t3 import restart
            restart()
            time.sleep(3)
        checkpoint = len(delivered)
        call('t3_prompt', {'thread_id': thread, 'text': 'QA_BASIC'})
        call('t3_wait', {'thread_id': thread, 'timeout': 15})
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not delivered[checkpoint:]:
            time.sleep(.05)
        later = delivered[checkpoint:]
        assert len(later) == 1 and later[0]['session'] == 'qa:b', delivered
        call('t3_unwatch', {'kind': 'thread', 'thread_id': thread}, 'qa:b')
        report = {'stage': sys.argv[1], 'delivered': delivered,
                  'initial_count': first_count, 'later_count': len(later), 'independent_unwatch': True}
        (ARTIFACTS / (sys.argv[1] + '.json')).write_text(json.dumps(report), encoding='utf-8')
        print(json.dumps(report), flush=True)
    if sys.argv[1:] == ['controls']:
        import time
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
        from model_tools import handle_function_call
        def call(name, args):
            result = json.loads(handle_function_call(name, args, session_id='qa-a',
                conversation_id='qa-a', enabled_tools=[name], enabled_toolsets=['t3code']))
            if 'error' in result:
                print(json.dumps({'stage': name, 'result': result}), flush=True)
                raise RuntimeError(name + ' failed')
            return result
        project = next(p for p in call('t3_list', {})['projects'] if p['title'] == 'Talaria QA')
        report = {'stage': 'controls', 'checks': []}
        def create(title):
            return call('t3_new_thread', {'project_id': project['id'], 'title': title,
                'model_selection': {'instance_id': 'qa', 'model': 'qa-model'}})['threadId']
        marker = ROOT / 'work/approved-marker.txt'
        thread = create('Talaria QA repeated approvals')
        request_ids = set()
        for decision, expected in [('accept', 'QA_APPROVED'), ('decline', 'QA_DECLINED')]:
            marker.unlink(missing_ok=True)
            call('t3_prompt', {'thread_id': thread, 'text': 'QA_APPROVAL'})
            waiting = call('t3_wait', {'thread_id': thread, 'timeout': 15})
            assert waiting['status'] == 'approval', waiting
            row = next(t for t in call('t3_list', {})['threads'] if t['id'] == thread)
            pending = row['pendingRequest']
            assert pending['id'] not in request_ids, pending
            request_ids.add(pending['id'])
            print(json.dumps({'stage': 'pending_approval', 'kind': pending['kind'], 'decision': decision}), flush=True)
            call('t3_respond', {'thread_id': thread, 'kind': 'approval',
                'request_id': pending['id'], 'decision': decision})
            done = call('t3_wait', {'thread_id': thread, 'timeout': 15})
            detail = call('t3_thread', {'thread_id': thread})
            assert done['status'] == 'settled', done
            assert any(expected in turn.get('agent_output', '') for turn in detail['turns']), detail
            assert marker.exists() == (decision == 'accept')
            report['checks'].append({'decision': decision, 'result': expected, 'marker_exists': marker.exists()})
        thread = create('Talaria QA interrupt')
        call('t3_prompt', {'thread_id': thread, 'text': 'QA_INTERRUPT'})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            detail = call('t3_thread', {'thread_id': thread})
            if any('QA_WAITING_FOR_INTERRUPT' in turn.get('agent_output', '') for turn in detail['turns']):
                break
            time.sleep(.1)
        else:
            raise AssertionError('Agent did not reach interrupt checkpoint')
        timed_out = call('t3_wait', {'thread_id': thread, 'timeout': 0})
        assert timed_out['status'] == 'timeout', timed_out
        report['checks'].append({'wait_timeout': timed_out['status']})
        call('t3_interrupt', {'thread_id': thread})
        interrupted = call('t3_wait', {'thread_id': thread, 'timeout': 15})
        assert interrupted['latestTurn']['state'] == 'interrupted', interrupted
        report['checks'].append({'interrupt': interrupted['latestTurn']['state']})
        (ARTIFACTS / 'controls.json').write_text(json.dumps(report), encoding='utf-8')
        print(json.dumps(report), flush=True)

    if sys.argv[1:] == ['agent-loop']:
        sys.path.insert(0, str(SCRIPTS))
        from hermes_agent_turn import run
        run(ROOT, REPO)

    if sys.argv[1:] == ['auth-failure']:
        import argparse
        from hermes_cli.env_loader import load_hermes_dotenv
        load_hermes_dotenv()
        from model_tools import handle_function_call
        denied = json.loads(handle_function_call('t3_list', {}, enabled_tools=['t3_list'], enabled_toolsets=['t3code']))
        assert 'error' in denied and 'login' in json.dumps(denied), denied
        code = manager._cli_commands['t3code']['handler_fn'](argparse.Namespace(t3code_command='logout', env='qa'))
        assert code == 0
        secret_file = ROOT / 'h/.env'
        assert not any(line.startswith('T3CODE_TOKEN_QA=') and line.split('=',1)[1].strip().strip('"' + "'")
                       for line in secret_file.read_text().splitlines())
        report = {'stage': 'auth-failure', 'revoked_token_rejected': True, 'logout_removed_secret': True}
        (ARTIFACTS / 'auth-failure.json').write_text(json.dumps(report))
        print(json.dumps(report), flush=True)
