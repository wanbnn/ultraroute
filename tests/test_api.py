import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from ultraroute.api import create_app
from ultraroute.browser import BrowserPool, GatewayError
from ultraroute.store import Store


class FakePool(BrowserPool):
    async def generate(self, account, prompt):
        yield 'Olá'
        await asyncio.sleep(.01)
        yield ', mundo!'

    async def shutdown(self):
        pass


@pytest.fixture
def gateway(tmp_path):
    app = create_app(tmp_path, pool_factory=FakePool)
    with TestClient(app, base_url='http://localhost') as client:
        client.get('/')
        key = client.post('/admin/keys', json={'name': 'test'}).json()['key']
        headers = {'Authorization': f'Bearer {key}'}
        account = client.post('/admin/accounts', json={'provider': 'chatgpt', 'name': 'test'}).json()
        app.state.store.status(account['id'], 'ready')
        yield app, client, headers, account


def test_auth_keys_and_models(gateway):
    app, client, headers, _ = gateway
    assert client.get('/v1/models').status_code == 401
    response = client.get('/v1/models', headers=headers)
    assert response.status_code == 200
    models = response.json()['data']
    assert len(models) == 6
    assert client.get('/v1/models/chatgpt/web', headers=headers).json()['id'] == 'chatgpt/web'
    assert client.get('/v1/models/nonexistent', headers=headers).status_code == 404
    assert 'digest' not in json.dumps(client.get('/admin/state').json())
    keys = client.get('/admin/state').json()['keys']
    assert client.delete('/admin/keys/' + keys[0]['id']).status_code == 200
    assert client.get('/v1/models', headers=headers).status_code == 401


def test_local_admin_csrf(gateway):
    _, client, _, _ = gateway
    assert client.post('/admin/keys', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/', headers={'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.get('/', headers={'Host': 'evil.example'}).status_code == 400
    client.cookies.clear()
    assert client.get('/admin/state').status_code == 401


@pytest.mark.parametrize('endpoint,body,expected', [
    ('chat/completions', {'messages': [{'role': 'user', 'content': 'Hi'}]}, 'chat.completion'),
    ('completions', {'prompt': 'Hi'}, 'text_completion'),
    ('responses', {'input': 'Hi', 'store': False}, 'response'),
    ('responses', {'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hi'}]}]}, 'response'),
])
def test_nonstream(gateway, endpoint, body, expected):
    app, client, headers, account = gateway
    response = client.post('/v1/' + endpoint, headers=headers, json={'model': 'chatgpt/web', **body})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['object'] == expected
    assert 'Olá, mundo!' in response.text
    assert result['usage'] is None
    assert not app.state.pool.lock(account['id']).locked()


@pytest.mark.parametrize('endpoint,body', [
    ('chat/completions', {'messages': [{'role': 'user', 'content': 'Hi'}], 'stream_options': {'include_usage': True}}),
    ('completions', {'prompt': 'Hi'}),
    ('responses', {'input': 'Hi'}),
])
def test_stream_format(gateway, endpoint, body):
    app, client, headers, account = gateway
    response = client.post('/v1/' + endpoint, headers=headers, json={'model': 'chatgpt/web', 'stream': True, **body})
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert 'Olá' in response.text and ', mundo!' in response.text
    if endpoint == 'responses':
        assert 'event: response.output_text.delta' in response.text
        assert 'event: response.completed' in response.text
    else:
        assert 'data: [DONE]' in response.text
        assert '"finish_reason": "stop"' in response.text
    assert not app.state.pool.lock(account['id']).locked()


def test_errors_explicit_and_no_false_success(gateway):
    app, client, headers, account = gateway
    base = {'model': 'chatgpt/web', 'messages': [{'role': 'user', 'content': 'Hi'}]}
    assert client.post('/v1/chat/completions', headers=headers, json={**base, 'tools': []}).status_code == 200
    assert client.post('/v1/chat/completions', headers=headers, json={**base, 'temperature': .7}).status_code == 200
    assert client.post('/v1/embeddings', headers=headers, json={}).status_code == 501
    assert client.post('/v1/responses', headers=headers, json={'model':'chatgpt/web','input':'Hi','store':True}).json()['store'] is False
    assert client.post('/v1/responses', headers=headers, json=[]).status_code == 400
    assert client.post('/v1/chat/completions', headers=headers, json={**base,'messages':[]}).status_code == 400
    app.state.store.status(account['id'], 'disconnected')
    response = client.post('/v1/chat/completions', headers=headers, json={**base, 'stream': True})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'login_required'


@pytest.mark.asyncio
async def test_reservation_busy_and_release(tmp_path):
    store = Store(tmp_path)
    account = store.add_account('chatgpt', 'One')
    store.status(account['id'], 'ready')
    pool = FakePool(store)
    async with pool.reserve('chatgpt'):
        with pytest.raises(GatewayError) as error:
            async with pool.reserve('chatgpt'):
                pass
        assert error.value.status == 429
    assert not pool.lock(account['id']).locked()


def test_secrets_hashed_and_profiles_reset(tmp_path):
    store = Store(tmp_path)
    token = store.create_key('test')['key']
    assert store.valid_key(token)
    assert token.encode() not in store.path.read_bytes()
    account = store.add_account('qwen', 'Private')
    store.status(account['id'], 'ready')
    restarted = Store(tmp_path)
    assert restarted.account(account['id'])['status'] == 'disconnected'
    assert restarted.valid_key(token)


@pytest.mark.parametrize('endpoint,body,marker', [
    ('chat/completions', {'messages':[{'role':'user','content':'Hi'}]}, '"content": "first"'),
    ('completions', {'prompt':'Hi'}, '"text": "first"'),
    ('responses', {'input':'Hi'}, '"delta": "first"'),
])
def test_real_network_incremental_delivery(tmp_path, endpoint, body, marker):
    finish = threading.Event()
    completed = threading.Event()

    class GatedPool(FakePool):
        async def generate(self, account, prompt):
            yield 'first'
            # Cannot finish until the HTTP client proves it received the first delta.
            while not finish.is_set():
                await asyncio.sleep(.01)
            yield 'second'
            completed.set()

    app = create_app(tmp_path, pool_factory=GatedPool)
    token = app.state.store.create_key('network')['key']
    account = app.state.store.add_account('chatgpt', 'Network')
    app.state.store.status(account['id'], 'ready')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(.01)
        with httpx.stream('POST', f'http://127.0.0.1:{port}/v1/{endpoint}',
                          headers={'Authorization': 'Bearer ' + token},
                          json={'model':'chatgpt/web','stream':True, **body}, timeout=5) as response:
            assert response.status_code == 200
            first_received = False
            all_lines = []
            for line in response.iter_lines():
                all_lines.append(line)
                if marker in line:
                    assert not completed.is_set(), 'Response was buffered until completion'
                    first_received = True
                    finish.set()
            assert first_received
            assert 'second' in '\n'.join(all_lines)
    finally:
        finish.set()
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


def test_openai_sdk_models_and_chat(gateway):
    from openai import OpenAI
    _, http_client, headers, _ = gateway
    sdk = OpenAI(base_url='http://localhost/v1', api_key=headers['Authorization'][7:], http_client=http_client)
    assert len(sdk.models.list().data) == 6
    assert sdk.models.retrieve('deepseek/web').id == 'deepseek/web'
    completion = sdk.chat.completions.create(model='chatgpt/web', messages=[{'role':'user','content':'Hi'}])
    assert completion.choices[0].message.content == 'Olá, mundo!'
    stream = sdk.chat.completions.create(model='chatgpt/web', messages=[{'role':'user','content':'Hi'}], stream=True)
    assert ''.join(c.choices[0].delta.content or '' for c in stream if c.choices) == 'Olá, mundo!'


def test_openai_sdk_responses(gateway):
    from openai import OpenAI
    _, http_client, headers, _ = gateway
    sdk = OpenAI(base_url='http://localhost/v1', api_key=headers['Authorization'][7:], http_client=http_client)
    response = sdk.responses.create(model='chatgpt/web', input='Hi', store=False)
    assert response.output_text == 'Olá, mundo!'
    with sdk.responses.stream(model='chatgpt/web', input='Hi', store=False) as stream:
        chunks = [e.delta for e in stream if e.type == 'response.output_text.delta']
        final = stream.get_final_response()
    assert ''.join(chunks) == 'Olá, mundo!'
    assert final.status == 'completed'


def test_cdp_input_validation(gateway):
    _, client, _, account = gateway
    assert client.post(f"/admin/accounts/{account['id']}/attach", json={'port':80}).status_code == 400
    assert client.post(f"/admin/accounts/{account['id']}/attach", json={'port':'https://evil.example'}).status_code == 400


def test_aistudio_model_catalog_account_and_protocols(gateway):
    from ultraroute.providers import PROVIDERS
    app, client, headers, _ = gateway
    model = 'aistudio/gemini-4-hydrogen'
    assert PROVIDERS['aistudio'].url == 'https://aistudio.google.com/prompts/new_chat?model=gemini-4-hydrogen'
    listing = client.get('/v1/models', headers=headers).json()['data']
    assert any(m['id'] == model and m['owned_by'] == 'aistudio' for m in listing)
    assert client.get('/v1/models/'+model, headers=headers).json()['id'] == model
    assert client.get('/v1/models/aistudio/web', headers=headers).json()['id'] == model
    assert client.get('/v1/models/aistudio/unknown-model', headers=headers).status_code == 404
    account = client.post('/admin/accounts', json={'provider':'aistudio','name':'Gemini'}).json()
    app.state.store.status(account['id'],'ready')
    for endpoint, body in [('chat/completions',{'messages':[{'role':'user','content':'Hi'}]}),
                           ('completions',{'prompt':'Hi'}), ('responses',{'input':'Hi'})]:
        response = client.post('/v1/'+endpoint, headers=headers, json={'model':model,**body})
        assert response.status_code == 200
        assert response.json()['model'] == model
        stream = client.post('/v1/'+endpoint, headers=headers,json={'model':model,'stream':True,**body})
        assert stream.status_code == 200
        assert model in stream.text and 'Olá' in stream.text
        assert ('response.completed' in stream.text) if endpoint=='responses' else ('[DONE]' in stream.text)
    state = client.get('/admin/state').json()
    assert next(p for p in state['providers'] if p['id']=='aistudio')['model'] == model


def test_upstream_permission_error_before_stream_has_http_error_and_unlocks(gateway, monkeypatch):
    app, client, headers, account = gateway
    async def denied(account,prompt):
        raise GatewayError('Google AI Studio: Permissão negada.','provider_permission_denied',403)
        yield ''
    monkeypatch.setattr(app.state.pool,'generate',denied)
    response=client.post('/v1/chat/completions',headers=headers,json={
        'model':'chatgpt/web','messages':[{'role':'user','content':'Hi'}],'stream':True})
    assert response.status_code==403
    assert response.json()['error']['code']=='provider_permission_denied'
    assert not app.state.pool.lock(account['id']).locked()


@pytest.mark.parametrize('endpoint,body', [
    ('chat/completions', {'messages':[{'role':'user','content':'Hi'}]}),
    ('completions', {'prompt':'Hi'}),
    ('responses', {'input':'Hi'}),
])
@pytest.mark.parametrize('stream', [False, True])
def test_max_output_tokens_accepted_without_truncation(gateway,endpoint,body,stream):
    _,client,headers,_=gateway
    response=client.post('/v1/'+endpoint,headers=headers,json={
        'model':'chatgpt/web','max_output_tokens':1,'stream':stream,**body})
    assert response.status_code==200, response.text
    if stream:
        events=[json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith('data: ') and line!='data: [DONE]']
        if endpoint=='responses':
            text=''.join(e['delta'] for e in events if e.get('type')=='response.output_text.delta')
        elif endpoint=='chat/completions':
            text=''.join(e['choices'][0]['delta'].get('content','') for e in events if e.get('choices'))
        else:
            text=''.join(e['choices'][0].get('text','') for e in events if e.get('choices'))
    else:
        result=response.json()
        if endpoint=='responses':text=result['output'][0]['content'][0]['text']
        elif endpoint=='chat/completions':text=result['choices'][0]['message']['content']
        else:text=result['choices'][0]['text']
    assert text=='Olá, mundo!'


@pytest.mark.parametrize('endpoint,body', [
    ('chat/completions', {'messages':[{'role':'user','content':'Hi'}]}),
    ('completions', {'prompt':'Hi'}),
    ('responses', {'input':'Hi'}),
])
@pytest.mark.parametrize('stream', [False, True])
def test_optional_client_parameters_ignored(gateway,endpoint,body,stream,monkeypatch):
    app,client,headers,_=gateway
    prompts=[]
    original=app.state.pool.generate
    async def capture(account,prompt):
        prompts.append(prompt)
        async for text in original(account,prompt):yield text
    monkeypatch.setattr(app.state.pool,'generate',capture)
    response=client.post('/v1/'+endpoint,headers=headers,json={
        'model':'chatgpt/web','stream':stream,**body,
        'reasoning':{'effort':'high','summary':'auto'},'reasoning_effort':'high',
        'temperature':0.5,'top_p':0.9,'max_tokens':1,'max_output_tokens':1,
        'max_completion_tokens':1,'n':2,'stop':['mundo'],
        'tools':[{'type':'function','function':{'name':'unused'}}],
        'tool_choice':'auto','response_format':{'type':'json_object'},
        'metadata':{'client':'test'},'store':True,
        'future_client_option':{'nested':[1,2,3]},
        'stream_options':{'include_usage':True,'future_stream_option':True}})
    assert response.status_code==200,response.text
    assert prompts==['Hi']
    assert 'Olá' in response.text and 'mundo!' in response.text
    assert 'unused' not in response.text
    if endpoint=='responses' and not stream:assert response.json()['store'] is False


@pytest.mark.parametrize('options', [None, 'unused', 42, ['unused']])
def test_irrelevant_stream_options_do_not_crash(gateway,options):
    _,client,headers,_=gateway
    for endpoint,body in [('chat/completions',{'messages':[{'role':'user','content':'Hi'}]}),('completions',{'prompt':'Hi'})]:
        response=client.post('/v1/'+endpoint,headers=headers,json={
            'model':'chatgpt/web',**body,'stream':True,'stream_options':options})
        assert response.status_code==200
        assert 'data: [DONE]' in response.text
