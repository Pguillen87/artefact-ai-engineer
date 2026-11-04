import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from test_data import write_dataset
from artefact.data import load_dataset

class ChatTests(unittest.TestCase):
 def setUp(self):
  self.tmp=TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name); write_dataset(self.root)
  self.snapshot=load_dataset(self.root)
  self.provider=Mock(); self.provider.request.return_value={'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Korg"}','call_id':'1'}]}
  from artefact.session_store import SessionStore, ChatService
  self.store=SessionStore(self.root/'chat.db')
  self.service=ChatService(self.store, lambda version:self.snapshot, lambda provider,model:self.provider, lambda:1, lambda:('google','gemini-test'))
 def test_restart_version_and_privacy(self):
  session=self.service.create(); sid=session['id']
  self.assertIn('Korg',self.service.send(sid,'Preço Korg?')['answer'])
  from artefact.session_store import SessionStore, ChatService
  again=ChatService(SessionStore(self.root/'chat.db'),lambda version:self.snapshot,lambda p,m:self.provider,lambda:2,lambda:('openai','other'))
  self.assertEqual(again.get(sid)['dataset_version'],1)
  self.assertIn('1.127,50',again.send(sid,'Qual o preço?')['answer'])
  self.assertNotIn('Preço Korg?',str(self.provider.request.call_args.args[0]))
  self.assertEqual(len(again.get(sid)['messages']),4)
  again.delete(sid); self.assertEqual(again.list(),[])
 def test_failure_does_not_change_history(self):
  sid=self.service.create()['id']; self.provider.request.side_effect=TimeoutError('secret')
  with self.assertRaises(Exception): self.service.send(sid,'Preço Korg?')
  self.assertEqual(self.service.get(sid)['messages'],[])

class ChatHTTPTests(ChatTests):
 def test_authenticated_api(self):
  import json, threading, urllib.request, urllib.error
  from http.server import ThreadingHTTPServer
  from artefact.chat_api import handler_for
  server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.service,'test-token'))
  threading.Thread(target=server.serve_forever,daemon=True).start()
  self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
  url='http://127.0.0.1:%s/sessions' % server.server_port
  with self.assertRaises(urllib.error.HTTPError) as err: urllib.request.urlopen(url)
  self.assertEqual(err.exception.code,401)
  req=urllib.request.Request(url,data=b'{}',headers={'Authorization':'Bearer test-token','Content-Type':'application/json'})
  with urllib.request.urlopen(req) as response: session=json.load(response)
  req=urllib.request.Request(url+'/'+session['id']+'/messages',data=json.dumps({'message':'Preço Korg?'}).encode(),headers={'Authorization':'Bearer test-token','Content-Type':'application/json'})
  with urllib.request.urlopen(req) as response: self.assertIn('Korg',json.load(response)['answer'])

class ConcurrencyTests(ChatTests):
 def test_simultaneous_turns_do_not_overwrite(self):
  import concurrent.futures, time
  sid=self.service.create()['id']
  def reply(messages):
   time.sleep(.05)
   return {'output':[]}
  self.provider.request.side_effect=reply
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
   results=list(pool.map(lambda text:self.service.send(sid,text),['Quero guitarra','Quero piano']))
  self.assertEqual(len(results),2)
  self.assertEqual(len(self.service.get(sid)['messages']),4)

class TelegramChatTests(ChatTests):
 def test_telegram_chat_reuses_session_after_restart(self):
  from artefact.chat_api import TelegramChat
  first=TelegramChat(self.service)
  self.assertIn('Korg',first.answer('Preço Korg?',chat_id=123))
  second=TelegramChat(self.service)
  self.assertIn('1.127,50',second.answer('Qual o preço?',chat_id=123))
  self.assertEqual(len(self.service.list()),1)

class MinimizedContextTests(ChatTests):
 def test_product_reference_is_available_without_prior_transcript(self):
  sid=self.service.create()['id']
  self.service.send(sid,'Preço Korg? meu email fake@example.invalid')
  self.service.send(sid,'Qual o preço?')
  sent=str(self.provider.request.call_args.args[0])
  self.assertIn('Korg Synth',sent)
  self.assertNotIn('fake@example.invalid',sent)

class BudgetFailureTests(ChatTests):
 def test_budget_exhaustion_is_reported_without_saving_turn(self):
  from artefact.state import BudgetExhausted
  from artefact.session_store import ChatError
  sid=self.service.create()['id']; self.provider.request.side_effect=BudgetExhausted()
  with self.assertRaises(ChatError) as caught: self.service.send(sid,'Preço Korg?')
  self.assertEqual(caught.exception.code,'budget_exhausted')
  self.assertEqual(self.service.get(sid)['messages'],[])

class ProviderTestHTTPTests(ChatTests):
 def test_explicit_provider_test_requires_auth_and_calls_injected_test(self):
  import json, threading, urllib.request, urllib.error
  from http.server import ThreadingHTTPServer
  from artefact.chat_api import handler_for
  check=Mock(return_value={'ok':True})
  server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.service,'secret',provider_test=check))
  threading.Thread(target=server.serve_forever,daemon=True).start()
  self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
  url='http://127.0.0.1:%s/provider-test' % server.server_port
  request=urllib.request.Request(url,data=b'{"provider":"google"}',headers={'Authorization':'Bearer secret'})
  with urllib.request.urlopen(request) as response: self.assertEqual(json.load(response),{'ok':True})
  check.assert_called_once_with('google')

class SchemaTests(unittest.TestCase):
 def test_partial_session_schema_is_rejected(self):
  import sqlite3
  from artefact.session_store import SessionStore
  with TemporaryDirectory() as d:
   path=Path(d)/'chat.db'
   db=sqlite3.connect(path); db.execute('CREATE TABLE chat_sessions (id TEXT PRIMARY KEY)'); db.close()
   with self.assertRaises(ValueError): SessionStore(path)

class TelegramPollTests(unittest.TestCase):
 def test_failed_delivery_does_not_consume_later_update(self):
  from artefact.chat_api import poll_once
  from artefact.agent import TelegramAdapter
  from artefact.telegram import TelegramError
  from artefact.state import State
  client=Mock();client.get_updates.return_value=[{'update_id':21,'message':{'chat':{'id':123,'type':'private'},'text':'first'}},{'update_id':22,'message':{'chat':{'id':123,'type':'private'},'text':'second'}}]
  agent=Mock();agent.answer.return_value='ok'
  with TemporaryDirectory() as d:
   path=Path(d)/'state.db'
   with self.assertRaises(TelegramError):poll_once(client,TelegramAdapter(agent,Mock(side_effect=TelegramError('failed')),State(path),123),0)
   sender=Mock();self.assertEqual(poll_once(client,TelegramAdapter(agent,sender,State(path),123),0),23)
   sender.assert_called_once_with(123,'ok');self.assertEqual(agent.answer.call_count,2)

class ProviderPinnedTests(unittest.TestCase):
 def test_old_model_is_rejected_when_new_model_was_verified(self):
  from artefact.chat_api import configured_provider
  config=Mock();config.status.return_value={'providers':{'google':{'has_key':True,'verified':True,'model':'new-model'}}}
  factory=Mock()
  with self.assertRaises(ValueError): configured_provider(config,'google','old-model',Mock(),factory)
  factory.assert_not_called()

class ReviewServiceTests(ChatTests):
 def test_chat_passes_review_to_authorized_writer(self):
  import json
  from dataclasses import replace
  snapshot=replace(self.snapshot,catalog_version=1)
  submit=Mock(return_value=5)
  from artefact.session_store import ChatService
  service=ChatService(self.store,lambda _:snapshot,lambda p,m:self.provider,lambda:1,lambda:('google','test'),review_submit=submit)
  self.provider.request.return_value={'output':[{'type':'function_call','name':'request_technical_review','arguments':json.dumps({'product_id':1,'field':'specs.keys','reason':'inconsistencia_catalogo'}),'call_id':'a'}]}
  answer=service.send(service.create()['id'],'Registre revisão técnica das teclas do produto 1')
  self.assertIn('registrada',answer['answer']);submit.assert_called_once_with(1,'specs.keys','inconsistencia_catalogo',1)
