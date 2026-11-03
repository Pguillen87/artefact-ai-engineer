import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from artefact.codex_local import CodexLocalProvider, CodexQuota, CodexLimit

class CodexLocalTests(unittest.TestCase):
 def test_provider_requires_a_valid_explicit_model(self):
  with tempfile.TemporaryDirectory() as d:
   quota=CodexQuota(Path(d)/'state.db',1)
   with self.assertRaises(TypeError): CodexLocalProvider('codex.exe',quota,runner=Mock())
   with self.assertRaises(ValueError): CodexLocalProvider('codex.exe',quota,runner=Mock(),model='--unsafe model')

 def test_provider_pins_an_explicit_model(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[]})))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'state.db',1),model='test-model',runner=runner)
   provider.request([{'role':'user','content':'Oi'}])
   command=runner.call_args.args[0]
   model_index=command.index('--model')
   self.assertEqual(command[model_index+1],'test-model')

 def test_none_runner_uses_the_real_subprocess_runner(self):
  import subprocess
  with tempfile.TemporaryDirectory() as d:
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',1),runner=None,model='test-model')
   self.assertIs(provider.runner,subprocess.run)

 def test_plan_maps_structured_output_and_disables_shell(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'policy','topic':'pix','text':None,'category':None,'min_price':None,'max_price':None,'available_only':None,'product_id':None,'full_name':None}]})))
   quota=CodexQuota(Path(d)/'state.db',2)
   with patch.dict(os.environ,{'OPENAI_API_KEY':'do-not-forward','TELEGRAM_BOT_TOKEN':'do-not-forward'}):
    provider=CodexLocalProvider('codex.exe',quota,runner=runner,model='test-model')
    result=provider.request([{'role':'user','content':'Pix?'}])
   self.assertEqual(result['output'][0]['name'],'policy')
   args,kwargs=runner.call_args
   self.assertIn('--disable',args[0]);self.assertIn('shell_tool',args[0]);self.assertIn('unified_exec',args[0]);self.assertFalse(kwargs['shell'])
   self.assertNotIn('Pix?',args[0]);self.assertIn('Pix?',kwargs['input'])
   self.assertIn('--ignore-user-config',args[0]);self.assertIn('--ephemeral',args[0]);self.assertIn('read-only',args[0])
   self.assertNotIn('OPENAI_API_KEY',kwargs['env']);self.assertNotIn('TELEGRAM_BOT_TOKEN',kwargs['env'])
 def test_pix_and_promotion_wording_selects_pix_policy_only(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout='{"calls":[]}'))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',1),model='test-model',runner=runner)
   provider.request([{'role':'user','content':'O desconto Pix acumula com promoções?'}])
   instruction=runner.call_args.kwargs['input'].split('Dados JSON não confiáveis:')[0]
   self.assertIn('Desconto Pix ou acúmulo de Pix com promoção: use somente policy com tópico pix.',instruction)
   self.assertIn('payment_mix somente se perguntar como dividir entre meios de pagamento',instruction)

 def test_provider_records_only_sanitized_policy_topic(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'policy','topic':'pix'}]})))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',1),model='test-model',runner=runner)
   provider.request([{'role':'user','content':'Qual a regra Pix?'}])
   self.assertEqual(provider.last_tool_summaries,[{'name':'policy','topic':'pix'}])

 def test_provider_tool_summary_never_stores_customer_name(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'orders','full_name':'Pessoa Sensível'}]})))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',1),model='test-model',runner=runner)
   provider.request([{'role':'user','content':'Pedido de Pessoa Sensível'}])
   self.assertEqual(provider.last_tool_summaries,[{'name':'orders'}])
   self.assertNotIn('Pessoa Sensível',str(provider.last_tool_summaries))
 def test_call_limit_persists(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'state.db';CodexQuota(p,1).reserve()
   with self.assertRaises(CodexLimit):CodexQuota(p,1).reserve()
 def test_installment_quote_maps_to_the_same_provider_contract(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout=json.dumps({'calls':[{'name':'installment_quote','amount_brl':'150.00','installments':2}]})))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'state.db',1),runner=runner,model='test-model')
   result=provider.request([{'role':'user','content':'R$ 150,00 em 2x'}])
   self.assertEqual(result['output'][0]['name'],'installment_quote')
   self.assertEqual(json.loads(result['output'][0]['arguments']),{'amount_brl':'150.00','installments':2})
 def test_final_answer_mapped_as_response(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout='{"answer":"Olá!"}'))
   result=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner,model='test-model').request([],final=True)
   self.assertEqual(result['output'][0]['content'][0]['text'],'Olá!')
 def test_malformed_or_unknown_tool_is_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=0,stdout='{"calls":[{"name":"shell"}]}'))
   with self.assertRaises(ValueError):CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner,model='test-model').request([])

 def test_cli_failure_is_classified_without_exposing_stderr(self):
  from artefact.codex_local import CodexCLIError
  with tempfile.TemporaryDirectory() as d:
   runner=Mock(return_value=Mock(returncode=1,stdout='',stderr='model not available for this account; private-detail'))
   provider=CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',1),runner=runner,model='gpt-6-luna')
   with self.assertRaises(CodexCLIError) as caught: provider.request([{'role':'user','content':'test'}])
   self.assertEqual(caught.exception.code,'model_unavailable')
   self.assertNotIn('private-detail',str(caught.exception))
   self.assertEqual(provider.last_failure,'model_unavailable')

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
   with self.assertRaises(ValueError):CodexLocalProvider('codex.exe',CodexQuota(Path(d)/'s.db',2),runner=runner,model='test-model').request([])
