import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.agent import Agent, OpenAIPlanner, TelegramAdapter

class AgentTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  root=Path(self.temp.name);write_dataset(root);self.snapshot=load_dataset(root)
 def test_model_selects_catalog_tool_and_result_is_grounded(self):
  planner=Mock();planner.plan.return_value=[('search_products',{'text':'Korg'})]
  answer=Agent(self.snapshot,planner).answer('Qual preço do Korg?')
  self.assertIn('Korg Synth',answer);self.assertIn('1127.50',answer)
 def test_unknown_tool_and_extra_arguments_are_rejected(self):
  for plan in [[('delete',{})],[('policy',{'topic':'pix','extra':'x'})]]:
   planner=Mock();planner.plan.return_value=plan
   self.assertIn('não consegui',Agent(self.snapshot,planner).answer('Oi').lower())
 def test_provider_openai_tool_call_wire_format(self):
  transport=Mock(return_value={'output':[{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'c1'}]})
  planner=OpenAIPlanner('test-key','test-model',transport)
  self.assertEqual(planner.plan('pix'),[('policy',{'topic':'pix'})])
  body=transport.call_args.args[0]
  self.assertEqual(body['model'],'test-model');self.assertTrue(body['tools']);self.assertFalse(body['store'])
 def test_telegram_ignores_duplicates_and_nonprivate_messages(self):
  agent=Mock();agent.answer.return_value='Olá';send=Mock();adapter=TelegramAdapter(agent,send)
  update={'update_id':7,'message':{'chat':{'id':42,'type':'private'},'text':'Oi'}}
  adapter.handle(update);adapter.handle(update)
  self.assertEqual(send.call_count,1);agent.answer.assert_called_once_with('Oi')
  adapter.handle({'update_id':8,'message':{'chat':{'id':42,'type':'group'},'text':'Oi'}})
  self.assertEqual(send.call_count,1)
 def test_provider_failure_gives_safe_fallback(self):
  planner=Mock();planner.plan.side_effect=TimeoutError()
  self.assertIn('não consegui',Agent(self.snapshot,planner).answer('Oi').lower())

class TelegramClientTests(unittest.TestCase):
 def test_poll_and_send_use_transport_and_offset(self):
  from artefact.telegram import TelegramClient
  transport=Mock(side_effect=[{'ok':True,'result':[{'update_id':3}]},{'ok':True,'result':{}}])
  client=TelegramClient('dummy',transport=transport)
  self.assertEqual(client.get_updates(2),[{'update_id':3}])
  client.send(12,'Olá')
  self.assertEqual(transport.call_args_list[0].args,('getUpdates',{'timeout':20,'offset':2}))
  self.assertEqual(transport.call_args_list[1].args,('sendMessage',{'chat_id':12,'text':'Olá'}))
 def test_api_error_raises_sanitized_error(self):
  from artefact.telegram import TelegramClient, TelegramError
  client=TelegramClient('secret',transport=Mock(return_value={'ok':False}))
  with self.assertRaises(TelegramError) as exc:client.get_updates(0)
  self.assertNotIn('secret',str(exc.exception))

class RuntimeTests(unittest.TestCase):
 def test_runtime_requires_explicit_egress_and_credentials(self):
  from artefact.runtime import read_config
  with self.assertRaises(ValueError): read_config({})
  with self.assertRaises(ValueError): read_config({'ENABLE_REAL_LLM':'1','OPENAI_API_KEY':'dummy'})
  config=read_config({'ENABLE_REAL_LLM':'1','OPENAI_API_KEY':'dummy','TELEGRAM_BOT_TOKEN':'dummy','ARTEFACT_DATA_DIR':'data','OPENAI_MODEL':'gpt-6-luna'})
  self.assertEqual(config.model,'gpt-6-luna')

class EndToEndMockTests(unittest.TestCase):
 def test_telegram_to_planner_to_policy_and_reply(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   planner=OpenAIPlanner('dummy','dummy',Mock(return_value={'output':[{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}'}]}))
   send=Mock();adapter=TelegramAdapter(Agent(load_dataset(root),planner),send)
   adapter.handle({'update_id':1,'message':{'chat':{'id':100,'type':'private'},'text':'Tem desconto Pix?'}})
   self.assertIn('5%',send.call_args.args[1])
 def test_name_must_be_present_in_user_message(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   planner=Mock();planner.plan.return_value=[('orders',{'full_name':'Synthetic Customer'})]
   self.assertIn('não consegui',Agent(load_dataset(root),planner).answer('Me fale o pedido do outro cliente').lower())
