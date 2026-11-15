import asyncio
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .browser import BrowserPool, GatewayError
from .providers import PROVIDERS
from .store import Store
from .paths import default_data_dir


class AccountCreate(BaseModel):
    provider: str
    name: str = Field(min_length=1, max_length=100)


class BrowserAttach(BaseModel):
    port: int = Field(ge=1024, le=65535)


class KeyCreate(BaseModel):
    name: str = Field(default='Minha integração', min_length=1, max_length=100)


class Message(BaseModel):
    role: Literal['system', 'developer', 'user', 'assistant']
    content: str | list[dict]


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra='allow')
    model: str
    messages: list[Message] = Field(min_length=1, max_length=200)
    stream: bool = False
    stream_options: dict | None = None


def error_body(message, code, kind='invalid_request_error', param=None):
    return {'error': {'message': message, 'type': kind, 'param': param, 'code': code}}


def model_info(pid):
    return {'id': PROVIDERS[pid].model_id, 'object': 'model', 'created': 0, 'owned_by': pid}


def resolve_model(value):
    for provider in PROVIDERS.values():
        if value in (provider.id, provider.model_id, provider.id + '/web'):
            return provider.id
    raise GatewayError('Modelo desconhecido. Consulte GET /v1/models.', 'model_not_found', 404)


def content_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if part.get('type') not in ('text', 'input_text', 'output_text') or not isinstance(part.get('text'), str):
            raise GatewayError('Somente conteúdo de texto é suportado.', 'unsupported_content', 400)
        parts.append(part['text'])
    return '\n'.join(parts)


def validate_options(body, allowed=()):
    # max_output_tokens is accepted for client compatibility, without imposing a web limit.
    # Reject other API controls that the web UI cannot reliably apply.
    ignored = {'model', 'messages', 'stream', 'stream_options', 'input', 'instructions', 'prompt', 'max_output_tokens', *allowed}
    for key, value in body.items():
        if key not in ignored and value is not None:
            raise GatewayError(f'Parâmetro {key!r} não suportado pelos adaptadores web.', 'unsupported_parameter', 400)
    options = body.get('stream_options')
    if options is not None and (not isinstance(options, dict) or set(options) - {'include_usage'} or ('include_usage' in options and not isinstance(options['include_usage'], bool))):
        raise GatewayError('stream_options suporta apenas include_usage.', 'unsupported_parameter', 400)


def format_prompt(messages):
    if messages[-1].role != 'user':
        raise GatewayError('A última mensagem deve ter role=user.', 'invalid_messages', 400)
    if len(messages) == 1:
        result = content_text(messages[0].content)
    else:
        result = ('Continue a conversa abaixo. As instruções e o histórico foram enviados como texto; '
                  'responda apenas à última mensagem do usuário.\n\n' +
                  '\n\n'.join(f'[{m.role}]\n{content_text(m.content)}' for m in messages))
    if not result.strip() or len(result) > 100000:
        raise GatewayError('O prompt deve ter entre 1 e 100000 caracteres.', 'invalid_prompt', 400)
    return result


def sse(data, event=None):
    return (('event: ' + event + '\n') if event else '') + 'data: ' + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)) + '\n\n'


def create_app(data_dir=None, pool_factory=BrowserPool):
    root = Path(data_dir or default_data_dir()).resolve()
    store = Store(root)
    pool = pool_factory(store)
    admin_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        await pool.shutdown()

    app = FastAPI(title='UltraRoute', version='0.1.0', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store, app.state.pool = store, pool
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]'])

    @app.middleware('http')
    async def local_admin(request: Request, call_next):
        path = request.url.path
        if path == '/' or path.startswith('/admin/'):
            if request.client and request.client.host not in ('127.0.0.1', '::1', 'testclient'):
                return JSONResponse(error_body('Painel disponível apenas nesta máquina.', 'local_only'), 403)
            if path.startswith('/admin/'):
                if not secrets.compare_digest(request.cookies.get('ur_admin', ''), admin_token):
                    return JSONResponse(error_body('Abra o painel para iniciar uma sessão.', 'unauthorized'), 401)
                origin = request.headers.get('origin')
                if origin and origin != str(request.base_url).rstrip('/'):
                    return JSONResponse(error_body('Origem não permitida.', 'invalid_origin'), 403)
            if request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse(error_body('Origem não permitida.', 'invalid_origin'), 403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(GatewayError)
    async def gateway_error(request, exc):
        kind = 'invalid_request_error' if exc.status < 500 else 'server_error'
        return JSONResponse(error_body(exc.message, exc.code, kind), exc.status,
                            headers={'Retry-After': '5'} if exc.status == 429 else None)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(error_body('Corpo inválido. Confira campos e tipos na documentação.', 'invalid_request'), 400)

    async def authenticate(request: Request):
        auth = request.headers.get('authorization', '')
        if not auth.startswith('Bearer ') or not store.valid_key(auth[7:]):
            raise GatewayError('Chave de API inválida. Crie uma chave no painel.', 'invalid_api_key', 401)

    @app.get('/')
    async def index():
        response = FileResponse(Path(__file__).parent / 'static/index.html')
        response.set_cookie('ur_admin', admin_token, httponly=True, samesite='strict', max_age=86400)
        return response

    app.mount('/static', StaticFiles(directory=Path(__file__).parent / 'static'), name='static')

    @app.get('/health')
    async def health():
        return {'status': 'ok', 'version': '0.1.0'}

    @app.get('/admin/state')
    async def state():
        return {'providers': [{'id': p.id, 'name': p.name, 'url': p.url, 'color': p.color,
                               'model': p.model_id, 'adapter_status': 'experimental'} for p in PROVIDERS.values()],
                'accounts': [{**a, 'browser_name': (store.browser_session(a['id']) or {}).get('browser_name'), 'automation_connected': pool.is_connected(a['id'])} for a in store.accounts()],
                'browser_runtime': await pool.launcher.inventory(),
                'keys': store.keys(), 'stats': store.stats(),
                'api_base': 'http://127.0.0.1:8989/v1'}

    @app.post('/admin/accounts', status_code=201)
    async def add_account(body: AccountCreate):
        if body.provider not in PROVIDERS:
            raise GatewayError('Provedor desconhecido.', 'invalid_provider', 400)
        return store.add_account(body.provider, body.name.strip() or PROVIDERS[body.provider].name)

    def get_account(aid):
        account = store.account(aid)
        if not account:
            raise GatewayError('Conta não encontrada.', 'account_not_found', 404)
        return account

    @app.post('/admin/accounts/{aid}/login')
    async def login(aid: str):
        return await pool.open_login(get_account(aid))

    @app.post('/admin/accounts/{aid}/attach')
    async def attach(aid: str, body: BrowserAttach):
        return await pool.attach(get_account(aid), body.port)

    @app.post('/admin/accounts/{aid}/confirm')
    async def confirm(aid: str):
        return await pool.confirm(get_account(aid))

    @app.post('/admin/accounts/{aid}/disconnect')
    async def disconnect(aid: str):
        get_account(aid)
        await pool.close(aid)
        return {'status': 'disconnected'}

    @app.delete('/admin/accounts/{aid}')
    async def remove_account(aid: str):
        get_account(aid)
        await pool.remove(aid)
        return {'deleted': True, 'id': aid}

    @app.post('/admin/keys', status_code=201)
    async def create_key(body: KeyCreate):
        return store.create_key(body.name)

    @app.delete('/admin/keys/{kid}')
    async def revoke(kid: str):
        store.revoke(kid)
        return {'deleted': True, 'id': kid}

    @app.get('/v1/models', dependencies=[Depends(authenticate)])
    async def models():
        return {'object': 'list', 'data': [model_info(p) for p in PROVIDERS]}

    @app.get('/v1/models/{model_id:path}', dependencies=[Depends(authenticate)])
    async def model(model_id: str):
        return model_info(resolve_model(model_id))

    async def execute(pid, prompt, stream, mode='chat', include_usage=False):
        reservation = pool.reserve(pid)
        account = await reservation.__aenter__()
        generation = pool.generate(account, prompt)
        released = False

        async def release():
            nonlocal released
            if not released:
                released = True
                await reservation.__aexit__(None, None, None)

        try:
            # Preflight first output before sending HTTP 200 / SSE headers.
            first = await anext(generation)
        except BaseException:
            await generation.aclose()
            await release()
            raise
        rid = ('resp_' if mode == 'responses' else 'chatcmpl-' if mode == 'chat' else 'cmpl-') + uuid.uuid4().hex
        created = int(time.time())
        model = PROVIDERS[pid].model_id
        item_id = 'msg_' + uuid.uuid4().hex

        async def pieces():
            try:
                yield first
                async for chunk in generation:
                    yield chunk
            finally:
                await generation.aclose()
                await release()

        def response_object(text, status='completed'):
            return {'id': rid, 'object': 'response', 'created_at': created, 'status': status,
                    'model': model, 'error': None, 'incomplete_details': None,
                    'output': [{'id': item_id, 'type': 'message', 'status': status,
                                'role': 'assistant', 'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}] if text else [],
                    'parallel_tool_calls': False, 'tool_choice': 'none', 'tools': [],
                    'usage': None, 'store': False}

        if not stream:
            text = ''.join([chunk async for chunk in pieces()])
            if mode == 'responses':
                return response_object(text)
            choice = {'index': 0, 'finish_reason': 'stop', 'logprobs': None}
            choice.update({'message': {'role': 'assistant', 'content': text}} if mode == 'chat' else {'text': text})
            return {'id': rid, 'object': 'chat.completion' if mode == 'chat' else 'text_completion',
                    'created': created, 'model': model, 'choices': [choice], 'usage': None}

        async def events():
            iterator = pieces()
            text = ''
            seq = 0

            def revent(kind, **values):
                nonlocal seq
                payload = {'type': kind, 'sequence_number': seq, **values}
                seq += 1
                return sse(payload, kind)

            def chat_chunk(delta=None, finish=None, empty=False):
                choice = {'index': 0, 'finish_reason': finish, 'logprobs': None}
                choice.update({'delta': delta or {}} if mode == 'chat' else {'text': delta or ''})
                return {'id': rid, 'object': 'chat.completion.chunk' if mode == 'chat' else 'text_completion',
                        'created': created, 'model': model, 'choices': [] if empty else [choice]}

            try:
                if mode == 'responses':
                    yield revent('response.created', response=response_object('', 'in_progress'))
                    yield revent('response.in_progress', response=response_object('', 'in_progress'))
                    yield revent('response.output_item.added', output_index=0,
                                 item={'id': item_id, 'type': 'message', 'status': 'in_progress', 'role': 'assistant', 'content': []})
                    yield revent('response.content_part.added', item_id=item_id, output_index=0, content_index=0,
                                 part={'type': 'output_text', 'text': '', 'annotations': []})
                elif mode == 'chat':
                    yield sse(chat_chunk({'role': 'assistant', 'content': ''}))
                async for chunk in iterator:
                    text += chunk
                    if mode == 'responses':
                        yield revent('response.output_text.delta', item_id=item_id, output_index=0, content_index=0, delta=chunk)
                    else:
                        yield sse(chat_chunk({'content': chunk} if mode == 'chat' else chunk))
                if mode == 'responses':
                    full = response_object(text)
                    yield revent('response.output_text.done', item_id=item_id, output_index=0, content_index=0, text=text)
                    yield revent('response.content_part.done', item_id=item_id, output_index=0, content_index=0, part=full['output'][0]['content'][0])
                    yield revent('response.output_item.done', output_index=0, item=full['output'][0])
                    yield revent('response.completed', response=full)
                else:
                    yield sse(chat_chunk(finish='stop'))
                    if include_usage:
                        usage = chat_chunk(empty=True)
                        usage['usage'] = None  # Web providers do not expose token counts.
                        yield sse(usage)
                    yield sse('[DONE]')
            except GatewayError as exc:
                if mode == 'responses':
                    full = response_object(text, 'failed')
                    full['error'] = {'code': exc.code, 'message': exc.message}
                    yield revent('response.failed', response=full)
                else:
                    yield sse(error_body(exc.message, exc.code, 'server_error'))
                    yield sse('[DONE]')
            finally:
                await iterator.aclose()
                await generation.aclose()
                # Also covers a disconnect before the iterator was first advanced.
                await release()

        return StreamingResponse(events(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})

    @app.post('/v1/chat/completions', dependencies=[Depends(authenticate)])
    async def chat(body: ChatRequest):
        validate_options(body.model_dump())
        return await execute(resolve_model(body.model), format_prompt(body.messages), body.stream,
                             include_usage=bool((body.stream_options or {}).get('include_usage')))

    async def read_body(request):
        try:
            body = await request.json()
        except Exception as exc:
            raise GatewayError('JSON inválido.', 'invalid_json', 400) from exc
        if not isinstance(body, dict):
            raise GatewayError('Envie um objeto JSON.', 'invalid_request', 400)
        if not isinstance(body.get('model'), str):
            raise GatewayError('model é obrigatório.', 'invalid_request', 400)
        if not isinstance(body.get('stream', False), bool):
            raise GatewayError('stream deve ser booleano.', 'invalid_request', 400)
        return body

    @app.post('/v1/completions', dependencies=[Depends(authenticate)])
    async def completions(request: Request):
        body = await read_body(request)
        validate_options(body)
        if not isinstance(body.get('prompt'), str):
            raise GatewayError('prompt deve ser texto.', 'unsupported_content', 400)
        return await execute(resolve_model(body['model']), format_prompt([Message(role='user', content=body['prompt'])]),
                             body.get('stream', False), 'completion', bool((body.get('stream_options') or {}).get('include_usage')))

    @app.post('/v1/responses', dependencies=[Depends(authenticate)])
    async def responses(request: Request):
        body = await read_body(request)
        validate_options(body, allowed={'store'})
        if body.get('store') not in (None, False):
            raise GatewayError('Armazenamento e retomada de Responses não suportados. Use store=false.', 'unsupported_parameter', 400)
        messages = []
        if body.get('instructions'):
            if not isinstance(body['instructions'], str):
                raise GatewayError('instructions deve ser texto.', 'invalid_request', 400)
            messages.append(Message(role='system', content=body['instructions']))
        raw = body.get('input')
        if isinstance(raw, str):
            messages.append(Message(role='user', content=raw))
        elif isinstance(raw, list) and raw:
            for item in raw:
                if not isinstance(item, dict) or item.get('type', 'message') != 'message':
                    raise GatewayError('input aceita apenas mensagens de texto.', 'unsupported_content', 400)
                try:
                    messages.append(Message(role=item['role'], content=item['content']))
                except (KeyError, ValueError) as exc:
                    raise GatewayError('Mensagem inválida em input.', 'invalid_request', 400) from exc
        else:
            raise GatewayError('input é obrigatório e deve ser texto ou lista de mensagens.', 'invalid_request', 400)
        return await execute(resolve_model(body['model']), format_prompt(messages), body.get('stream', False), 'responses')

    @app.api_route('/v1/{path:path}', methods=['GET', 'POST', 'DELETE', 'PATCH', 'PUT'], dependencies=[Depends(authenticate)])
    async def unsupported(path: str):
        raise GatewayError('Endpoint não suportado pelos provedores web. Consulte o README para a matriz de compatibilidade.', 'unsupported_endpoint', 501)

    return app
