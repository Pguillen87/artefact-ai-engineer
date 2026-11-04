import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from artefact.provider_config import ProviderConfig
from artefact.provider import make_provider, ProviderError
from artefact.state import State, BudgetExhausted

class ProviderErrorMatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); p=Path(self.tmp.name)
        self.cfg=ProviderConfig(p/'config',p/'master'); self.state=State(p/'state')
        self.cfg.save('openai','gpt-4.1-mini','private-secret', max_calls=20)
        self.cfg.save('google','gemini-3.5-flash-lite','private-secret', max_calls=20)
    def tearDown(self): self.tmp.cleanup()
    def test_http_errors_are_classified_and_sanitized_for_both_adapters(self):
        for provider, model in [('openai','gpt-4.1-mini'),('google','gemini-3.5-flash-lite')]:
            for status, category in [(401,'provider_authentication'),(403,'provider_authentication'),(429,'provider_rate_limit'),(500,'provider_unavailable'),(503,'provider_unavailable'),(408,'provider_timeout')]:
                with self.subTest(provider=provider,status=status):
                    class Failure(Exception): code=status
                    def fail(*args,**kwargs): raise Failure('private-secret')
                    client=SimpleNamespace(models=SimpleNamespace(generate_content=fail))
                    adapter=make_provider(self.cfg,provider,model,self.state,transport=fail,client=client)
                    with self.assertRaises(ProviderError) as error: adapter.request([{'role':'user','content':'oi'}])
                    self.assertEqual(error.exception.category,category)
                    self.assertNotIn('private-secret',str(error.exception))
    def test_timeout_and_bad_payload_are_controlled(self):
        for provider, model in [('openai','gpt-4.1-mini'),('google','gemini-3.5-flash-lite')]:
            def timeout(*args,**kwargs): raise TimeoutError('private-secret')
            adapter=make_provider(self.cfg,provider,model,self.state,transport=timeout,client=SimpleNamespace(models=SimpleNamespace(generate_content=timeout)))
            with self.assertRaises(ProviderError) as error: adapter.request([{'role':'user','content':'oi'}])
            self.assertEqual(error.exception.category,'provider_timeout')
    def test_openai_malformed_response_is_rejected(self):
        adapter=make_provider(self.cfg,'openai','gpt-4.1-mini',self.state,transport=lambda body:{'output':'wrong'})
        with self.assertRaises(ProviderError): adapter.request([{'role':'user','content':'oi'}])
    def test_no_network_during_configuration_or_construction(self):
        calls=[]
        def fail(*a, **kw): calls.append(1); raise AssertionError()
        make_provider(self.cfg,'openai','gpt-4.1-mini',self.state,transport=fail)
        make_provider(self.cfg,'google','gemini-3.5-flash-lite',self.state,client=SimpleNamespace(models=SimpleNamespace(generate_content=fail)))
        self.assertEqual(calls,[])
        self.assertEqual(self.state.usage()['calls'],0)
    def test_malformed_google_function_call_is_rejected(self):
        response=SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=SimpleNamespace(name=3,args={}))]))],usage_metadata=None)
        adapter=make_provider(self.cfg,'google','gemini-3.5-flash-lite',self.state,client=SimpleNamespace(models=SimpleNamespace(generate_content=lambda **kw:response)))
        with self.assertRaises(ProviderError): adapter.request([{'role':'user','content':'oi'}])
    def test_failure_never_fails_over_and_consumes_one_reservation(self):
        calls=[]
        def fail(body): calls.append('openai'); raise RuntimeError('private-secret')
        adapter=make_provider(self.cfg,'openai','gpt-4.1-mini',self.state,transport=fail)
        with self.assertRaises(ProviderError): adapter.request([{'role':'user','content':'oi'}])
        self.assertEqual(calls,['openai'])
        self.assertEqual(self.state.usage()['calls'],1)
    def test_openai_usage_and_exhaustion(self):
        self.cfg.save('openai','gpt-4.1-mini','private-secret',max_calls=1)
        calls=[]
        def reply(body): calls.append(1); return {'output':[],'usage':{'input_tokens':10,'output_tokens':5}}
        adapter=make_provider(self.cfg,'openai','gpt-4.1-mini',self.state,transport=reply)
        adapter.request([{'role':'user','content':'oi'}])
        with self.assertRaises(BudgetExhausted): adapter.request([{'role':'user','content':'oi'}])
        self.assertEqual(len(calls),1)
        self.assertEqual(self.state.usage()['input_tokens'],10)
        self.assertEqual(self.state.usage()['output_tokens'],5)
