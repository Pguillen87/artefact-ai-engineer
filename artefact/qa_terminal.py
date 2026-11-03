"""Host-only QA terminal for the real Artefact conversation agent."""
import argparse
from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time
import uuid

from .codex_local import CodexLocalProvider, CodexQuota
from .conversation import ConversationAgent
from .data import StructuralError, load_dataset


_SESSION_ID = re.compile(r'[A-Za-z0-9._-]{1,80}')
_SAFE_ERRORS = {
    'approval_required': 'Confirme o uso da cota local com CODEX_DEMO_APPROVED=1.',
    'missing_config': 'Configuração obrigatória da CLI QA está ausente.',
    'invalid_config': 'Limite de chamadas ou modelo configurado é inválido.',
    'unsafe_state_path': 'O banco de quota precisa ficar fora do checkout e dos CSVs.',
    'state_path_unavailable': 'O diretório do banco persistente de quota não está disponível.',
    'codex_cli_missing': 'Codex CLI não foi encontrado no PATH deste processo.',
    'data_directory_missing': 'O diretório de dados autorizado não existe.',
    'data_baseline_invalid': 'Os seis CSVs não passaram na validação do baseline.',
    'state_unavailable': 'Não foi possível abrir o contador persistente de chamadas.',
    'invalid_session': 'ID de sessão inválido.',
    'invalid_message': 'A mensagem deve conter entre 1 e 2.000 caracteres.',
    'invalid_json': 'A linha não contém JSON válido.',
    'invalid_request': 'Use exatamente os campos session_id e message.',
    'model_unavailable': 'O modelo configurado não está disponível para esta CLI/conta.',
    'authentication_required': 'A sessão do Codex CLI não está autenticada.',
    'quota_limit': 'O limite persistente local de chamadas foi atingido.',
    'quota_exhausted': 'O serviço Codex recusou mais chamadas por limite de uso.',
    'codex_timeout': 'A chamada Codex excedeu o timeout configurado.',
    'codex_cli_missing': 'Codex CLI não foi encontrado no PATH deste processo.',
    'network_error': 'A CLI não conseguiu alcançar o serviço Codex.',
    'invalid_cli_output': 'A CLI retornou uma resposta que não passou no schema.',
    'codex_cli_failed': 'A chamada do Codex CLI falhou; veja o código de erro sanitizado.',
    'tool_failed': 'A ferramenta local não concluiu a consulta com segurança.',
    'agent_failed': 'O agente não concluiu a consulta; veja o código de erro sanitizado.',
    'provider_failed': 'A solicitação ao provider não foi concluída.',
    'data_snapshot_error': 'O snapshot de dados não pôde ser carregado com segurança.',
    'invalid_tool_plan': 'O plano de ferramentas não passou na validação local.',
    'tool_isolation': 'A ferramenta de escrita precisa ser enviada separadamente.',
    'render_failed': 'A resposta segura não pôde ser montada.',
}


class QATerminalConfigError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(_SAFE_ERRORS.get(code, 'Configuração local inválida.'))


@dataclass(frozen=True)
class QATerminalConfig:
    data_dir: Path
    state_path: Path
    max_calls: int
    model: str


def read_config(env):
    if env.get('CODEX_DEMO_APPROVED') != '1':
        raise QATerminalConfigError('approval_required')
    names = ('ARTEFACT_DATA_DIR', 'CODEX_DEMO_STATE_PATH', 'CODEX_DEMO_MAX_CALLS', 'CODEX_DEMO_MODEL')
    if any(not env.get(name) for name in names):
        raise QATerminalConfigError('missing_config')
    try:
        max_calls = int(env['CODEX_DEMO_MAX_CALLS'])
        model = env['CODEX_DEMO_MODEL']
        if max_calls <= 0 or not re.fullmatch(r'[A-Za-z0-9._-]{1,80}', model):
            raise ValueError()
        data_dir = Path(env['ARTEFACT_DATA_DIR']).expanduser().resolve()
        state_path = Path(env['CODEX_DEMO_STATE_PATH']).expanduser().resolve()
    except (TypeError, ValueError, OSError):
        raise QATerminalConfigError('invalid_config') from None
    checkout = Path(__file__).resolve().parents[1]
    if (state_path == data_dir or data_dir in state_path.parents or
            state_path == checkout or checkout in state_path.parents):
        raise QATerminalConfigError('unsafe_state_path')
    if not state_path.parent.is_dir():
        raise QATerminalConfigError('state_path_unavailable')
    return QATerminalConfig(data_dir, state_path, max_calls, model)


def build_runtime(config, *, runner=None):
    if not config.data_dir.is_dir():
        raise QATerminalConfigError('data_directory_missing')
    executable = shutil.which('codex.exe') if os.name == 'nt' else shutil.which('codex')
    if not executable:
        raise QATerminalConfigError('codex_cli_missing')
    try:
        snapshot = load_dataset(config.data_dir, verify_baseline=True)
    except (StructuralError, OSError, ValueError):
        raise QATerminalConfigError('data_baseline_invalid') from None
    try:
        quota = CodexQuota(config.state_path, config.max_calls)
    except (OSError, ValueError, sqlite3.Error):
        raise QATerminalConfigError('state_unavailable') from None
    provider = CodexLocalProvider(executable, quota, runner=runner, model=config.model)
    return ConversationAgent(snapshot, provider), quota


class TerminalSessionRunner:
    """Route each session ID into ConversationAgent's bounded per-chat history."""
    def __init__(self, agent, quota, *, model, max_sessions=100):
        self.agent = agent
        self.quota = quota
        self.model = model
        self.max_sessions = max_sessions
        self.sessions = OrderedDict()

    def _touch(self, session_id):
        self.sessions[session_id] = None
        self.sessions.move_to_end(session_id)
        while len(self.sessions) > self.max_sessions:
            expired, _ = self.sessions.popitem(last=False)
            self.agent.context.pop(expired, None)
            self.agent.history.pop(expired, None)

    def ask(self, session_id, message):
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            return {'status': 'error', 'error_code': 'invalid_session', 'message': _SAFE_ERRORS['invalid_session']}
        if not isinstance(message, str) or not message.strip() or len(message) > 2000:
            return {'status': 'error', 'error_code': 'invalid_message', 'message': _SAFE_ERRORS['invalid_message']}
        self._touch(session_id)
        started = time.monotonic()
        try:
            before = self.quota.usage()
            response = self.agent.answer(message, chat_id=session_id)
            after = self.quota.usage()
        except (OSError, sqlite3.Error):
            return {'status': 'error', 'session_id': session_id, 'model': self.model,
                    'error_code': 'state_unavailable', 'message': _SAFE_ERRORS['state_unavailable']}
        trace = getattr(self.agent, 'last_trace', {}) or {}
        status = trace.get('status', 'agent_failed')
        tools_selected = trace.get('selected_tools', [])
        tools_executed = trace.get('executed_tools', [])
        common = {
            'session_id': session_id,
            'model': self.model,
            'calls_reserved_total': after,
            'calls_reserved_this_turn': max(0, after - before),
            'inference_response_received': bool(trace.get('model_call')),
            'tools_selected': tools_selected,
            'tools_executed': tools_executed,
            'tool_details': [dict(item) for item in trace.get('tool_details', [])[:3]
                             if isinstance(item, dict) and isinstance(item.get('name'), str)],
            'latency_ms': round((time.monotonic() - started) * 1000),
        }
        if status == 'error':
            code = trace.get('error_code') or 'agent_failed'
            return {'status': 'error', **common, 'error_code': code,
                    'message': _SAFE_ERRORS.get(code, _SAFE_ERRORS['agent_failed'])}
        if status in {'tool_error', 'uncertain'}:
            code = trace.get('error_code') or 'tool_failed'
            return {'status': status, **common, 'error_code': code,
                    'message': _SAFE_ERRORS.get(code, _SAFE_ERRORS['tool_failed']), 'response': response}
        return {'status': status, **common, 'response': response}


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def run_batch(input_stream, output_stream, terminal):
    errors = 0
    for line_number, line in enumerate(input_stream, 1):
        if not line.strip():
            continue
        try:
            request = json.loads(line, object_pairs_hook=_json_object)
        except (json.JSONDecodeError, ValueError):
            result = {'status': 'error', 'error_code': 'invalid_json',
                      'message': _SAFE_ERRORS['invalid_json'], 'line': line_number}
        else:
            if (not isinstance(request, dict) or set(request) != {'session_id', 'message'}):
                result = {'status': 'error', 'error_code': 'invalid_request',
                          'message': _SAFE_ERRORS['invalid_request'], 'line': line_number}
            else:
                result = terminal.ask(request['session_id'], request['message'])
        if result.get('status') == 'error':
            errors += 1
            if 'line' not in result:
                result['line'] = line_number
        output_stream.write(json.dumps(result, ensure_ascii=True, separators=(',', ':')) + '\n')
        output_stream.flush()
    return errors


def _interactive(terminal, initial_session_id):
    session_id = initial_session_id or ('qa-' + uuid.uuid4().hex)
    print(f'Sessão QA: {session_id}. Use /session ID, /new ou /quit.', flush=True)
    while True:
        try:
            message = input('Você: ')
        except EOFError:
            return 0
        command = message.strip()
        if command in {'/quit', '/exit'}:
            return 0
        if command == '/new':
            session_id = 'qa-' + uuid.uuid4().hex
            print(f'Nova sessão QA: {session_id}', flush=True)
            continue
        if command.startswith('/session '):
            candidate = command[len('/session '):].strip()
            if not _SESSION_ID.fullmatch(candidate):
                print(_SAFE_ERRORS['invalid_session'], file=sys.stderr, flush=True)
                continue
            session_id = candidate
            print(f'Sessão QA: {session_id}', flush=True)
            continue
        if not command:
            continue
        result = terminal.ask(session_id, message)
        if result.get('status') == 'error':
            print(f"ERRO [{result['error_code']}]: {result['message']}", file=sys.stderr, flush=True)
        elif result.get('status') in {'tool_error', 'uncertain'}:
            print(f"ERRO [{result['error_code']}]: {result['message']}", file=sys.stderr, flush=True)
            print(result.get('response', ''), flush=True)
        else:
            print(result.get('response', ''), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Converse com o agente Artefact real pelo terminal local.')
    parser.add_argument('--batch-jsonl', action='store_true', help='Leia JSONL de stdin e escreva JSONL em stdout.')
    parser.add_argument('--session-id', help='ID da sessão interativa; no modo lote, use o ID em cada linha.')
    args = parser.parse_args(argv)
    try:
        config = read_config(os.environ)
        agent, quota = build_runtime(config)
    except QATerminalConfigError as exc:
        print(f'ERRO [{exc.code}]: {exc}', file=sys.stderr)
        return 2
    terminal = TerminalSessionRunner(agent, quota, model=config.model)
    if args.batch_jsonl:
        return 1 if run_batch(sys.stdin, sys.stdout, terminal) else 0
    return _interactive(terminal, args.session_id)


if __name__ == '__main__':
    raise SystemExit(main())
