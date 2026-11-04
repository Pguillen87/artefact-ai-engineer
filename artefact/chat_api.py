"""Authenticated private HTTP interface for the chat service."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
import threading
import time
from contextlib import closing
from pathlib import Path

from .session_store import ChatError, ChatService, SessionStore
from .bootstrap_secret import bootstrap_secret
from .migrations import ensure_schema

_ERRORS = {'session_not_found':404,'dataset_missing':409,'provider_missing':409,
           'dataset_unavailable':409,'provider_unavailable':409,'invalid_message':400,'agent_unavailable':503,'budget_exhausted':429}

def handler_for(service, token, *, provider_test=None):
    if not token: raise ValueError('service token required')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args): pass
        def reply(self, status, value):
            raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw))); self.send_header('Cache-Control','no-store'); self.end_headers()
            self.wfile.write(raw)
        def handle_request(self):
            supplied=self.headers.get('Authorization','')
            if not hmac.compare_digest(supplied, 'Bearer '+token): return self.reply(401,{'error':'unauthorized'})
            parts=self.path.split('?')[0].strip('/').split('/')
            try:
                if self.command=='GET' and parts==['health']: return self.reply(200,{'status':'ok'})
                if self.command=='GET' and parts==['sessions']: return self.reply(200,{'sessions':service.list()})
                if self.command=='POST' and parts==['sessions']: return self.reply(201,service.create())
                if self.command=='POST' and parts==['provider-test'] and provider_test:
                    length=int(self.headers.get('Content-Length','0'))
                    if length<1 or length>8192: raise ChatError('invalid_message')
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict) or body.get('provider') not in ('google','openai'): raise ChatError('invalid_message')
                    try: return self.reply(200,provider_test(body['provider']))
                    except Exception: return self.reply(503,{'error':'provider_test_failed'})
                if len(parts)==2 and parts[0]=='sessions':
                    if self.command=='GET': return self.reply(200,service.get(parts[1]))
                    if self.command=='DELETE': service.delete(parts[1]); return self.reply(200,{'deleted':True})
                if self.command=='POST' and len(parts)==3 and parts[0]=='sessions' and parts[2]=='messages':
                    length=int(self.headers.get('Content-Length','0'))
                    if length<1 or length>8192: raise ChatError('invalid_message')
                    body=json.loads(self.rfile.read(length))
                    if not isinstance(body,dict): raise ChatError('invalid_message')
                    return self.reply(200,service.send(parts[1],body.get('message')))
                return self.reply(404,{'error':'not_found'})
            except ChatError as exc: return self.reply(_ERRORS.get(exc.code,500),{'error':exc.code})
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError): return self.reply(400,{'error':'invalid_request'})
            except Exception: return self.reply(500,{'error':'internal_error'})
        do_GET=handle_request
        do_POST=handle_request
        do_DELETE=handle_request
    return Handler

class TelegramChat:
    def __init__(self, service):
        self.service = service
        with closing(service.store.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            ensure_schema(db,'telegram_chat',1,{'telegram_sessions':'CREATE TABLE IF NOT EXISTS telegram_sessions (chat_id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE)'})

    def answer(self, text, chat_id=None):
        try:
            with closing(self.service.store.connect()) as db:
                row = db.execute('SELECT session_id FROM telegram_sessions WHERE chat_id=?', (chat_id,)).fetchone()
            if row is None:
                sid = self.service.create()['id']
                with closing(self.service.store.connect()) as db, db:
                    db.execute('INSERT OR REPLACE INTO telegram_sessions VALUES (?,?)', (chat_id, sid))
            else: sid = row[0]
            return self.service.send(sid, text)['answer']
        except ChatError as exc:
            return {'dataset_missing':'Importe e ative um dataset antes de conversar.',
                    'provider_missing':'Configure um provedor de IA antes de conversar.',
                    'dataset_unavailable':'A versão dos dados desta conversa não está disponível.',
                    'provider_unavailable':'O provedor desta conversa não está disponível.'}.get(exc.code, 'Não consegui processar sua solicitação. Tente novamente.')

def poll_once(client, adapter, offset):
    from .telegram import TelegramError
    for update in client.get_updates(offset):
        if not isinstance(update,dict) or type(update.get('update_id')) is not int:
            raise TelegramError('Invalid update identifier')
        adapter.handle(update)
        offset=max(offset,update['update_id']+1)
    return offset

def configured_provider(config, name, model, state, factory):
    entry=config.status()['providers'].get(name, {})
    if not entry.get('has_key') or not entry.get('verified') or entry.get('model') != model:
        raise ValueError('Provider and pinned model require verification')
    return factory(config,name,model,state)

def _run_telegram(service, state, token, allowed_id):
    from .agent import TelegramAdapter
    from .telegram import TelegramClient, TelegramError
    client = TelegramClient(token)
    adapter = TelegramAdapter(TelegramChat(service), client.send, state=state, allowed_chat_id=allowed_id)
    offset = 0
    while True:
        try:
            offset=poll_once(client,adapter,offset)
        except (TelegramError, OSError):
            time.sleep(3)

def main():
    from .dataset_store import DatasetStore
    from .provider_config import ProviderConfig
    from .provider import make_provider
    from .state import State
    from .review_client import ReviewClient
    datasets=DatasetStore(Path(os.environ.get('ARTEFACT_CATALOG_DB','/catalog/catalog.sqlite')))
    config=ProviderConfig(Path(os.environ.get('ARTEFACT_CONFIG_DB','/config/providers.sqlite')),Path(os.environ.get('ARTEFACT_MASTER_KEY_PATH','/secrets/master.key')))
    state=State(Path(os.environ.get('ARTEFACT_STATE_PATH','/state/agent.sqlite')))
    def active():
        status=config.status(); name=status['active']
        if not name: return None
        entry=status['providers'][name]
        return (name,entry['model']) if entry['has_key'] and entry.get('verified') else None
    token=bootstrap_secret(Path(os.environ.get('ARTEFACT_SECRET_DIR','/secrets'))/'service-token')
    review_submit=ReviewClient('http://admin:8080/internal/reviews',token)
    service=ChatService(SessionStore(Path(os.environ.get('ARTEFACT_CHAT_DB','/state/chat.sqlite'))),datasets.load,
                        lambda name,model: configured_provider(config,name,model,state,make_provider),datasets.active_version,active,review_submit=review_submit)
    telegram_token=os.environ.get('TELEGRAM_BOT_TOKEN')
    if telegram_token:
        allowed=int(os.environ['TELEGRAM_ALLOWED_CHAT_ID'])
        if allowed <= 0: raise ValueError('Invalid Telegram allowed chat')
        threading.Thread(target=_run_telegram,args=(service,state,telegram_token,allowed),daemon=True).start()
    server=ThreadingHTTPServer((os.environ.get('ARTEFACT_AGENT_BIND','0.0.0.0'),int(os.environ.get('ARTEFACT_AGENT_PORT','8081'))),handler_for(service,token,provider_test=lambda name: config.test_connection(name,state)))
    server.serve_forever()

if __name__=='__main__': main()
