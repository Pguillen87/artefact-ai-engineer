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
   answer=ConversationAgent(self.snapshot,provider).answer('Pix?',chat_id=1).lower()
   if len(text)>1000: self.assertIn('não consegui',answer)
   else: self.assertNotIn('99%',answer);self.assertIn('5%',answer)

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
   self.assertNotIn('pagamento está confirmado',answer.lower())
   self.assertIn('confirmação do pagamento não informada',answer.lower())

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
   self.assertIn('1.127,50',agent.answer('Qual o preço?',chat_id=9))
   self.assertIn('Procuro Korg',str(provider.request.call_args_list[2].args[0]))

class GeneralQuestionTests(unittest.TestCase):
 def test_capabilities_question_needs_no_api_or_tool(self):
  provider=Mock();agent=ConversationAgent(None,provider)
  answer=agent.answer('O que você faz?',chat_id=1)
  self.assertIn('produtos',answer.lower());provider.request.assert_not_called()

class RecommendationToolTests(unittest.TestCase):
 def test_recommendations_never_include_unavailable(self):
  from artefact.agent import Agent
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,{'products':{'status':'coming_soon'}})
   planner=Mock();planner.plan.return_value=[('recommend_products',{'text':'Korg'})]
   result=Agent(load_dataset(root),planner).answer('Recomende Korg')
   self.assertNotIn('Korg Synth',result)
   self.assertIn('resultado',result)

class ThreeMockedJourneysTests(unittest.TestCase):
 def test_product_policy_and_fictional_order_complete_cycles(self):
  from decimal import Decimal
  from artefact.state import State
  from artefact.provider import ResponsesProvider
  from artefact.agent import TelegramAdapter
  sequences=[
   ('Qual preço Korg?', 'search_products', '{"text":"Korg"}', 'O Korg Synth custa 1127.50.'),
   ('Qual desconto Pix?', 'policy', '{"topic":"pix"}', 'O Pix dá 5% sem acumular com promoção.'),
   ('Meu nome é Synthetic Customer, meu pedido?', 'orders', '{"full_name":"Synthetic Customer"}', 'O pedido registra total 1127.50.')]
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);state=State(root/'usage.db');wire=[]
   for _,name,args,answer in sequences:
    wire.extend([{'output':[{'type':'function_call','name':name,'arguments':args,'call_id':'call'}]}, {'output':[{'type':'message','content':[{'type':'output_text','text':answer}]}]}])
   transport=Mock(side_effect=wire);send=Mock()
   provider=ResponsesProvider('dummy','model',state,Decimal('1'),6,Decimal('0.1'),Decimal('0.5'),transport=transport)
   adapter=TelegramAdapter(ConversationAgent(load_dataset(root),provider),send,state)
   for index,(question,_,__,___) in enumerate(sequences):
    adapter.handle({'update_id':index,'message':{'chat':{'id':index+1,'type':'private'},'text':question}})
   self.assertEqual(send.call_count,3)
   self.assertTrue(all('não consegui' not in call.args[1].lower() for call in send.call_args_list))
   self.assertEqual(state.usage()['calls'],6)

class LocaleEvidenceTests(unittest.TestCase):
 def test_brl_format_is_compared_numerically_to_tool_evidence(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);provider=Mock();provider.request.side_effect=[
    {'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Korg"}','call_id':'x'}]},
    {'output':[{'type':'message','content':[{'type':'output_text','text':'O Korg Synth custa R$ 1.127,50.'}]}]}]
   self.assertIn('1.127,50',ConversationAgent(load_dataset(root),provider).answer('Preço Korg?',chat_id=1))

class CriticalEvidenceTests(unittest.TestCase):
 def _answer(self, changes, tool, args, reply, question):
  from test_data import write_dataset
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,changes);provider=Mock()
   provider.request.side_effect=[{'output':[{'type':'function_call','name':tool,'arguments':__import__('json').dumps(args),'call_id':'x'}]}, {'output':[{'type':'message','content':[{'type':'output_text','text':reply}]}]}]
   return ConversationAgent(load_dataset(root),provider).answer(question,chat_id=1)
 def test_price_never_taken_from_id_or_stock(self):
  for false_price in ('R$ 1.000','R$ 1','R$ 12','R$ 1,000.00','R$ 250,01'):
   with self.subTest(false_price=false_price):
    answer=self._answer({'products':{'price_brl':'250.00','stock_quantity':'12'}},'search_products',{'text':'Korg'},'Custa '+false_price+'.','Preço Korg?')
    self.assertNotIn(false_price,answer)
    self.assertIn('250,00',answer)
 def test_correct_price_derived_from_typed_evidence_for_multiple_formats(self):
  for rendered in ('R$ 250,00','R$ 250.00','R$ 2.500,00','R$ 2,500.00'):
   value='2500.00' if '500' in rendered else '250.00'
   answer=self._answer({'products':{'price_brl':value}},'search_products',{'text':'Korg'},'Custa '+rendered+'.','Preço Korg?')
   self.assertIn('2.500,00' if value=='2500.00' else '250,00',answer)
 def test_delivered_status_is_not_physical_delivery_confirmation(self):
  for reply in ('Entregue fisicamente em 01/01/2026.','Recebido pelo cliente.','A entrega foi confirmada.','Pagamento confirmado.'):
   answer=self._answer({'orders':{'status':'delivered','estimated_delivery':'2026-01-03'}},'orders',{'full_name':'Synthetic Customer'},reply,'Synthetic Customer pedido')
   self.assertIn('status cadastrado',answer.lower());self.assertIn('entregue',answer.lower())
   self.assertNotIn('confirmada em',answer.lower());self.assertNotIn('pagamento confirmado',answer.lower())
   self.assertIn('data efetiva',answer.lower())

class FieldBindingTests(unittest.TestCase):
 def test_prices_and_stocks_remain_bound_to_each_product(self):
  from artefact.rendering import render_results
  result=render_results([{'consulta':'search_products','resultado':[
   {'name':'Produto A','product_id':1,'price_brl':'250.00','stock_quantity':12,'available':True},
   {'name':'Produto B','product_id':250,'price_brl':'12.00','stock_quantity':1,'available':True}]}])
  self.assertIn('Produto A: R$ 250,00; disponível no cadastro, estoque registrado de 12',result)
  self.assertIn('Produto B: R$ 12,00; disponível no cadastro, estoque registrado de 1',result)
 def test_absent_price_is_not_synthesized_from_other_fields(self):
  from artefact.rendering import render_results
  with self.assertRaises((ValueError,KeyError)):
   render_results([{'consulta':'search_products','resultado':[{'name':'Produto','product_id':250,'stock_quantity':12,'available':True}]}])
 def test_tracking_and_estimate_remain_distinct_from_actual_delivery(self):
  from artefact.rendering import render_results
  result=render_results([{'consulta':'orders','resultado':[{'order_id':1,'status':'delivered','total_brl':'250.00','items':[],'payment_method':'pix','payment_confirmed':None,'tracking_code':'TEST123','estimated_delivery':'2026-01-03','delivered_at':None}]}])
  self.assertIn('Previsão de entrega cadastrada: 2026-01-03',result)
  self.assertIn('Código de rastreamento cadastrado: TEST123',result)
  self.assertIn('Data efetiva de entrega não informada',result)
 def test_oversized_rendered_response_is_not_silently_truncated(self):
  from test_data import write_dataset
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,{'products':{'name':'Korg '+('x'*3600)}})
   provider=Mock();provider.request.side_effect=[{'output':[{'type':'function_call','name':'search_products','arguments':'{"text":"Korg"}','call_id':'x'}]}, {'output':[{'type':'message','content':[{'type':'output_text','text':'Aqui está.'}]}]}]
   answer=ConversationAgent(load_dataset(root),provider).answer('Korg?',chat_id=1)
   self.assertIn('refine sua consulta',answer)
