import tempfile
import unittest
from pathlib import Path
from artefact.provider_config import ProviderConfig

class ProviderConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        p = Path(self.temp.name)
        self.db, self.master = p/'config.db', p/'secret'/'master.key'
        self.config = ProviderConfig(self.db, self.master)

    def tearDown(self):
        self.temp.cleanup()

    def test_credentials_are_encrypted_and_survive_rotation(self):
        self.config.save('google', 'gemini-3.5-flash-lite', api_key='secret-test-value')
        from artefact.state import State
        from types import SimpleNamespace
        class Models:
            def generate_content(self, **kw): return SimpleNamespace(candidates=[], usage_metadata=None)
        self.config.test_connection('google', State(Path(self.temp.name)/'usage.db'), client=SimpleNamespace(models=Models()))
        self.config.activate('google')
        self.assertNotIn(b'secret-test-value', self.db.read_bytes())
        self.assertNotIn('secret-test-value', repr(self.config.status()))
        self.config.rotate_master_key()
        reloaded = ProviderConfig(self.db, self.master)
        self.assertEqual(reloaded.credentials('google'), 'secret-test-value')
        self.assertIsNone(reloaded.status()['active'])
        reloaded.remove_key('google')
        self.assertIsNone(reloaded.status()['active'])
        with self.assertRaises(ValueError): reloaded.credentials('google')

    def test_unknown_model_or_missing_key_cannot_activate(self):
        with self.assertRaises(ValueError): self.config.save('google', 'invented', api_key='key')
        self.config.save('openai', 'gpt-4.1-mini')
        with self.assertRaises(ValueError): self.config.activate('openai')

    def test_lost_key_is_not_silently_replaced(self):
        self.config.save('google', 'gemini-3.5-flash-lite', api_key='key')
        self.master.unlink()
        with self.assertRaises(ValueError): ProviderConfig(self.db, self.master)

    def test_partial_save_preserves_existing_limits_key_and_consumption(self):
        from artefact.state import State
        state = State(Path(self.temp.name)/'usage.db')
        self.config.save('openai', 'gpt-4.1-mini', 'saved-key', max_calls=23,
                         max_usd='2.4', input_price='0.15', output_price='0.6')
        self.config.test_connection('openai', state, transport=lambda body: {
            'output': [], 'usage': {'input_tokens': 10, 'output_tokens': 4}})
        before = state.usage()
        self.config.save_partial('openai', 'gpt-4.1-nano')
        self.assertEqual(self.config.credentials('openai'), 'saved-key')
        saved = self.config.status()['providers']['openai']
        self.assertEqual([saved[k] for k in ('max_calls','max_usd','input_price','output_price')],
                         [23, '2.4', '0.15', '0.6'])
        self.assertEqual(state.usage(), before)
        self.assertFalse(saved['verified'])
        self.config.save_partial('openai', 'gpt-4.1-nano', max_usd='3.5')
        self.assertEqual(self.config.status()['providers']['openai']['max_usd'], '3.5')
        self.assertEqual(self.config.status()['providers']['openai']['max_calls'], 23)

    def test_partial_save_uses_defaults_only_for_new_provider(self):
        self.config.save_partial('google', 'gemini-3.5-flash-lite', 'new-key')
        saved = self.config.status()['providers']['google']
        self.assertEqual([saved[k] for k in ('max_calls','max_usd','input_price','output_price')],
                         [100, '1', '1', '5'])

    def test_partial_save_rejects_invalid_fields_without_changing_settings(self):
        self.config.save('openai', 'gpt-4.1-mini', 'original', max_calls=9)
        before = self.config.status()
        for values in ({'max_calls': 0}, {'max_usd': 'NaN'}, {'input_price': None},
                       {'unknown': 'value'}, {'api_key': ''}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.config.save_partial('openai', 'gpt-4.1-nano', **values)
        self.assertEqual(self.config.status(), before)
        self.assertEqual(self.config.credentials('openai'), 'original')

    def test_partial_save_keeps_verified_active_provider_for_unchanged_credentials(self):
        from artefact.state import State
        state = State(Path(self.temp.name)/'usage.db')
        self.config.save('openai', 'gpt-4.1-mini', 'original')
        self.config.test_connection('openai', state, transport=lambda body: {'output': []})
        self.config.activate('openai')
        before = state.usage()
        for changes in ({}, {'max_calls': 25}, {'api_key': ' original ', 'max_usd': '4'}):
            with self.subTest(changes=changes):
                self.config.save_partial('openai', 'gpt-4.1-mini', **changes)
                status = self.config.status()
                self.assertEqual(status['active'], 'openai')
                self.assertTrue(status['providers']['openai']['verified'])
                self.config.activate('openai')
                self.assertEqual(state.usage(), before)

    def test_partial_save_invalidates_verified_active_provider_for_changed_credentials(self):
        from artefact.state import State
        state = State(Path(self.temp.name)/'usage.db')
        for model,key in (('gpt-4.1-nano',None), ('gpt-4.1-mini','replacement')):
            with self.subTest(model=model,key=key):
                self.config.save('openai', 'gpt-4.1-mini', 'original')
                self.config.test_connection('openai', state, transport=lambda body: {'output': []})
                self.config.activate('openai')
                self.config.save_partial('openai', model, key)
                status = self.config.status()
                self.assertIsNone(status['active'])
                self.assertFalse(status['providers']['openai']['verified'])
                with self.assertRaises(ValueError): self.config.activate('openai')

if __name__ == '__main__': unittest.main()

class ProviderFactoryTests(unittest.TestCase):
    def test_google_factory_reserves_and_records_usage(self):
        from types import SimpleNamespace
        from artefact.provider import make_provider
        from artefact.state import State, BudgetExhausted
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'config.db', p/'key')
            cfg.save('google', 'gemini-3.5-flash-lite', api_key='fake', max_calls=1)
            state = State(p/'usage.db')
            class Models:
                def generate_content(self, **kw):
                    return SimpleNamespace(candidates=[], usage_metadata=SimpleNamespace(prompt_token_count=10, candidates_token_count=5))
            provider = make_provider(cfg, 'google', 'gemini-3.5-flash-lite', state, client=SimpleNamespace(models=Models()))
            provider.request([{'role': 'user', 'content': 'hello'}])
            self.assertEqual(state.usage()['calls'], 1)
            self.assertEqual(state.usage()['input_tokens'], 10)
            with self.assertRaises(BudgetExhausted): provider.request([{'role': 'user', 'content': 'hello'}])
    def test_factory_sanitizes_provider_failure(self):
        from artefact.provider import make_provider, ProviderError
        from artefact.state import State
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'db', p/'key')
            cfg.save('openai', 'gpt-4.1-mini', api_key='private-secret')
            def fail(_): raise RuntimeError('private-secret')
            provider = make_provider(cfg, 'openai', 'gpt-4.1-mini', State(p/'usage'), transport=fail)
            with self.assertRaises(ProviderError) as caught:
                provider.request([{'role':'user', 'content':'hello'}])
            self.assertNotIn('private-secret', str(caught.exception))
    def test_connection_test_requires_explicit_request(self):
        from artefact.provider import test_connection
        with self.assertRaises(ValueError):
            test_connection(None, None, None, None)
    def test_explicit_connection_marks_model_verified_and_key_change_invalidates(self):
        from artefact.state import State
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'db', p/'key')
            cfg.save('openai', 'gpt-4.1-mini', 'first')
            fake = lambda body: {'output': [], 'usage': {'input_tokens': 1, 'output_tokens': 1}}
            cfg.test_connection('openai', State(p/'usage'), transport=fake)
            self.assertTrue(cfg.status()['providers']['openai']['verified'])
            cfg.save('openai', 'gpt-4.1-mini', 'second')
            self.assertFalse(cfg.status()['providers']['openai']['verified'])
    def test_external_curated_catalog_controls_allowed_models(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            catalog = p/'catalog.json'
            catalog.write_text(json.dumps({'google': [{'id':'curated-model', 'function_calling': True, 'reference':'https://ai.google.dev/models/curated'}], 'openai': [{'id':'gpt-4.1-mini', 'function_calling': True, 'reference':'https://developers.openai.com/api/docs/models/gpt-4.1-mini'}]}))
            cfg = ProviderConfig(p/'db', p/'key', catalog_path=catalog)
            cfg.save('google', 'curated-model', 'fake')
            self.assertEqual(cfg.models()['google'], ['curated-model'])
            with self.assertRaises(ValueError): cfg.save('google', 'invented', 'fake')
            catalog.write_text('{}')
            with self.assertRaises(ValueError): cfg.models()
    def test_simultaneous_bootstrap_uses_one_complete_master_key(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            def create(_): return ProviderConfig(p/'db', p/'master')
            with ThreadPoolExecutor(max_workers=8) as executor:
                configs = list(executor.map(create, range(16)))
            configs[0].save('openai', 'gpt-4.1-mini', 'fake')
            self.assertTrue(all(cfg.credentials('openai') == 'fake' for cfg in configs))
    def test_provider_value_error_is_sanitized(self):
        from artefact.provider import make_provider, ProviderError
        from artefact.state import State
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'db', p/'key')
            cfg.save('openai', 'gpt-4.1-mini', 'private-secret')
            def fail(_): raise ValueError('private-secret')
            with self.assertRaises(ProviderError) as error:
                make_provider(cfg, 'openai', 'gpt-4.1-mini', State(p/'usage'), transport=fail).request([{'role':'user', 'content':'hello'}])
            self.assertNotIn('private-secret', str(error.exception))
    def test_credential_read_is_safe_during_master_rotation(self):
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'db', p/'key')
            cfg.save('openai', 'gpt-4.1-mini', 'fake')
            rotating = ProviderConfig(p/'db', p/'key')
            captured = threading.Event()
            resume = threading.Event()
            original = cfg.secrets.decrypt
            def delayed(value):
                captured.set()
                resume.wait(3)
                return original(value)
            cfg.secrets.decrypt = delayed
            reader_result = []
            def read():
                try: reader_result.append(cfg.credentials('openai'))
                except Exception as exc: reader_result.append(exc)
            reader = threading.Thread(target=read)
            reader.start()
            self.assertTrue(captured.wait(3))
            rotation = threading.Thread(target=rotating.rotate_master_key)
            rotation.start()
            rotation.join(.3)
            resume.set()
            reader.join(5); rotation.join(5)
            self.assertEqual(reader_result, ['fake'])
            self.assertFalse(rotation.is_alive())
    def test_activation_requires_explicit_successful_connection_test(self):
        from artefact.state import State
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            cfg = ProviderConfig(p/'db', p/'key')
            cfg.save('openai', 'gpt-4.1-mini', 'fake')
            with self.assertRaises(ValueError): cfg.activate('openai')
            cfg.test_connection('openai', State(p/'usage'), transport=lambda body: {'output': [], 'usage': {'input_tokens':1,'output_tokens':1}})
            cfg.activate('openai')
            self.assertEqual(cfg.status()['active'], 'openai')
    def test_replacing_active_credentials_deactivates_until_reverified(self):
        from artefact.state import State
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); cfg=ProviderConfig(p/'db',p/'key')
            cfg.save('openai','gpt-4.1-mini','first')
            cfg.test_connection('openai', State(p/'usage'), transport=lambda body: {'output': []})
            cfg.activate('openai')
            cfg.save('openai','gpt-4.1-mini','second')
            self.assertIsNone(cfg.status()['active'])
