import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.conversation import ConversationAgent

class ConversationTests(unittest.TestCase):
 def setUp(self):
  self.temp=TemporaryDirectory();self.addCleanup(self.temp.cleanup);root=Path(self.temp.name);write_dataset(root)
  self.snapshot=load_dataset(root)
 def test_tool_result_returns_to_model_then_natural_reply(self):
  provider=Mock();provider.request.side_effect=[
   {'output':[{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'c1'}]},
   {'output':[{'type':'message','content':[{'type':'output_text','text':'No Pix, o desconto é de 5%, sem acumular com promoção.'}]}]}]
  agent=ConversationAgent(self.snapshot,provider)
  self.assertIn('5%',agent.answer('Como funciona o Pix?',chat_id=1))
  second=provider.request.call_args_list[1].args[0]
  self.assertTrue(any(item.get('type')=='function_call_output' and item.get('call_id')=='c1' and '5%' in item['output'] for item in second))
 def test_greeting_without_provider_and_failure_fallback(self):
  provider=Mock();agent=ConversationAgent(self.snapshot,provider)
  self.assertIn('Olá',agent.answer('oi',chat_id=1));provider.request.assert_not_called()
  provider.request.side_effect=TimeoutError()
  self.assertIn('não consegui',agent.answer('Preço?',chat_id=1).lower())
 def test_repeated_calls_and_invented_facts_are_rejected(self):
  for output in [
   [{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'1'},{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'2'}],
   [{'type':'function_call','name':'unknown','arguments':'{}','call_id':'1'}]]:
   provider=Mock();provider.request.return_value={'output':output}
   self.assertIn('não consegui',ConversationAgent(self.snapshot,provider).answer('Pix?',chat_id=1).lower())
 def test_context_is_bounded_and_separated_by_chat(self):
  provider=Mock();provider.request.side_effect=[{'output':[]},{'output':[]}]
  agent=ConversationAgent(self.snapshot,provider)
  agent.answer('Quero guitarra',chat_id=1);agent.answer('E o preço?',chat_id=2)
  self.assertNotIn('Quero guitarra',str(provider.request.call_args.args[0]))
 def test_too_long_or_unsupported_final_text_is_rejected(self):
  for text in ['x'*2000,'O desconto é 99%.']:
   provider=Mock();provider.request.side_effect=[{'output':[{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'c'}]}, {'output':[{'type':'message','content':[{'type':'output_text','text':text}]}]}]
   self.assertIn('não consegui',ConversationAgent(self.snapshot,provider).answer('Pix?',chat_id=1).lower())

class ProviderTests(unittest.TestCase):
 def test_strict_schema_and_budget_enforced_before_transport(self):
  from decimal import Decimal
  from artefact.provider import ResponsesProvider
  from artefact.state import State, BudgetExhausted
  with TemporaryDirectory() as d:
   state=State(Path(d)/'usage.db'); transport=Mock(return_value={'output':[]})
   provider=ResponsesProvider('dummy','gpt-6-luna',state,Decimal('1'),1,Decimal('0.10'),Decimal('0.50'),transport=transport)
   provider.request([{'role':'user','content':'oi'}])
   body=transport.call_args.args[0]
   self.assertFalse(body['store']);self.assertTrue(all(t['strict'] for t in body['tools']))
   for tool in body['tools']:
    self.assertEqual(set(tool['parameters']['required']),set(tool['parameters']['properties']))
    self.assertFalse(tool['parameters']['additionalProperties'])
   with self.assertRaises(BudgetExhausted): provider.request([{'role':'user','content':'outra'}])
   self.assertEqual(transport.call_count,1)

class DurableTelegramTests(unittest.TestCase):
 def test_claim_prevents_duplicate_after_restart(self):
  from artefact.state import State
  from artefact.agent import TelegramAdapter
  with TemporaryDirectory() as d:
   path=Path(d)/'state.db'; agent=Mock();agent.answer.return_value='Resposta';send=Mock()
   update={'update_id':5,'message':{'chat':{'id':123,'type':'private'},'text':'Olá'}}
   TelegramAdapter(agent,send,state=State(path)).handle(update)
   TelegramAdapter(agent,send,state=State(path)).handle(update)
   self.assertEqual(send.call_count,1)
   agent.answer.assert_called_once_with('Olá',chat_id=123)

class UsageTests(unittest.TestCase):
 def test_provider_records_token_usage_without_content(self):
  from decimal import Decimal
  from artefact.provider import ResponsesProvider
  from artefact.state import State
  with TemporaryDirectory() as d:
   state=State(Path(d)/'usage.db')
   response={'output':[],'usage':{'input_tokens':12,'output_tokens':7}}
   ResponsesProvider('dummy','test',state,Decimal('1'),3,Decimal('0.1'),Decimal('0.5'),transport=Mock(return_value=response)).request([{'role':'user','content':'sensitive text'}])
   self.assertEqual(state.usage()['input_tokens'],12)
   self.assertEqual(state.usage()['output_tokens'],7)

class CompleteFlowTests(unittest.TestCase):
 def test_full_mocked_responses_cycle_and_persistent_dedup(self):
  from decimal import Decimal
  from artefact.state import State
  from artefact.provider import ResponsesProvider
  from artefact.agent import TelegramAdapter
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   state=State(root/'runtime.db')
   transport=Mock(side_effect=[
    {'output':[{'type':'function_call','name':'policy','arguments':'{"topic":"pix"}','call_id':'call-1'}],'usage':{'input_tokens':10,'output_tokens':5}},
    {'output':[{'type':'message','content':[{'type':'output_text','text':'No Pix há 5% de desconto, sem acumular com promoção.'}]}],'usage':{'input_tokens':15,'output_tokens':9}}])
   provider=ResponsesProvider('dummy','gpt-6-luna',state,Decimal('1'),2,Decimal('0.1'),Decimal('0.5'),transport=transport)
   send=Mock();adapter=TelegramAdapter(ConversationAgent(load_dataset(root),provider),send,state)
   update={'update_id':100,'message':{'chat':{'id':10,'type':'private'},'text':'Qual desconto Pix?'}}
   adapter.handle(update);TelegramAdapter(ConversationAgent(load_dataset(root),provider),send,state).handle(update)
   self.assertEqual(send.call_count,1);self.assertIn('5%',send.call_args.args[1])
   self.assertEqual(state.usage()['calls'],2);self.assertEqual(state.usage()['input_tokens'],25)
   self.assertTrue(any(x.get('type')=='function_call_output' for x in transport.call_args_list[1].args[0]['input']))

class ContradictionTests(unittest.TestCase):
 def test_unconfirmed_order_payment_cannot_be_asserted(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   provider=Mock();provider.request.side_effect=[
    {'output':[{'type':'function_call','name':'orders','arguments':'{"full_name":"Synthetic Customer"}','call_id':'x'}]},
    {'output':[{'type':'message','content':[{'type':'output_text','text':'Seu pagamento está confirmado.'}]}]}]
   answer=ConversationAgent(load_dataset(root),provider).answer('Meu nome é Synthetic Customer, veja meu pedido',chat_id=1)
   self.assertIn('não consegui',answer.lower())

class MissingEvidenceTests(unittest.TestCase):
 def test_missing_product_does_not_reach_model_for_invention(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);provider=Mock()
   provider.request.return_value={'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Unobtainium"}','call_id':'x'}]}
   answer=ConversationAgent(load_dataset(root),provider).answer('Existe Unobtainium?',chat_id=1)
   self.assertIn('não encontrei',answer.lower());self.assertEqual(provider.request.call_count,1)

class FollowUpTests(unittest.TestCase):
 def test_follow_up_context_remains_in_same_private_chat(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   provider=Mock();provider.request.side_effect=[
    {'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Korg"}','call_id':'a'}]},
    {'output':[{'type':'message','content':[{'type':'output_text','text':'Encontrei o Korg Synth.'}]}]},
    {'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Korg"}','call_id':'b'}]},
    {'output':[{'type':'message','content':[{'type':'output_text','text':'O valor é 1127.50.'}]}]}]
   agent=ConversationAgent(load_dataset(root),provider)
   self.assertIn('Korg',agent.answer('Procuro Korg',chat_id=9))
   self.assertIn('1127.50',agent.answer('Qual o preço?',chat_id=9))
   self.assertIn('Procuro Korg',str(provider.request.call_args_list[2].args[0]))

class GeneralQuestionTests(unittest.TestCase):
 def test_capabilities_question_needs_no_api_or_tool(self):
  provider=Mock();agent=ConversationAgent(None,provider)
  answer=agent.answer('O que você faz?',chat_id=1)
  self.assertIn('produtos',answer.lower());provider.request.assert_not_called()
