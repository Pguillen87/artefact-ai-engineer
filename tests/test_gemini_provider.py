import unittest
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

from artefact import provider as provider_module
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from test_data import write_dataset


class FakeModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({'model': model, 'contents': contents, 'config': config})
        return self.response


class GeminiProviderTests(unittest.TestCase):
    def provider_type(self):
        provider_type = getattr(provider_module, 'GeminiProvider', None)
        self.assertIsNotNone(provider_type, 'GeminiProvider adapter is not implemented')
        return provider_type

    def test_function_calls_are_returned_for_python_validation_without_sdk_execution(self):
        function_call = SimpleNamespace(
            id='model-call-1',
            name='search_products',
            args={'text': 'Korg Synth 1 Pro'},
        )
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function_call)]))],
            usage_metadata=SimpleNamespace(
                prompt_token_count=120,
                candidates_token_count=22,
                thoughts_token_count=4,
                total_token_count=146,
            ),
        )
        models = FakeModels(response)
        reserved = []
        provider = self.provider_type()(
            api_key='test-key',
            model='gemini-test',
            client=SimpleNamespace(models=models),
            before_request=lambda model, size, output: reserved.append((model, size, output)),
        )

        result = provider.request([{'role': 'user', 'content': 'Qual o preço do Korg?'}])

        self.assertEqual(result['output'], [{
            'type': 'function_call',
            'name': 'search_products',
            'call_id': 'model-call-1',
            'arguments': '{"text": "Korg Synth 1 Pro"}',
        }])
        self.assertEqual(result['usage'], {
            'input_tokens': 120,
            'output_tokens': 22,
            'thought_tokens': 4,
            'total_tokens': 146,
        })
        self.assertEqual(len(models.calls), 1)
        self.assertEqual(len(reserved), 1)
        self.assertEqual(models.calls[0]['contents'], [{
            'role': 'user', 'parts': [{'text': 'Qual o preço do Korg?'}],
        }])
        config = models.calls[0]['config']
        self.assertTrue(config['automatic_function_calling']['disable'])
        declarations = config['tools'][0]['function_declarations']
        self.assertTrue(any(tool['name'] == 'search_products' for tool in declarations))
        self.assertTrue(all('parameters_json_schema' in tool for tool in declarations))

    def test_response_without_function_calls_returns_empty_plan(self):
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='texto livre')]))],
            usage_metadata=None,
        )
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test', client=SimpleNamespace(models=models),
        )

        result = provider.request([{'role': 'user', 'content': 'Oi'}])

        self.assertEqual(result['output'], [])
        self.assertEqual(result['usage'], {})
        self.assertEqual(len(models.calls), 1)

    def test_failed_request_does_not_reuse_previous_turn_usage(self):
        class FailingModels:
            def generate_content(self, **_kwargs):
                raise RuntimeError('synthetic provider failure')

        provider = self.provider_type()(
            api_key='test-key', model='gemini-test',
            client=SimpleNamespace(models=FailingModels()),
        )
        provider.last_usage = {'input_tokens': 99, 'output_tokens': 10}

        with self.assertRaises(RuntimeError):
            provider.request([{'role': 'user', 'content': 'Oi'}])

        self.assertEqual(provider.last_usage, {})
        self.assertEqual(provider.last_failure, 'provider_failed')

    def test_sdk_client_uses_a_single_attempt_without_automatic_retries(self):
        google_module = ModuleType('google')
        google_module.__path__ = []
        genai_module = ModuleType('google.genai')
        types_module = ModuleType('google.genai.types')
        options = {}

        class FakeRetryOptions:
            def __init__(self, **values):
                options['retry'] = values
                options['retry_object'] = self

        class FakeHttpOptions:
            def __init__(self, **values):
                options['http'] = values

        class FakeClient:
            def __init__(self, *, api_key, http_options):
                options['client_http_options'] = http_options
                options['api_key_supplied'] = bool(api_key)

        types_module.HttpRetryOptions = FakeRetryOptions
        types_module.HttpOptions = FakeHttpOptions
        genai_module.Client = FakeClient
        google_module.genai = genai_module
        with patch.dict(sys.modules, {
            'google': google_module,
            'google.genai': genai_module,
            'google.genai.types': types_module,
        }):
            self.provider_type()(api_key='test-key', model='gemini-test')

        self.assertEqual(options['retry'], {'attempts': 1})
        self.assertEqual(options['http']['timeout'], 25_000)
        self.assertIs(options['http']['retry_options'], options['retry_object'])
        self.assertTrue(options['api_key_supplied'])

    def test_catalog_tool_schema_explains_query_fields_and_budget_filter(self):
        declarations = self._tool_declarations()
        for name in ('search_products', 'recommend_products'):
            tool = next(item for item in declarations if item['name'] == name)
            properties = tool['parameters']['properties']
            self.assertIn('categoria', properties['category'].get('description', '').casefold())
            self.assertIn('nome específico', properties['text'].get('description', '').casefold())
            self.assertIn('não subtraia o frete', properties['max_price'].get('description', '').casefold())

    @staticmethod
    def _tool_declarations():
        from artefact.agent import TOOLS
        return [tool for tool in TOOLS if tool.get('type') == 'function']

    def test_oversized_request_is_rejected_before_budget_reservation_or_network(self):
        models = FakeModels(SimpleNamespace(candidates=[], usage_metadata=None))
        reserved = []
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test', client=SimpleNamespace(models=models),
            before_request=lambda *args: reserved.append(args), max_request_bytes=100,
        )

        with self.assertRaises(ValueError):
            provider.request([{'role': 'user', 'content': 'x' * 1000}])

        self.assertEqual(models.calls, [])
        self.assertEqual(reserved, [])

    def test_gemini_proposal_runs_through_existing_python_validator_and_renderer(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(root, {'products': {'name': 'Korg Synth 1 Pro'}})
            snapshot = load_dataset(root)
        function_call = SimpleNamespace(
            id='model-call-2',
            name='search_products',
            args={'text': 'Korg Synth 1 Pro'},
        )
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(function_call=function_call)]))],
            usage_metadata=None,
        )
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test', client=SimpleNamespace(models=models),
        )

        agent = ConversationAgent(snapshot, provider)
        answer = agent.answer('Qual o preço do Korg Synth 1 Pro?', chat_id='synthetic-session')

        self.assertIn('Korg Synth 1 Pro', answer)
        self.assertEqual(agent.last_trace['selected_tools'], ['search_products'])
        self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])
        self.assertEqual(len(models.calls), 1)


if __name__ == '__main__':
    unittest.main()
