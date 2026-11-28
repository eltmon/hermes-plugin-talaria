"""Deterministic model protocol peer; the Hermes conversation loop stays real."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time


def run(root, repo):
    calls = []
    class Peer(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'messages' not in request:
                data = json.dumps({'error': {'message': 'QA peer supports chat completions only'}}).encode()
                self.send_response(404)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            tool_messages = [m for m in request['messages'] if m['role'] == 'tool']
            step = len(tool_messages)
            result = json.loads(tool_messages[-1]['content']) if tool_messages else {}
            assert 'error' not in result, result
            if step == 0:
                name, args = 't3_list', {}
            elif step == 1:
                project = next(p for p in result['projects'] if p['title'] == 'Talaria QA')
                name, args = 't3_new_thread', {'project_id': project['id'], 'title': 'Hermes agent loop QA',
                    'model_selection': {'instance_id': 'qa', 'model': 'qa-model'}}
            elif step == 2:
                self.server.thread_id = result['threadId']
                name, args = 't3_prompt', {'thread_id': self.server.thread_id, 'text': 'QA_BASIC'}
            elif step == 3:
                name, args = 't3_wait', {'thread_id': self.server.thread_id, 'timeout': 15}
            elif step == 4:
                assert result['status'] == 'settled', result
                name, args = 't3_thread', {'thread_id': self.server.thread_id}
            else:
                assert step == 5 and any('TALARIA_QA_DONE' in t.get('agent_output', '') for t in result['turns']), result
                name, args = None, None
            message = {'role': 'assistant', 'content': None}
            if name:
                calls.append(name)
                message['tool_calls'] = [{'id': f'qa_{step}', 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(args)}}]
            else:
                message['content'] = 'HERMES_TALARIA_E2E_VERIFIED'
            body = {'id': f'qa-{step}', 'object': 'chat.completion', 'created': int(time.time()),
                'model': 'qa-model', 'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if name else 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}}
            if request.get('stream'):
                delta = dict(message)
                if name:
                    delta['tool_calls'] = [{**t, 'index': i} for i, t in enumerate(message['tool_calls'])]
                chunk = {**body, 'object': 'chat.completion.chunk', 'choices': [
                    {'index': 0, 'delta': delta, 'finish_reason': None}]}
                finish = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if name else 'stop'}]}
                data = ('data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(finish) + '\n\ndata: [DONE]\n\n').encode()
                content_type = 'text/event-stream'
            else:
                data, content_type = json.dumps(body).encode(), 'application/json'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Peer)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()
    import os
    os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
    from run_agent import AIAgent
    agent = None
    try:
        agent = AIAgent(base_url=f'http://127.0.0.1:{server.server_port}/v1', api_key='disposable-qa-only',
            provider='openai', api_mode='chat_completions', model='qa-model', max_iterations=8,
            enabled_toolsets=['t3code'], quiet_mode=True, save_trajectories=False,
            skip_context_files=True, skip_memory=True, skip_background_review=True,
            session_id='qa-agent-loop', run_budget_seconds=40, cwd=str(root / 'work'))
        outcome = agent.run_conversation('Run the Talaria QA task and report its result.')
        assert 'HERMES_TALARIA_E2E_VERIFIED' in outcome.get('final_response', ''), str(outcome)[-2000:]
        assert calls == ['t3_list', 't3_new_thread', 't3_prompt', 't3_wait', 't3_thread'], calls
        report = {'stage': 'agent-loop', 'tools': calls, 'final_response': outcome['final_response'],
                  'model_peer': 'deterministic loopback HTTP/SSE fixture'}
        (root / 'results/agent-loop.json').write_text(json.dumps(report))
        print(json.dumps(report), flush=True)
    finally:
        if agent:
            agent.close()
        server.shutdown()
        server.server_close()
