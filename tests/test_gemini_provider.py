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

    def test_final_response_preserves_structured_composition(self):
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='{"opening":"Separei as informações para você.","next_step":"Quer ajuda com mais alguma coisa?","evidence_refs":["E1"]}')]))],
            usage_metadata=None,
        )
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test', client=SimpleNamespace(models=models),
        )

        result = provider.request([{'role': 'user', 'content': 'Use apenas E1.'}], final=True)

        self.assertEqual(result['output'][0]['type'], 'message')
        self.assertNotIn('tools', models.calls[0]['config'])
        self.assertNotIn('tool_config', models.calls[0]['config'])
        self.assertEqual(models.calls[0]['config']['response_mime_type'], 'application/json')
        schema = models.calls[0]['config']['response_json_schema']
        self.assertEqual(schema['type'], 'object')
        self.assertFalse(schema['additionalProperties'])
        self.assertIn('product_order', schema['required'])
        self.assertIn('anyOf', schema['properties']['product_order'])
        self.assertEqual(models.calls[0]['config']['max_output_tokens'], 2048)
        instructions = models.calls[0]['config']['system_instruction']
        self.assertIn('frases curtas', instructions)
        self.assertIn('uma pergunta útil por vez', instructions)
        self.assertIn('heurísticas de comunicação', instructions)

    def test_final_response_captures_safe_sdk_metadata_without_text(self):
        raw_text = '{"opening":"Separei opções.","next_step":"Quer ajuda?","evidence_refs":["E1"]}'
        response = SimpleNamespace(
            candidates=[SimpleNamespace(
                finish_reason='STOP',
                content=SimpleNamespace(parts=[SimpleNamespace(text=raw_text)]),
            )],
            usage_metadata=SimpleNamespace(
                prompt_token_count=41,
                candidates_token_count=14,
                thoughts_token_count=7,
            ),
        )
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test',
            client=SimpleNamespace(models=FakeModels(response)),
        )

        provider.request([{'role': 'user', 'content': 'Use apenas E1.'}], final=True)

        self.assertEqual(provider.last_response_metadata, {
            'sdk_finish_reason': 'STOP',
            'response_text_present': True,
            'response_text_length': len(raw_text),
        })
        self.assertEqual(provider.last_usage['input_tokens'], 41)
        self.assertEqual(provider.last_usage['output_tokens'], 14)
        self.assertEqual(provider.last_usage['thought_tokens'], 7)
        self.assertNotIn(raw_text, str(provider.last_response_metadata))

    def test_low_thinking_level_is_set_only_for_documented_gemini_3_5_models(self):
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='{"opening":"","next_step":"","evidence_refs":[]}')]))],
            usage_metadata=None,
        )
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-3.5-flash-lite', client=SimpleNamespace(models=models),
        )

        provider.request([{'role': 'user', 'content': 'Use only the evidence.'}], final=True)

        self.assertEqual(models.calls[0]['config']['thinking_config'], {'thinking_level': 'LOW'})
        older_models = FakeModels(response)
        older_provider = self.provider_type()(
            api_key='test-key', model='gemini-2.5-flash', client=SimpleNamespace(models=older_models),
        )

        older_provider.request([{'role': 'user', 'content': 'Use only the evidence.'}], final=True)

        self.assertNotIn('thinking_config', older_models.calls[0]['config'])

    def test_final_response_reservation_uses_the_2048_token_ceiling(self):
        models = FakeModels(SimpleNamespace(candidates=[], usage_metadata=None))
        reserved = []
        provider = self.provider_type()(
            api_key='test-key', model='gemini-3.5-flash-lite',
            client=SimpleNamespace(models=models),
            before_request=lambda model, size, output: reserved.append((model, size, output)),
        )

        provider.request([{'role': 'user', 'content': 'Use only the evidence.'}], final=True)

        self.assertEqual(reserved[0][2], 2048)

    def test_final_response_matches_the_installed_sdk_wire_contract(self):
        try:
            from google.genai import types
        except ImportError:
            self.skipTest('google-genai is optional outside the Gemini image')
        response = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(text='{"opening":"","next_step":"","evidence_refs":[]}')]))],
            usage_metadata=None,
        )
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-3.5-flash-lite', client=SimpleNamespace(models=models),
        )

        provider.request([{'role': 'user', 'content': 'Use only the evidence.'}], final=True)
        wire = types.GenerateContentConfig.model_validate(models.calls[0]['config']).model_dump(
            by_alias=True, exclude_none=True)

        self.assertEqual(wire['thinkingConfig']['thinkingLevel'].value, 'LOW')
        self.assertEqual(wire['responseMimeType'], 'application/json')
        self.assertIn('responseJsonSchema', wire)
        self.assertEqual(wire['responseJsonSchema']['type'], 'object')
        self.assertEqual(wire['maxOutputTokens'], 2048)
        self.assertNotIn('tools', wire)
        self.assertNotIn('toolConfig', wire)

    def test_final_response_uses_geminis_ten_second_minimum_deadline(self):
        normal_models = FakeModels(SimpleNamespace(candidates=[], usage_metadata=None))
        final_models = FakeModels(SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(
                text='{"opening":"","next_step":"","evidence_refs":[]}')]))],
            usage_metadata=None))
        clients = [SimpleNamespace(models=normal_models), SimpleNamespace(models=final_models)]
        options_seen = []

        class HttpOptions:
            def __init__(self, **values):
                self.__dict__.update(values)

        class HttpRetryOptions:
            def __init__(self, **values):
                self.__dict__.update(values)

        fake_types = ModuleType('google.genai.types')
        fake_types.HttpOptions = HttpOptions
        fake_types.HttpRetryOptions = HttpRetryOptions
        fake_genai = ModuleType('google.genai')
        def client_factory(*, api_key, http_options):
            options_seen.append((api_key, http_options))
            return clients[len(options_seen) - 1]
        fake_genai.Client = client_factory
        fake_genai.types = fake_types
        fake_google = ModuleType('google')
        fake_google.genai = fake_genai

        with patch.dict(sys.modules, {
            'google': fake_google,
            'google.genai': fake_genai,
            'google.genai.types': fake_types,
        }):
            provider = self.provider_type()(api_key='test-key', model='gemini-test')
            result = provider.request([{'role': 'user', 'content': 'Use only the evidence.'}], final=True)

        self.assertEqual([options.timeout for _, options in options_seen], [25_000, 10_000])
        self.assertEqual([options.retry_options.attempts for _, options in options_seen], [1, 1])
        self.assertNotIn('httpx_client', options_seen[1][1].__dict__)
        self.assertEqual(normal_models.calls, [])
        self.assertEqual(len(final_models.calls), 1)
        self.assertEqual(result['output'][0]['type'], 'message')

    def test_transport_timeout_has_a_sanitized_timeout_category(self):
        import httpx

        class TimingOutModels:
            def generate_content(self, **_kwargs):
                raise httpx.ReadTimeout('synthetic network timeout')

        provider = self.provider_type()(
            api_key='test-key', model='gemini-test',
            client=SimpleNamespace(models=TimingOutModels()),
        )

        with self.assertRaises(httpx.ReadTimeout):
            provider.request([{'role': 'user', 'content': 'Test'}], final=True)

        self.assertEqual(provider.last_failure, 'provider_timeout')

    def test_api_request_rejection_has_a_sanitized_category(self):
        class RequestRejected(Exception):
            code = 400

        class RejectedModels:
            def generate_content(self, **_kwargs):
                raise RequestRejected('synthetic invalid request')

        provider = self.provider_type()(
            api_key='test-key', model='gemini-test',
            client=SimpleNamespace(models=RejectedModels()),
        )

        with self.assertRaises(RequestRejected):
            provider.request([{'role': 'user', 'content': 'Test'}])

        self.assertEqual(provider.last_failure, 'provider_request_rejected')

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
        options = []

        class FakeRetryOptions:
            def __init__(self, **values):
                self.values = values

        class FakeHttpOptions:
            def __init__(self, **values):
                self.values = values

        class FakeClient:
            def __init__(self, *, api_key, http_options):
                options.append({'http': http_options, 'api_key_supplied': bool(api_key)})

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

        self.assertEqual([item['http'].values['timeout'] for item in options], [25_000, 10_000])
        self.assertEqual([item['http'].values['retry_options'].values for item in options],
                         [{'attempts': 1}, {'attempts': 1}])
        self.assertNotIn('httpx_client', options[1]['http'].values)
        self.assertTrue(all(item['api_key_supplied'] for item in options))

    def test_catalog_tool_schema_explains_query_fields_and_budget_filter(self):
        declarations = self._tool_declarations()
        for name in ('search_products', 'recommend_products'):
            tool = next(item for item in declarations if item['name'] == name)
            properties = tool['parameters']['properties']
            self.assertIn('categoria', properties['category'].get('description', '').casefold())
            self.assertIn('nome específico', properties['text'].get('description', '').casefold())
            self.assertIn('não subtraia o frete', properties['max_price'].get('description', '').casefold())

    def test_provider_limits_category_argument_to_snapshot_labels(self):
        response = SimpleNamespace(candidates=[], usage_metadata=None)
        models = FakeModels(response)
        provider = self.provider_type()(
            api_key='test-key', model='gemini-test', client=SimpleNamespace(models=models),
            catalog_categories=['Teclados e Pianos'],
        )

        provider.request([{'role': 'user', 'content': 'Quero opções de teclados.'}])

        declarations = models.calls[0]['config']['tools'][0]['function_declarations']
        search = next(tool for tool in declarations if tool['name'] == 'search_products')
        recommend = next(tool for tool in declarations if tool['name'] == 'recommend_products')
        self.assertEqual(search['parameters_json_schema']['properties']['category']['enum'], ['Teclados e Pianos'])
        self.assertEqual(recommend['parameters_json_schema']['properties']['category']['enum'], ['Teclados e Pianos'])

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
        self.assertEqual(len(models.calls), 2)
        self.assertNotIn('tools', models.calls[1]['config'])
        self.assertNotIn('tool_config', models.calls[1]['config'])
        self.assertEqual(models.calls[1]['config']['response_mime_type'], 'application/json')


if __name__ == '__main__':
    unittest.main()
