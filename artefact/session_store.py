"""Durable private chat sessions; typed state is stored as JSON, never pickle."""
from collections.abc import Mapping
from contextlib import contextmanager, closing
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .context import (ConversationContext, CommercialBasis, CatalogScope, MonetaryValue,
                      PendingCatalogSearch, redact_contact_details)
from .conversation import ConversationAgent, local_only_request
from .chat_limits import MAX_ASSISTANT_ANSWER_CHARS, MAX_USER_MESSAGE_CHARS
from .migrations import ensure_schema

_TYPES = {c.__name__: c for c in (ConversationContext, CommercialBasis, CatalogScope,
                                  MonetaryValue, PendingCatalogSearch)}

class ChatError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)

def _encode(value):
    if is_dataclass(value):
        return {'$type': type(value).__name__, 'fields': {f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, Decimal): return {'$decimal': str(value)}
    if isinstance(value, date): return {'$date': value.isoformat()}
    if isinstance(value, (tuple, frozenset)):
        return {'$tuple' if isinstance(value, tuple) else '$set': [_encode(v) for v in value]}
    if isinstance(value, Mapping): return {k: _encode(v) for k, v in value.items()}
    if isinstance(value, list): return [_encode(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)): return value
    raise ValueError('unsupported context type')

def _decode(value):
    if isinstance(value, list): return [_decode(v) for v in value]
    if not isinstance(value, dict): return value
    if '$type' in value:
        cls = _TYPES[value['$type']]
        return cls(**{k: _decode(v) for k,v in value['fields'].items()})
    if '$decimal' in value: return Decimal(value['$decimal'])
    if '$date' in value: return date.fromisoformat(value['$date'])
    if '$tuple' in value: return tuple(_decode(v) for v in value['$tuple'])
    if '$set' in value: return frozenset(_decode(v) for v in value['$set'])
    return {k:_decode(v) for k,v in value.items()}

class SessionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / (self.path.name + '.locks')
        self.lock_dir.mkdir(exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            ensure_schema(db,'chat',1,{
                'chat_sessions':'CREATE TABLE IF NOT EXISTS chat_sessions (id TEXT PRIMARY KEY, dataset_version INTEGER NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, context TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)',
                'chat_messages':'CREATE TABLE IF NOT EXISTS chat_messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL)'})
    def connect(self):
        db=sqlite3.connect(self.path, timeout=30)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db
    @contextmanager
    def lock(self, sid):
        try: sid = str(uuid.UUID(sid))
        except (ValueError, TypeError, AttributeError): raise ChatError('session_not_found') from None
        with open(self.lock_dir / sid, 'a+b') as stream:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0: stream.write(b'0'); stream.flush()
                stream.seek(0)
                # Windows LK_LOCK retries for a bounded interval.
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX)
            try: yield
            finally:
                if os.name == 'nt':
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(stream, fcntl.LOCK_UN)
    def create(self, version, provider, model):
        sid=str(uuid.uuid4())
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO chat_sessions(id,dataset_version,provider,model) VALUES (?,?,?,?)',(sid,version,provider,model))
        return self.get(sid)
    def get(self, sid):
        with closing(self.connect()) as db:
            row=db.execute('SELECT * FROM chat_sessions WHERE id=?',(sid,)).fetchone()
            if row is None: raise ChatError('session_not_found')
            result=dict(row); result['context']=_decode(json.loads(result['context'])) if result['context'] else None
            result['messages']=[dict(r) for r in db.execute('SELECT role,content FROM chat_messages WHERE session_id=? ORDER BY id',(sid,))]
            return result
    def list(self):
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute('SELECT id,dataset_version,provider,model,created_at,updated_at FROM chat_sessions ORDER BY updated_at DESC, rowid DESC')]
    def save(self, sid, message, answer, context):
        if not isinstance(message, str) or len(message) > MAX_USER_MESSAGE_CHARS:
            raise ChatError('invalid_message')
        if not isinstance(answer, str) or len(answer) > MAX_ASSISTANT_ANSWER_CHARS:
            raise ChatError('answer_too_long')
        with closing(self.connect()) as db, db:
            cur=db.execute('UPDATE chat_sessions SET context=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(json.dumps(_encode(context),ensure_ascii=False),sid))
            if not cur.rowcount: raise ChatError('session_not_found')
            db.executemany('INSERT INTO chat_messages(session_id,role,content) VALUES (?,?,?)',[(sid,'user',message),(sid,'assistant',answer)])
    def delete(self, sid):
        with closing(self.connect()) as db, db:
            if not db.execute('DELETE FROM chat_sessions WHERE id=?',(sid,)).rowcount: raise ChatError('session_not_found')

class ChatService:
    def __init__(self, store, snapshot_loader, provider_factory, active_version, active_provider, *, review_submit=None):
        self.store,self.snapshot_loader,self.provider_factory=store,snapshot_loader,provider_factory
        self.active_version,self.active_provider=active_version,active_provider
        self.review_submit=review_submit
        self.last_trace = {}

    @staticmethod
    def _safe_trace(agent_trace, *, snapshot_ms=None, provider_setup_ms=None):
        safe = {}
        for key in ('status', 'stage', 'error_code', 'error_type', 'final_response_status'):
            value = agent_trace.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', value):
                safe[key] = value
        for key in ('selected_tools', 'executed_tools'):
            values = agent_trace.get(key)
            if isinstance(values, list):
                safe[key] = [value for value in values[:3] if isinstance(value, str) and
                             re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value)]
        calls = agent_trace.get('model_calls')
        if isinstance(calls, list):
            safe_calls = []
            for call in calls[:2]:
                if not isinstance(call, dict):
                    continue
                entry = {}
                for key in ('stage', 'status', 'error_code'):
                    value = call.get(key)
                    if isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value):
                        entry[key] = value
                for key in ('duration_ms', 'input_tokens', 'output_tokens', 'thought_tokens'):
                    value = call.get(key)
                    if type(value) is int and value >= 0:
                        entry[key] = value
                safe_calls.append(entry)
            safe['model_calls'] = safe_calls
        timings = agent_trace.get('timings_ms')
        if isinstance(timings, dict):
            safe['timings_ms'] = {key: value for key, value in timings.items()
                                  if key in {'snapshot', 'interpretation', 'execution', 'final_response',
                                             'render', 'persistence', 'provider_setup', 'end_to_end'}
                                  and type(value) is int and value >= 0}
        if snapshot_ms is not None:
            safe.setdefault('timings_ms', {})['snapshot'] = snapshot_ms
        if provider_setup_ms is not None:
            safe.setdefault('timings_ms', {})['provider_setup'] = provider_setup_ms
        return safe
    @staticmethod
    def _public(session):
        return {k:v for k,v in session.items() if k != 'context'}
    def create(self):
        version=self.active_version()
        if version is None: raise ChatError('dataset_missing')
        selection=self.active_provider()
        if not selection: raise ChatError('provider_missing')
        return self._public(self.store.create(version,*selection))
    def list(self): return self.store.list()
    def get(self,sid): return self._public(self.store.get(sid))
    def delete(self,sid):
        with self.store.lock(sid): self.store.delete(sid)
    def send(self,sid,message):
        self.last_trace={'status':'pending','stage':'validation','error_code':None}
        if (not isinstance(message,str) or not message.strip() or
                len(message)>MAX_USER_MESSAGE_CHARS):
            self.last_trace={'status':'error','stage':'validation','error_code':'invalid_message'}
            raise ChatError('invalid_message')
        try:
            sid=str(uuid.UUID(sid))
        except (ValueError, TypeError, AttributeError):
            self.last_trace={'status':'error','stage':'session','error_code':'session_not_found'}
            raise ChatError('session_not_found') from None
        request_started=time.monotonic()
        with self.store.lock(sid):
            try:
                session=self.store.get(sid)
            except ChatError as exc:
                self.last_trace={'status':'error','stage':'session','error_code':exc.code}
                raise
            snapshot_started=time.monotonic()
            try: snapshot=self.snapshot_loader(session['dataset_version'])
            except (ValueError, KeyError):
                self.last_trace={'status':'error','stage':'snapshot','error_code':'dataset_unavailable'}
                raise ChatError('dataset_unavailable') from None
            snapshot_ms=max(0,int((time.monotonic()-snapshot_started)*1000))
            local_only = local_only_request(message, snapshot, session['context'])
            if local_only:
                provider=None
                provider_setup_ms=0
            else:
                provider_started=time.monotonic()
                try: provider=self.provider_factory(session['provider'],session['model'])
                except (ValueError, KeyError):
                    self.last_trace={'status':'error','stage':'provider_setup','error_code':'provider_unavailable'}
                    raise ChatError('provider_unavailable') from None
                if provider is None:
                    self.last_trace={'status':'error','stage':'provider_setup','error_code':'provider_unavailable'}
                    raise ChatError('provider_unavailable')
                provider_setup_ms=max(0,int((time.monotonic()-provider_started)*1000))
            agent=ConversationAgent(snapshot,provider,review_submit=self.review_submit)
            if session['context'] is not None:
                prior=session['context']
                agent.context[sid]=prior
                # Only catalog identity and a policy topic are needed for tool selection.
                # Never replay transcripts, customer details or order results.
                hints=[]
                if prior.product_name: hints.append('Produto de referência: '+prior.product_name[:200])
                if prior.policy_topic: hints.append('Política de referência: '+prior.policy_topic[:50])
                if hints: agent.history[sid]=[{'role':'user','content':'; '.join(hints)}]
            answer=agent.answer(message,chat_id=sid)
            timings=agent.last_trace.setdefault('timings_ms',{})
            timings['snapshot']=snapshot_ms
            timings['provider_setup']=provider_setup_ms
            timings['end_to_end']=max(0,int((time.monotonic()-request_started)*1000))
            self.last_trace=self._safe_trace(agent.last_trace)
            if agent.last_trace['status'] in {'error','tool_error','uncertain'}:
                if agent.last_trace.get('error_type') == 'BudgetExhausted': raise ChatError('budget_exhausted')
                raise ChatError('agent_unavailable')
            persistence_started=time.monotonic()
            persisted_message=agent.last_trace.get('persisted_user_message',redact_contact_details(message))
            try:
                self.store.save(sid,persisted_message,answer,agent.context.get(sid))
            except ChatError as exc:
                self.last_trace.update(status='error',stage='persistence',error_code=exc.code)
                raise
            try:
                saved_session=self.get(sid)
            except ChatError as exc:
                self.last_trace=self._safe_trace(agent.last_trace)
                self.last_trace.update(status='error',stage='session',error_code=exc.code)
                raise
            timings['persistence']=max(0,int((time.monotonic()-persistence_started)*1000))
            timings['end_to_end']=max(0,int((time.monotonic()-request_started)*1000))
            self.last_trace=self._safe_trace(agent.last_trace)
            return {'answer':answer,'session':saved_session}
