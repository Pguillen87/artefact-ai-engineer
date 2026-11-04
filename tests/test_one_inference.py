import json
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from unittest.mock import patch
from decimal import Decimal, ROUND_CEILING
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.conversation import ConversationAgent
from artefact.provider import ResponsesProvider
from artefact.state import State

class TwoStageInferenceTests(unittest.TestCase):
 def test_default_openai_transport_shortens_only_the_final_request(self):
  timeouts=[]
  class Response(io.BytesIO):
   def __enter__(self): return self
   def __exit__(self,*_args): self.close()
  def urlopen(_request,timeout):
   timeouts.append(timeout)
   return Response(b'{"output":[],"usage":{}}')
  with TemporaryDirectory() as d:
   state=State(Path(d)/'state.db')
   provider=ResponsesProvider('test-key','model',state,Decimal('1'),6,Decimal('0.1'),Decimal('0.5'))
   with patch('artefact.agent.urllib.request.urlopen',side_effect=urlopen):
    provider.request([{'role':'user','content':'Choose a tool.'}])
    provider.request([{'role':'user','content':'Use evidence only.'}],final=True)
  self.assertEqual(timeouts,[25,5])

 def test_openai_output_ceiling_and_budget_reservation_use_2048_tokens(self):
  with TemporaryDirectory() as d:
   state=State(Path(d)/'state.db')
   transport=Mock(return_value={'output':[]})
   provider=ResponsesProvider('test-key','model',state,Decimal('1'),6,Decimal('0.1'),Decimal('0.5'),transport=transport)
   provider.request([{'role':'user','content':'Use only evidence.'}],final=True)
   body=transport.call_args.args[0]
   self.assertEqual(body['max_output_tokens'],2048)
   self.assertNotIn('tools',body)
   self.assertEqual(body['tool_choice'],'none')
   output_format=body['text']['format']
   self.assertEqual(output_format['type'],'json_schema')
   self.assertTrue(output_format['strict'])
   self.assertFalse(output_format['schema']['additionalProperties'])
   self.assertIn('opening',output_format['schema']['required'])
   self.assertIn('product_order',output_format['schema']['required'])
   payload=json.dumps(body,ensure_ascii=False).encode('utf-8')
   estimate=(Decimal(len(payload)+4096)*Decimal('0.1')+Decimal(2048)*Decimal('0.5'))/Decimal(1_000_000)
   expected=(estimate*Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING)/Decimal(1_000_000)
   self.assertEqual(state.usage()['reserved_usd'],expected)

 def test_product_pix_order_use_at_most_two_responses_calls_each(self):
  cases=[('Preço Korg?','search_products',{'text':'Korg'},'1.127,50'),('Pix?','policy',{'topic':'pix'},'5%'),('Synthetic Customer pedido?','orders',{'full_name':'Synthetic Customer'},'status cadastrado')]
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);snapshot=load_dataset(root);state=State(root/'s.db')
   for question,name,args,expected in cases:
    response={'output':[{'type':'function_call','name':name,'arguments':json.dumps(args),'call_id':'x'}],'usage':{'input_tokens':10,'output_tokens':2}}
    transport=Mock(side_effect=[response,response])
    provider=ResponsesProvider('dummy','model',state,Decimal('1'),6,Decimal('0.1'),Decimal('0.5'),transport=transport)
    self.assertIn(expected,ConversationAgent(snapshot,provider).answer(question,chat_id=1))
    self.assertEqual(transport.call_count,2)
   self.assertEqual(state.usage()['calls'],6)
