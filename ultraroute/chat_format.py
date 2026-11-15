"""Chat Completions transport for text-only web providers."""
import json
import uuid

from .errors import GatewayError


def tool_message(text, tools, choice):
    raw = text.strip()
    if raw.startswith('```') and raw.endswith('```'):
        raw = raw.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        message = json.loads(raw)
    except ValueError:
        message = None
    calls = message.get('tool_calls') if isinstance(message, dict) else None
    allowed = {t['function']['name'] for t in tools}
    forced = choice['function']['name'] if isinstance(choice, dict) else None
    if calls and choice != 'none':
        if not isinstance(calls, list) or not (message.get('content') is None or isinstance(message.get('content'), str)):
            raise GatewayError('Chamada de ferramenta inválida do provedor.', 'invalid_tool_call', 502)
        result = []
        for call in calls:
            try:
                function = call['function']
                name, arguments = function['name'], function['arguments']
                if name not in allowed or (forced and name != forced):
                    raise ValueError()
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if not isinstance(arguments, dict):
                    raise ValueError()
            except (KeyError, TypeError, ValueError) as exc:
                raise GatewayError('Chamada de ferramenta inválida do provedor.', 'invalid_tool_call', 502) from exc
            result.append({'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
                           'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}})
        return {'role': 'assistant', 'content': message.get('content'), 'tool_calls': result}
    if choice == 'required' or forced:
        raise GatewayError('O provedor não retornou a chamada de ferramenta exigida.', 'invalid_tool_call', 502)
    if isinstance(message, dict) and message.get('role') == 'assistant' and isinstance(message.get('content'), str):
        text = message['content']
    return {'role': 'assistant', 'content': text}
