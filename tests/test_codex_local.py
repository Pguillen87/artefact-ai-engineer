import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from artefact.codex_local import CodexLocalProvider, CodexQuota, CodexLimit

class CodexLocalTests(unittest.TestCase):
 def test_plan_maps_structured_output_and_disables_shell(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'policy','topic':'pix','text':None,'category':None,'min_price':None,'max_price':None,'available_only':None,'product_id':None,'full_name':None}]})))
   quota=CodexQuota(Path(d)/'state.db',2)
   with patch.dict(os.environ,{'OPENAI_API_KEY':'do-not-forward','TELEGRAM_BOT_TOKEN':'do-not-forward'}):
    provider=CodexLocalProvider('codex.exe',quota,runner=runner)
    result=provider.request([{'role':'user','content':'Pix?'}])
   self.assertEqual(result['output'][0]['name'],'policy')
   args,kwargs=runner.call_args
   self.assertIn('--disable',args[0]);self.assertIn('shell_tool',args[0]);self.assertIn('unified_exec',args[0]);self.assertFalse(kwargs['shell'])
   self.assertNotIn('Pix?',args[0]);self.assertIn('Pix?',kwargs['input'])
   self.assertIn('--ignore-user-config',args[0]);self.assertIn('--ephemeral',args[0]);self.assertIn('read-only',args[0])
   self.assertNotIn('OPENAI_API_KEY',kwargs['env']);self.assertNotIn('TELEGRAM_BOT_TOKEN',kwargs['env'])
 def test_call_limit_persists(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'state.db';CodexQuota(p,1).reserve()
   with self.assertRaises(CodexLimit):CodexQuota(p,1).reserve()
 def test_installment_quote_maps_to_the_same_provider_contract(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'installment_quote','amount_brl':'150.00','installments':2}]})))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'state.db',1),runner=runner)
   result=provider.request([{'role':'user','content':'R$ 150,00 em 2x'}])
   self.assertEqual(result['output'][0]['name'],'installment_quote')
   self.assertEqual(json.loads(result['output'][0]['arguments']),{'amount_brl':'150.00','installments':2})
 def test_final_answer_mapped_as_response(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout='{"answer":"Olá!"}'))
   result=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner).request([],final=True)
   self.assertEqual(result['output'][0]['content'][0]['text'],'Olá!')
 def test_malformed_or_unknown_tool_is_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout='{"calls":[{"name":"shell"}]}'))
   with self.assertRaises(ValueError):CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner).request([])

class DemoFixtureTests(unittest.TestCase):
 def test_demo_fixture_contains_only_synthetic_rows(self):
  from artefact.codex_demo import synthetic_snapshot
  snapshot=synthetic_snapshot()
  self.assertEqual(snapshot.tables['customers'][0]['name'],'Pessoa Exemplo')
  self.assertEqual(snapshot.tables['products'][0]['name'],'Teclado Exemplo')

class CodexRequiredArgumentsTests(unittest.TestCase):
 def test_required_tool_arguments_cannot_be_null(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'promotion','product_id':None}]})))
   with self.assertRaises(ValueError):CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner).request([])
