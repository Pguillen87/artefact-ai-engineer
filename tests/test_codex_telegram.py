import unittest
from unittest.mock import Mock
from pathlib import Path
from tempfile import TemporaryDirectory
from artefact.agent import TelegramAdapter
from artefact.state import State

class AuthorizedChatTests(unittest.TestCase):
 def test_unauthorized_private_chat_never_reaches_model_or_sender(self):
  with TemporaryDirectory() as d:
   agent=Mock();send=Mock();state=State(Path(d)/'state.db')
   adapter=TelegramAdapter(agent,send,state=state,allowed_chat_id=123)
   adapter.handle({'update_id':10,'message':{'chat':{'id':999,'type':'private'},'text':'Oi'}})
   agent.answer.assert_not_called();send.assert_not_called()
   self.assertTrue(state.claim_update(10))
 def test_authorized_chat_still_deduplicates_across_restart(self):
  with TemporaryDirectory() as d:
   agent=Mock();agent.answer.return_value='Olá';send=Mock();path=Path(d)/'s.db'
   update={'update_id':11,'message':{'chat':{'id':123,'type':'private'},'text':'Oi'}}
   TelegramAdapter(agent,send,state=State(path),allowed_chat_id=123).handle(update)
   TelegramAdapter(agent,send,state=State(path),allowed_chat_id=123).handle(update)
   self.assertEqual(send.call_count,1);agent.answer.assert_called_once_with('Oi',chat_id=123)

class HostConfigTests(unittest.TestCase):
 def test_requires_chat_token_data_path_state_and_explicit_opt_in(self):
  from artefact.codex_telegram import read_host_config
  with self.assertRaises(ValueError): read_host_config({})
  env={'ENABLE_CODEX_TELEGRAM':'1','TELEGRAM_BOT_TOKEN':'dummy','TELEGRAM_ALLOWED_CHAT_ID':'123','ARTEFACT_DATA_DIR':'/data','CODEX_DEMO_STATE_PATH':'/tmp/example.db','CODEX_DEMO_MAX_CALLS':'6'}
  config=read_host_config(env)
  self.assertEqual(config.allowed_chat_id,123)
  self.assertNotIn('dummy',repr(config))
  env['TELEGRAM_ALLOWED_CHAT_ID']='-42'
  with self.assertRaises(ValueError):read_host_config(env)
 def test_poll_cycle_filters_and_acknowledges_updates(self):
  from artefact.codex_telegram import poll_once
  client=Mock();client.get_updates.return_value=[{'update_id':7,'message':{'chat':{'id':99,'type':'private'},'text':'x'}},{'update_id':8,'message':{'chat':{'id':123,'type':'private'},'text':'Oi'}}]
  agent=Mock();agent.answer.return_value='Olá';send=Mock()
  with TemporaryDirectory() as d:
   adapter=TelegramAdapter(agent,send,State(Path(d)/'s.db'),allowed_chat_id=123)
   self.assertEqual(poll_once(client,adapter,0),9)
  agent.answer.assert_called_once_with('Oi',chat_id=123);send.assert_called_once()

class HostFlowTests(unittest.TestCase):
 def test_authorized_telegram_to_codex_bridge_and_budget(self):
  import json,sys
  from test_data import write_dataset
  from artefact.data import load_dataset
  from artefact.codex_local import CodexLocalProvider,CodexQuota
  from artefact.conversation import ConversationAgent
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);state=State(root/'state.db');quota=CodexQuota(root/'state.db',2)
   plan={'calls':[{'name':'policy','topic':'pix','text':None,'category':None,'min_price':None,'max_price':None,'available_only':None,'product_id':None,'full_name':None}]}
   runner=Mock(side_effect=[Mock(returncode=0,stdout=json.dumps(plan)),Mock(returncode=0,stdout=json.dumps({'answer':'O desconto Pix é 5% e não acumula com promoção.'}))])
   provider=CodexLocalProvider('codex.exe',quota,runner)
   send=Mock();adapter=TelegramAdapter(ConversationAgent(load_dataset(root),provider),send,state,allowed_chat_id=123)
   adapter.handle({'update_id':22,'message':{'chat':{'id':999,'type':'private'},'text':'Pix?'}})
   self.assertEqual(quota.usage(),0)
   adapter.handle({'update_id':23,'message':{'chat':{'id':123,'type':'private'},'text':'Pix?'}})
   self.assertIn('5%',send.call_args.args[1]);self.assertEqual(quota.usage(),2)
   adapter.handle({'update_id':23,'message':{'chat':{'id':123,'type':'private'},'text':'Pix?'}})
   self.assertEqual(send.call_count,1);self.assertEqual(runner.call_count,2)

class StateIsolationTests(unittest.TestCase):
 def test_state_cannot_be_written_within_original_data_directory(self):
  from artefact.codex_telegram import read_host_config
  with TemporaryDirectory() as d:
   root=Path(d)
   env={'ENABLE_CODEX_TELEGRAM':'1','TELEGRAM_BOT_TOKEN':'dummy','TELEGRAM_ALLOWED_CHAT_ID':'123','ARTEFACT_DATA_DIR':str(root),'CODEX_DEMO_STATE_PATH':str(root/'state.db'),'CODEX_DEMO_MAX_CALLS':'6'}
   with self.assertRaises(ValueError):read_host_config(env)
