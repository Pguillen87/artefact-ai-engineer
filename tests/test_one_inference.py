import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from decimal import Decimal
from test_data import write_dataset
from artefact.data import load_dataset
from artefact.conversation import ConversationAgent
from artefact.provider import ResponsesProvider
from artefact.codex_local import CodexLocalProvider, CodexQuota
from artefact.state import State

class OneInferenceTests(unittest.TestCase):
 def test_product_pix_order_use_one_responses_call_each(self):
  cases=[('Preço Korg?','search_products',{'text':'Korg'},'1.127,50'),('Pix?','policy',{'topic':'pix'},'5%'),('Synthetic Customer pedido?','orders',{'full_name':'Synthetic Customer'},'status cadastrado')]
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);snapshot=load_dataset(root);state=State(root/'s.db')
   for question,name,args,expected in cases:
    response={'output':[{'type':'function_call','name':name,'arguments':json.dumps(args),'call_id':'x'}],'usage':{'input_tokens':10,'output_tokens':2}}
    transport=Mock(side_effect=[response,AssertionError('unexpected second inference')])
    provider=ResponsesProvider('dummy','model',state,Decimal('1'),3,Decimal('0.1'),Decimal('0.5'),transport=transport)
    self.assertIn(expected,ConversationAgent(snapshot,provider).answer(question,chat_id=1))
    self.assertEqual(transport.call_count,1)
   self.assertEqual(state.usage()['calls'],3)
 def test_codex_provider_uses_one_subprocess_for_policy(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);quota=CodexQuota(root/'q.db',1)
   runner=Mock(side_effect=[Mock(returncode=0,stdout='{"calls":[{"name":"policy","topic":"pix"}]}'),AssertionError('unexpected second inference')])
   provider=CodexLocalProvider('codex.exe',quota,runner)
   self.assertIn('5%',ConversationAgent(load_dataset(root),provider).answer('Pix?',chat_id=1))
   self.assertEqual(runner.call_count,1);self.assertEqual(quota.usage(),1)
