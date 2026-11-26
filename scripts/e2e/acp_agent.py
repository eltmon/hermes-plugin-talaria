"""Deterministic ACP peer for real-runtime acceptance; no model or network."""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

write_lock = threading.Lock()
sessions = {}
pending = {}
next_request = 1000

def send(message):
    with write_lock:
        print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)

def response(identifier, result):
    send({'id': identifier, 'result': result})

def message(session, text):
    send({'method': 'session/update', 'params': {'sessionId': session, 'update': {
        'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}})

def settings(session):
    return {'sessionId': session,
        'modes': {'currentModeId': 'default', 'availableModes': [
            {'id': 'default', 'name': 'Default'}, {'id': 'plan', 'name': 'Plan'}]},
        'models': {'currentModelId': 'qa-model', 'availableModels': [
            {'modelId': 'qa-model', 'name': 'QA deterministic agent'}]}}

def prompt(identifier, params):
    global next_request
    session = params['sessionId']
    text = '\n'.join(part.get('text', '') for part in params.get('prompt', []))
    stop = sessions[session]['stop']
    stop.clear()
    try:
        if 'QA_APPROVAL' in text:
            with write_lock:
                next_request += 1
                request_id = next_request
            event, box = threading.Event(), {}
            pending[request_id] = (event, box)
            send({'id': request_id, 'method': 'session/request_permission', 'params': {
                'sessionId': session, 'toolCall': {'toolCallId': 'qa-write',
                    'title': 'Write a disposable QA marker', 'kind': 'edit', 'status': 'pending',
                    'content': []}, 'options': [
                    {'optionId': 'allow', 'name': 'Allow once', 'kind': 'allow_once'},
                    {'optionId': 'deny', 'name': 'Reject', 'kind': 'reject_once'}]}})
            if not event.wait(45):
                raise TimeoutError('QA approval deadline')
            outcome = box.get('result', {}).get('outcome', {})
            allowed = outcome.get('outcome') == 'selected' and outcome.get('optionId') == 'allow'
            if allowed:
                (Path(sessions[session]['cwd']) / 'approved-marker.txt').write_text('approved', encoding='utf-8')
            message(session, 'QA_APPROVED' if allowed else 'QA_DECLINED')
        elif 'QA_INTERRUPT' in text:
            message(session, 'QA_WAITING_FOR_INTERRUPT')
            stop.wait(45)
        else:
            message(session, 'TALARIA_QA_DONE')
        response(identifier, {'stopReason': 'cancelled' if stop.is_set() else 'end_turn'})
    except Exception as exc:
        send({'id': identifier, 'error': {'code': -32603, 'message': str(exc)}})

for line in sys.stdin:
    try:
        request = json.loads(line)
        method, identifier = request.get('method'), request.get('id')
        params = request.get('params') or {}
        if method == 'initialize':
            response(identifier, {'protocolVersion': 1, 'agentInfo': {'name': 'talaria-qa', 'version': '1.0.0'},
                'agentCapabilities': {'loadSession': True}, 'authMethods': []})
        elif method in ('session/new', 'session/load', 'session/resume'):
            session = params.get('sessionId') or str(uuid.uuid4())
            sessions[session] = {'cwd': params.get('cwd', str(Path.cwd())), 'stop': threading.Event()}
            response(identifier, settings(session))
        elif method == 'session/prompt':
            threading.Thread(target=prompt, args=(identifier, params), daemon=True).start()
        elif method == 'session/cancel':
            sessions.get(params.get('sessionId'), {}).get('stop', threading.Event()).set()
            if identifier is not None:
                response(identifier, {})
        elif method in ('session/set_mode', 'session/set_model', 'session/set_config_option', 'authenticate'):
            response(identifier, {})
        elif method == 'session/list':
            response(identifier, {'sessions': []})
        elif method is not None and identifier is not None:
            send({'id': identifier, 'error': {'code': -32601, 'message': 'Unsupported QA method'}})
        elif identifier in pending:
            event, box = pending.pop(identifier)
            box.update(request)
            event.set()
    except Exception as exc:
        sys.stderr.write(type(exc).__name__ + ': ' + str(exc) + '\n')
