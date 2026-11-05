import csv
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.dataset_store import DatasetStore
from artefact.session_store import ChatService, SessionStore
from artefact.services import Catalog
from test_data import write_dataset


def _snapshot(root):
    write_dataset(root)
    additions = {
        'categories': [
            ['2', 'Violões', 'instrumentos de corda'],
            ['3', 'Guitarras', 'instrumentos de corda'],
            ['4', 'Baterias', 'instrumentos de percussão'],
        ],
        'products': [
            ['2', '650.00', 'Yamaha Violão Nylon', '2', 'Violão clássico de nylon', '4', 'active',
             json.dumps({'material': 'nylon', 'strings': '6'}), '2026-01-01'],
            ['3', '720.00', 'Tagima Violão Aço', '2', 'Violão de aço', '2', 'active',
             json.dumps({'material': 'aço', 'strings': '6'}), '2026-01-01'],
            ['4', '900.00', 'Yamaha Guitarra Pacifica', '3', 'Guitarra elétrica', '1', 'active',
             json.dumps({'material': 'mogno'}), '2026-01-01'],
            ['5', '300.00', 'Violão sem estoque', '2', 'Violão de nylon', '0', 'active',
             json.dumps({'material': 'nylon'}), '2026-01-01'],
            ['6', '680.00', 'Rozini Violão Nylon', '2', 'Violão clássico com cordas de nylon', '2', 'active',
             json.dumps({'material': 'nylon', 'strings': '6'}), '2026-01-01'],
            ['7', '820.00', 'Yamaha Violão Nylon Concert', '2', 'Violão de nylon', '1', 'active',
             json.dumps({'material': 'nylon', 'strings': '6'}), '2026-01-01'],
        ],
    }
    for table, rows in additions.items():
        with (root / f'{table}.csv').open('a', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows(rows)
    return load_dataset(root)


def _many_violao_snapshot(root, count=16):
    write_dataset(root)
    with (root / 'categories.csv').open('a', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow(['2', 'Violões', 'instrumentos de corda'])
    with (root / 'products.csv').open('a', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        for product_id in range(2, count + 2):
            writer.writerow([
                str(product_id), str(300 + product_id), f'Violão de Teste {product_id:02d}', '2',
                'Modelo sintético para validação de listagem.', '1', 'active',
                json.dumps({'material': 'nylon'}), '2026-01-01'])
    return load_dataset(root)


class CatalogDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.snapshot = _snapshot(Path(self.temp.name))

    def test_overview_shows_available_categories_counts_and_distinct_examples(self):
        overview = Catalog(self.snapshot).overview()

        self.assertEqual(
            [(row['name'], row['available_count']) for row in overview['categories']],
            [('Guitarras', 1), ('Teclados e Pianos', 1), ('Violões', 4)],
        )
        self.assertEqual(len(overview['examples']), 3)
        self.assertEqual(len({row['category_id'] for row in overview['examples']}), 3)
        self.assertTrue(all(row['available'] for row in overview['examples']))
        self.assertTrue(all(row['price_brl'] is not None for row in overview['examples']))

    def test_search_defaults_to_saleable_stock_and_allows_explicit_unavailable_items(self):
        catalog = Catalog(self.snapshot)

        self.assertEqual({row['product_id'] for row in catalog.search(category='Violões')}, {2, 3, 6, 7})
        self.assertEqual(
            {row['product_id'] for row in catalog.search(category='Violões', available_only=False)},
            {2, 3, 5, 6, 7},
        )

    def test_search_cap_keeps_the_lowest_fifty_checkout_prices(self):
        with TemporaryDirectory() as directory:
            snapshot = _many_violao_snapshot(Path(directory), count=60)

        result = Catalog(snapshot).search(category='Violões', limit=50)

        self.assertEqual(len(result), 50)
        self.assertEqual({item['product_id'] for item in result}, set(range(2, 52)))
        self.assertEqual(
            [item['product_id'] for item in Catalog(snapshot).search(category='Violões', limit=1)],
            [2],
        )

        from artefact.sales import render_sales_results
        answer = render_sales_results(
            [{'consulta': 'search_products', 'resultado': list(result)}],
            'voce tem violão?', snapshot)
        shown = [product_id for product_id in range(2, 62)
                 if f'Violão de Teste {product_id:02d}' in answer]
        self.assertEqual(shown, [2, 3, 4, 5, 6])

    def test_typed_brand_feature_and_price_filters_apply_together(self):
        result = Catalog(self.snapshot).search(
            category='Violões', brand='Yamaha', features=('nylon',), max_price=Decimal('650'),
        )

        self.assertEqual([row['product_id'] for row in result], [2])

    def test_broad_question_uses_catalog_overview_and_keeps_rendered_facts(self):
        class Provider:
            def __init__(self):
                self.calls = []

            def request(self, messages, final=False):
                self.calls.append((messages, final))
                if not final:
                    return {'output': []}
                facts = json.loads(messages[0]['content'])['evidence'][0]['validated_facts']
                return {'output': [{
                    'type': 'message',
                    'content': [{'type': 'output_text', 'text': json.dumps({
                        'opening': 'Separei algumas opções para você.',
                        'next_step': 'Que tipo de instrumento ou categoria te interessa?',
                        'evidence_refs': ['E1'],
                    })}],
                }]}

        provider = Provider()
        agent = ConversationAgent(self.snapshot, provider)

        answer = agent.answer('O que vocês têm?', chat_id='discovery')

        self.assertIn('Violões', answer)
        self.assertIn('Yamaha Violão Nylon', answer)
        self.assertIn('R$ 650,00', answer)
        self.assertIn('Que tipo de instrumento', answer)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(provider.calls[0][1])
        self.assertEqual(agent.last_trace['executed_tools'], ['catalog_overview'])

    def test_explicit_category_availability_query_never_includes_out_of_stock(self):
        class Provider:
            def request(self, messages, final=False):
                if final:
                    return {'output': []}
                return {'output': []}

        agent = ConversationAgent(self.snapshot, Provider())
        answer = agent.answer('Quais violões vocês têm para vender?', chat_id='category')

        self.assertIn('Yamaha Violão Nylon', answer)
        self.assertIn('Tagima Violão Aço', answer)
        self.assertNotIn('Violão sem estoque', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])

    def test_direct_question_about_named_out_of_stock_product_reports_its_status(self):
        class Provider:
            def request(self, messages, final=False):
                if final:
                    return {'output': []}
                return {'output': [{'type': 'function_call', 'name': 'product_detail',
                                    'arguments': json.dumps({'product_id': 5}), 'call_id': 'detail'}]}

        agent = ConversationAgent(self.snapshot, Provider())
        answer = agent.answer('Vocês têm o Violão sem estoque?', chat_id='explicit-unavailable')

        self.assertIn('Violão sem estoque', answer)
        self.assertIn('indisponível', answer)
        self.assertNotIn('Yamaha Violão Nylon', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['product_detail'])

    def test_pending_search_refines_terms_and_budget_without_losing_category(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        agent = ConversationAgent(self.snapshot, Provider())
        agent.answer('Quais violões vocês têm para vender?', chat_id='refine')

        nylon = agent.answer('nylon', chat_id='refine')
        self.assertIn('Yamaha Violão Nylon', nylon)
        self.assertIn('Rozini Violão Nylon', nylon)
        self.assertNotIn('Tagima Violão Aço', nylon)

        yamaha = agent.answer('Yamaha', chat_id='refine')
        self.assertIn('Yamaha Violão Nylon', yamaha)
        self.assertNotIn('Rozini Violão Nylon', yamaha)

        budget = agent.answer('até 700', chat_id='refine')
        self.assertIn('Yamaha Violão Nylon', budget)
        self.assertNotIn('Yamaha Violão Nylon Concert', budget)

    def test_pending_search_paginates_five_at_a_time_and_supports_full_list_and_tenth_ordinal(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        with TemporaryDirectory() as directory:
            snapshot = _many_violao_snapshot(Path(directory))
            agent = ConversationAgent(snapshot, Provider())
            first = agent.answer('Quais violões vocês têm para vender?', chat_id='pages')
            second = agent.answer('mais opções', chat_id='pages')
            detail = agent.answer('o quinto', chat_id='pages')
            detail_tools = list(agent.last_trace['executed_tools'])
            complete = agent.answer('lista completa', chat_id='pages')
            tenth = agent.answer('o décimo', chat_id='pages')

        first_names = [f'Violão de Teste {product_id:02d}' for product_id in range(2, 7)]
        second_names = [f'Violão de Teste {product_id:02d}' for product_id in range(7, 12)]
        self.assertTrue(all(name in first for name in first_names))
        self.assertTrue(all(f'Violão de Teste {product_id:02d}' not in first
                            for product_id in range(7, 18)))
        self.assertIn('mais opções', first.lower())
        self.assertTrue(all(name in second for name in second_names))
        self.assertTrue(all(name not in second for name in first_names))
        self.assertIn('Violão de Teste 11', detail)
        self.assertEqual(sum(f'Violão de Teste {product_id:02d}' in complete
                             for product_id in range(2, 18)), 16)
        self.assertLess(complete.index('Violão de Teste 02'), complete.index('Violão de Teste 03'))
        self.assertLess(len(complete), 64 * 1024)
        self.assertIn('Violão de Teste 11', tenth)
        self.assertEqual(detail_tools, ['product_detail'])

    def test_simple_violao_discovery_is_bounded_for_both_accents_and_fallback(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_dataset(root)
            with (root / 'categories.csv').open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerow(['2', 'Violões', 'instrumentos de corda'])
            with (root / 'products.csv').open('a', encoding='utf-8', newline='') as stream:
                writer = csv.writer(stream)
                for product_id in range(2, 37):
                    status = 'coming_soon' if product_id in {2, 3} else 'active'
                    writer.writerow([
                        str(product_id), str(300 + product_id),
                        f'Violão de Teste {product_id:02d}', '2',
                        'Modelo sintético para validação de listagem.', '2', status,
                        '{}', '2026-01-01'])
            snapshot = load_dataset(root)

            class NoInterpretationProvider:
                def request(self, messages):
                    return {'output': []}

            for question in ('voce tem violão?', 'voce tem violao?'):
                with self.subTest(question=question):
                    agent = ConversationAgent(snapshot, NoInterpretationProvider())
                    answer = agent.answer(question, chat_id=question)
                    displayed = [f'Violão de Teste {product_id:02d}'
                                 for product_id in range(2, 37)
                                 if f'Violão de Teste {product_id:02d}' in answer]
                    self.assertEqual(len(displayed), 5)
                    self.assertNotIn('Violão de Teste 02', answer)
                    self.assertNotIn('Violão de Teste 03', answer)
                    self.assertLess(len(answer), 64 * 1024)
                    self.assertIn('mais opções', answer.lower())
                    self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])

            from artefact.sales import render_sales_results
            mixed = Catalog(snapshot).search(category='Violões', available_only=False, limit=50)
            fallback = render_sales_results(
                [{'consulta': 'search_products', 'resultado': list(mixed)}],
                'voce tem violao?', snapshot)
            fallback_names = [f'Violão de Teste {product_id:02d}'
                              for product_id in range(2, 37)
                              if f'Violão de Teste {product_id:02d}' in fallback]
            self.assertEqual(len(fallback_names), 5)
            self.assertNotIn('Violão de Teste 02', fallback)
            self.assertNotIn('Violão de Teste 03', fallback)
            self.assertLess(len(fallback), 64 * 1024)

    def test_rejecting_selected_page_item_then_requesting_more_does_not_repeat(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        with TemporaryDirectory() as directory:
            snapshot = _many_violao_snapshot(Path(directory), count=8)
            agent = ConversationAgent(snapshot, Provider())
            first = agent.answer('Quais violões vocês têm para vender?', chat_id='reject')
            agent.answer('o segundo', chat_id='reject')
            replacement = agent.answer('não quero esse', chat_id='reject')
            next_page = agent.answer('mais opções', chat_id='reject')

        self.assertNotIn('Violão de Teste 07', first)
        self.assertIn('Violão de Teste 07', replacement)
        self.assertTrue(all(f'Violão de Teste {product_id:02d}' not in replacement
                            for product_id in range(2, 7)))
        self.assertIn('Já mostrei todas as opções', next_page)

    def test_final_prose_cannot_swap_prices_or_invent_availability_and_terms(self):
        malicious_replies = (
            'Yamaha Violão Nylon custa R$ 720,00.',
            'Yamaha Violão Nylon está indisponível, com frete grátis e garantia vitalícia.',
        )
        for reply in malicious_replies:
            with self.subTest(reply=reply):
                class Provider:
                    def request(self, messages, final=False):
                        if not final:
                            return {'output': []}
                        return {'output': [{'type': 'message', 'content': [{
                            'type': 'output_text', 'text': json.dumps({
                                'opening': reply,
                                'next_step': 'Qual dessas opções você prefere?',
                                'evidence_refs': ['E1'],
                            }),
                        }]}]}

                agent = ConversationAgent(self.snapshot, Provider())
                answer = agent.answer('Quais violões vocês têm para vender?', chat_id='guard')

                self.assertIn('Yamaha Violão Nylon: R$ 650,00', answer)
                self.assertIn('Tagima Violão Aço: R$ 720,00', answer)
                self.assertNotIn('frete grátis', answer.lower())
                self.assertNotIn('garantia vitalícia', answer.lower())
                self.assertEqual(agent.last_trace['final_response_status'], 'invalid')

    def test_invalid_final_response_trace_logs_only_structured_reason_code(self):
        rejected_text = 'Yamaha custa.'

        class Provider:
            def request(self, messages, final=False):
                response_text = json.dumps({
                        'opening': rejected_text,
                        'next_step': 'Qual dessas opções você prefere?',
                        'evidence_refs': ['E1'],
                    })
                if final:
                    self.last_response_metadata = {
                        'sdk_finish_reason': 'STOP',
                        'response_text_present': True,
                        'response_text_length': len(response_text),
                    }
                    return {'output': [{'type': 'message', 'content': [{
                        'type': 'output_text', 'text': response_text,
                    }]}], 'usage': {
                        'input_tokens': 41, 'output_tokens': 14, 'thought_tokens': 7,
                    }}
                return {'output': []}

        agent = ConversationAgent(self.snapshot, Provider())
        with self.assertLogs('artefact.operations', level='WARNING') as captured:
            answer = agent.answer('Quais violões vocês têm para vender?', chat_id='reason-code')

        log_text = '\n'.join(captured.output)
        self.assertEqual(agent.last_trace['final_response_status'], 'invalid')
        self.assertEqual(agent.last_trace['final_response_rejection_code'],
                         'opening_contains_commercial_claim')
        self.assertEqual(agent.last_trace['final_response_diagnostics']['guard'], 'commercial_claim')
        self.assertEqual(agent.last_trace['final_response_diagnostics']['field'], 'opening')
        self.assertEqual(agent.last_trace['final_response_diagnostics']['sdk_finish_reason'], 'STOP')
        self.assertEqual(agent.last_trace['final_response_diagnostics']['input_tokens'], 41)
        self.assertEqual(agent.last_trace['final_response_diagnostics']['output_tokens'], 14)
        self.assertEqual(agent.last_trace['final_response_diagnostics']['thought_tokens'], 7)
        self.assertEqual(agent.last_trace['final_response_diagnostics']['validated_evidence_refs'], ['E1'])
        self.assertIn('"reason_code": "opening_contains_commercial_claim"', log_text)
        self.assertIn('"guard": "commercial_claim"', log_text)
        self.assertIn('"field": "opening"', log_text)
        self.assertIn('"sdk_finish_reason": "STOP"', log_text)
        self.assertIn('"thought_tokens": 7', log_text)
        self.assertIn('"response_text_present": true', log_text)
        self.assertIn('"validated_evidence_refs": ["E1"]', log_text)
        self.assertNotIn(rejected_text, log_text)
        self.assertNotIn('999', log_text)
        self.assertIn('Yamaha Violão Nylon: R$ 650,00', answer)

    def test_model_can_reorder_product_blocks_by_validated_ids(self):
        class Provider:
            def request(self, messages, final=False):
                if not final:
                    return {'output': []}
                prompt = json.loads(messages[0]['content'])
                ids = [item['product_id'] for item in prompt['selectable_products']]
                order = ids if prompt['customer_message'] == 'mais opções' else [ids[-1]]
                return {'output': [{'type': 'message', 'content': [{
                    'type': 'output_text', 'text': json.dumps({
                        'opening': 'Encontrei algumas opções para você.',
                        'next_step': 'Qual dessas opções combina mais com o que você procura?',
                        'evidence_refs': ['E1'],
                        'product_order': list(reversed(ids)),
                    }),
                }]}]}

        agent = ConversationAgent(self.snapshot, Provider())
        answer = agent.answer('Quais violões vocês têm para vender?', chat_id='order')

        self.assertLess(answer.index('Tagima Violão Aço'), answer.index('Rozini Violão Nylon'))
        self.assertLess(answer.index('Rozini Violão Nylon'), answer.index('Yamaha Violão Nylon:'))
        self.assertIn('Tagima Violão Aço: R$ 720,00', answer)
        self.assertIn('Yamaha Violão Nylon: R$ 650,00', answer)
        self.assertTrue(answer.endswith('Qual dessas opções combina mais com o que você procura?'))
        self.assertEqual(agent.last_trace['final_response_status'], 'accepted')
        detail = agent.answer('o primeiro', chat_id='order')
        self.assertIn('Yamaha Violão Nylon Concert', detail)
        self.assertNotIn('Tagima Violão Aço', detail)

    def test_model_cannot_shrink_the_default_five_product_page(self):
        class Provider:
            def request(self, messages, final=False):
                if not final:
                    return {'output': []}
                prompt = json.loads(messages[0]['content'])
                ids = [item['product_id'] for item in prompt['selectable_products']]
                return {'output': [{'type': 'message', 'content': [{
                    'type': 'output_text', 'text': json.dumps({
                        'opening': 'Separei uma opção para você.',
                        'next_step': 'Qual delas combina mais com o que você procura?',
                        'evidence_refs': ['E1'],
                        'product_order': [ids[-1]],
                    }),
                }]}]}

        with TemporaryDirectory() as directory:
            snapshot = _many_violao_snapshot(Path(directory), count=8)
            agent = ConversationAgent(snapshot, Provider())
            answer = agent.answer('Quais violões vocês têm para vender?', chat_id='five-required')

        shown = [f'Violão de Teste {product_id:02d}'
                 for product_id in range(2, 10)
                 if f'Violão de Teste {product_id:02d}' in answer]
        self.assertEqual(len(shown), 5)
        self.assertEqual(agent.last_trace['final_response_status'], 'invalid')

    def test_model_selected_products_can_page_hidden_valid_results_without_repeats(self):
        class Provider:
            def request(self, messages, final=False):
                if not final:
                    return {'output': []}
                prompt = json.loads(messages[0]['content'])
                ids = [item['product_id'] for item in prompt['selectable_products']]
                product_order = (ids if prompt['customer_message'] == 'mais opções'
                                 else [ids[-1]] if len(ids) > 1 else ids)
                return {'output': [{'type': 'message', 'content': [{
                    'type': 'output_text', 'text': json.dumps({
                        'opening': 'Separei uma opção para começar.',
                        'next_step': 'Quer ver outras opções?',
                        'evidence_refs': ['E1'],
                        'product_order': product_order,
                    }),
                }]}]}

        agent = ConversationAgent(self.snapshot, Provider())
        first = agent.answer('Quais violões vocês têm para vender?', chat_id='select-subset')
        next_page = agent.answer('mais opções', chat_id='select-subset')

        self.assertIn('Yamaha Violão Nylon Concert', first)
        self.assertNotIn('Yamaha Violão Nylon:', first)
        self.assertIn('Yamaha Violão Nylon', next_page)
        self.assertIn('Rozini Violão Nylon', next_page)
        self.assertIn('Tagima Violão Aço', next_page)
        self.assertNotIn('Yamaha Violão Nylon Concert', next_page)

    def test_model_order_survives_serialized_chat_service_reconstruction(self):
        class Provider:
            def request(self, messages, final=False):
                if not final:
                    return {'output': []}
                prompt = json.loads(messages[0]['content'])
                selectable = [item['product_id'] for item in prompt['selectable_products']]
                response = {
                    'opening': 'Separei estas opções para você.',
                    'next_step': 'Qual delas combina mais com o que você procura?',
                    'evidence_refs': ['E1'],
                }
                if selectable:
                    response['product_order'] = list(reversed(selectable))
                return {'output': [{'type': 'message', 'content': [{
                    'type': 'output_text', 'text': json.dumps(response),
                }]}]}

        with TemporaryDirectory() as root:
            root = Path(root)
            snapshot_path = root / 'dataset-files'
            snapshot_path.mkdir()
            _snapshot(snapshot_path)
            dataset = DatasetStore(root / 'catalog.sqlite')
            files = [(path.name, path.read_bytes()) for path in snapshot_path.glob('*.csv')]
            dataset.import_files(files)
            chat_path = root / 'chat.sqlite'
            make_chat = lambda: ChatService(
                SessionStore(chat_path), dataset.load, lambda _provider, _model: Provider(),
                dataset.active_version, lambda: ('google', 'synthetic'))
            session_id = make_chat().create()['id']

            first = make_chat().send(session_id, 'Quais violões vocês têm para vender?')['answer']
            detail = make_chat().send(session_id, 'o primeiro')['answer']

        self.assertLess(first.index('Tagima Violão Aço'), first.index('Yamaha Violão Nylon:'))
        self.assertIn('Yamaha Violão Nylon Concert', detail)
        self.assertNotIn('Tagima Violão Aço', detail)

    def test_ordinal_selection_opens_grounded_product_details(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        agent = ConversationAgent(self.snapshot, Provider())
        agent.answer('Quais violões vocês têm para vender?', chat_id='select')

        answer = agent.answer('o terceiro', chat_id='select')

        self.assertIn('Tagima Violão Aço', answer)
        self.assertIn('Especificações cadastradas', answer)
        self.assertIn('material: aço', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['product_detail'])

    def test_bare_budget_reply_refines_saved_category_across_service_instances(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_files = root / 'dataset-files'
            dataset_files.mkdir()
            _snapshot(dataset_files)
            dataset = DatasetStore(root / 'catalog.sqlite')
            dataset.import_files([(path.name, path.read_bytes()) for path in dataset_files.glob('*.csv')])
            chat_path = root / 'chat.sqlite'
            def make_chat():
                return ChatService(SessionStore(chat_path), dataset.load,
                    lambda _provider, _model: Provider(), dataset.active_version,
                    lambda: ('google', 'synthetic'))

            session_id = make_chat().create()['id']
            first = make_chat().send(session_id, 'Quais violões vocês têm disponíveis?')['answer']
            context = make_chat().store.get(session_id)['context']
            self.assertIn('Qual faixa de preço você tem em mente?', first)
            self.assertTrue(context.awaiting_budget)
            self.assertEqual(context.pending_search.category, 'Violões')

            answer = make_chat().send(session_id, '700')['answer']
            saved = make_chat().store.get(session_id)['context']

        self.assertIn('Yamaha Violão Nylon', answer)
        self.assertNotIn('Yamaha Violão Nylon Concert', answer)
        self.assertNotIn('Qual faixa de preço você tem em mente?', answer)
        self.assertEqual(saved.pending_search.max_price, Decimal('700'))

    def test_empty_search_requests_filter_confirmation_then_offers_seller_help(self):
        class Provider:
            def request(self, messages, final=False):
                return {'output': []}

        agent = ConversationAgent(self.snapshot, Provider())
        first = agent.answer('Quais baterias vocês têm?', chat_id='empty')
        self.assertIn('Só amplio a busca depois da sua confirmação', first)
        second = agent.answer('nylon', chat_id='empty')
        self.assertIn('peça ajuda a um vendedor', second)
        self.assertNotIn('Yamaha Violão Nylon', second)


if __name__ == '__main__':
    unittest.main()
