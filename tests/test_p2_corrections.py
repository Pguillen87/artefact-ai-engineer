import csv
import json
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from test_data import ROWS, write_dataset
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.services import Catalog, OrderService
from baseline_support import load_original_baseline


def product_row(product_id, price, name, stock=1, status='active'):
    return [str(product_id), str(price), name, '1', 'instrumento de teste', str(stock),
            status, '{}', '2026-01-01']


def call(name, arguments=None, call_id='qa'):
    return {'type': 'function_call', 'name': name,
            'arguments': json.dumps(arguments or {}, ensure_ascii=False), 'call_id': call_id}


class ScriptedProvider:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.last_tool_summaries = []

    def request(self, messages):
        self.calls.append(messages)
        output = self.outputs.pop(0) if self.outputs else []
        return {'output': output}


class P2CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self, changes=None, extra_products=(), extra_promotions=(), extra_orders=(), extra_items=()):
        write_dataset(self.root, changes)
        for table, rows in (('products', extra_products), ('promotions', extra_promotions),
                            ('orders', extra_orders), ('order_items', extra_items)):
            if not rows:
                continue
            with (self.root / f'{table}.csv').open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows(rows)
        return load_dataset(self.root)

    def test_b08_global_cheapest_uses_effective_catalog_price_and_marketability(self):
        snapshot = self.snapshot(
            {'products': {'price_brl': '300.00', 'name': 'Korg Synth 1 Pro'},
             'promotions': {'product_id': '1', 'discount_percent': '50', 'is_active': '1'}},
            extra_products=[
                product_row(119, '159.90', 'Shelby SU-21S Soprano Sunburst', 8),
                product_row(120, '1.00', 'Out of stock', 0),
                product_row(121, '2.00', 'Discontinued', 50, 'discontinued'),
                product_row(122, '3.00', 'Coming soon', 50, 'coming_soon'),
            ],
            extra_promotions=[['2', '119', '90', 'inactive promotion', '0']],
        )
        provider = ScriptedProvider([call('cheapest_product')])
        agent = ConversationAgent(snapshot, provider)

        answer = agent.answer('Qual o instrumento mais barato em estoque?', chat_id='B08')

        self.assertIn('Korg Synth 1 Pro', answer)
        self.assertIn('R$ 150,00', answer)
        self.assertNotIn('Shelby', answer)
        self.assertNotIn('Out of stock', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['cheapest_product'])

    def test_b08_real_baseline_selects_product_119_as_global_minimum(self):
        snapshot = load_original_baseline(self)
        cheapest = Catalog(snapshot).cheapest_available()
        self.assertEqual(cheapest['product_id'], 119)
        self.assertEqual(cheapest['name'], 'Shelby SU-21S Soprano Sunburst')
        self.assertEqual(format(cheapest['price_quote']['checkout_price_brl'], '.2f'), '159.90')
        self.assertEqual(cheapest['stock_quantity'], 8)
        provider = ScriptedProvider([call('cheapest_product')])
        answer = ConversationAgent(snapshot, provider).answer(
            'Qual o instrumento mais barato em estoque?', chat_id='B08-real')
        self.assertIn('Shelby SU-21S Soprano Sunburst', answer)
        self.assertIn('R$ 159,90', answer)

    def test_g05_order_three_keeps_historical_total_and_reports_separate_catalog_delta(self):
        snapshot = self.snapshot({
            'products': {'product_id': '130', 'price_brl': '1749.00', 'name': 'Produto do pedido 3',
                         'stock_quantity': '2', 'status': 'coming_soon'},
            'orders': {'order_id': '3', 'total_brl': '3450.00'},
            'order_items': {'order_id': '3', 'product_id': '130', 'quantity': '2'},
            'promotions': {'product_id': '130', 'is_active': '0'},
        })
        provider = ScriptedProvider([call('orders', {'full_name': 'Synthetic Customer'})])
        agent = ConversationAgent(snapshot, provider)
        question = ('Meu nome completo é Synthetic Customer. Por que os produtos do meu pedido 3 '
                    'hoje somam R$ 3.498,00?')

        answer = agent.answer(question, chat_id='G05')

        self.assertIn('Pedido 3', answer)
        self.assertIn('total registrado R$ 3.450,00', answer)
        self.assertIn('preços-base cadastrados hoje', answer)
        self.assertIn('R$ 3.498,00', answer)
        self.assertIn('diferença de R$ 48,00', answer)
        self.assertIn('hipótese', answer.lower())
        self.assertNotIn('a causa foi', answer.lower())
        self.assertEqual(agent.last_trace['executed_tools'], ['orders'])

    def test_g05_original_baseline_order_three_comparison_uses_authorized_name_and_is_sanitized(self):
        snapshot = load_original_baseline(self)
        order = next(row for row in snapshot.tables['orders'] if row['order_id'] == 3)
        customer = next(row for row in snapshot.tables['customers'] if row['customer_id'] == order['customer_id'])
        result = OrderService(snapshot).lookup(customer['name'], order_id=3, include_catalog_comparison=True)
        found = next(row for row in result if row['order_id'] == 3)
        self.assertEqual(str(found['total_brl']), '3450')
        self.assertEqual(str(found['catalog_comparison']['catalog_total_brl']), '3498')
        self.assertEqual(str(found['catalog_comparison']['difference_brl']), '48')
        provider = ScriptedProvider([call('orders', {'full_name': customer['name']})])
        question = (f"Meu nome completo é {customer['name']}. Por que os produtos do meu pedido 3 "
                    'hoje somam R$ 3.498,00?')
        answer = ConversationAgent(snapshot, provider).answer(question, chat_id='G05-real')
        self.assertIn('total registrado R$ 3.450,00', answer)
        self.assertIn('R$ 3.498,00', answer)
        self.assertIn('diferença de R$ 48,00', answer)
        self.assertIn('hipóteses, não fatos confirmados', answer)
        self.assertNotIn(customer['name'], answer)

    def test_c2_catalog_route_explains_divergence_and_quotes_only_catalog_price(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        provider = ScriptedProvider([call('search_products',
            {'text': 'Korg Synth 1 Pro', 'installments': 3})])
        answer = ConversationAgent(snapshot, provider).answer(
            'O Korg Synth 1 Pro foi anunciado por R$ 900. Confirme o preço correto do catálogo e busque opções em 3x.',
            chat_id='C2-catalog')

        self.assertIn('R$ 900,00', answer)
        self.assertIn('R$ 1.127,00', answer)
        self.assertIn('3x (R$ 375,67', answer)
        self.assertIn('usa somente o preço do catálogo', answer.lower())
        self.assertNotIn('3x R$ 300,00', answer)

    def test_c2_direct_quote_route_does_not_accept_conflicting_product_price(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        provider = ScriptedProvider([call('installment_quote',
            {'amount_brl': '900.00', 'installments': 3})])
        answer = ConversationAgent(snapshot, provider).answer(
            'O Korg Synth 1 Pro custa R$ 900,00. Simule em 3x com esse valor.', chat_id='C2-direct')

        self.assertIn('R$ 1.127,00', answer)
        self.assertIn('R$ 900,00', answer)
        self.assertIn('quer que eu simule com o preço efetivo do catálogo?', answer.lower())
        self.assertNotIn('3x R$ 300,00', answer)

    def test_lr02_brazilian_thousands_filter_is_normalized_before_catalog_search(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        provider = ScriptedProvider([call('search_products',
            {'max_price': '1.500', 'available_only': True})])
        agent = ConversationAgent(snapshot, provider)

        answer = agent.answer('Quais instrumentos disponíveis custam até R$ 1.500?', chat_id='LR02')

        self.assertIn('Korg Synth 1 Pro', answer)
        self.assertEqual(agent.last_trace['status'], 'success')
        self.assertEqual(agent.last_trace['tool_executions'][0]['result_count'], 1)
        self.assertEqual(agent.last_trace['tool_executions'][0]['max_price_brl'], '1500.00')

    def test_lr02_original_baseline_finds_shelby_with_brazilian_budget(self):
        snapshot = load_original_baseline(self)
        provider = ScriptedProvider([call('search_products',
            {'max_price': '1.500', 'available_only': True})])
        answer = ConversationAgent(snapshot, provider).answer(
            'Quais instrumentos disponíveis custam até R$ 1.500?', chat_id='LR02-real-data')
        self.assertIn('Shelby SU-21S Soprano Sunburst', answer)

    def test_codex_provider_records_safe_normalized_filter_diagnostics(self):
        from artefact.codex_local import CodexLocalProvider

        quota = Mock()
        runner = Mock(return_value=SimpleNamespace(
            returncode=0,
            stdout=json.dumps({'calls': [{
                'name': 'search_products', 'max_price': '1.500', 'available_only': True,
                'text': 'Korg Synth 1 Pro',
            }]}),
            stderr='',
        ))
        provider = CodexLocalProvider('codex', quota, runner=runner, model='gpt-6-luna')

        provider.request([{'role': 'user', 'content': 'Quais instrumentos até R$ 1.500?'}])

        self.assertEqual(provider.last_tool_summaries, [{
            'name': 'search_products', 'text_filter': True, 'category_filter': False,
            'available_only': True, 'max_price_brl': '1500.00', 'max_price_valid': True,
        }])
        self.assertNotIn('Korg Synth 1 Pro', str(provider.last_tool_summaries))

    def test_lr02_diagnostics_separate_invalid_filter_empty_result_and_execution_error(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        invalid = ConversationAgent(snapshot, ScriptedProvider([
            call('search_products', {'max_price': 'one thousand', 'available_only': True})
        ]))
        invalid.answer('Quais instrumentos disponíveis custam até R$ 1.500?', chat_id='invalid-filter')
        self.assertEqual(invalid.last_trace['tool_executions'][0]['reason'], 'invalid_price_filter')

        empty = ConversationAgent(snapshot, ScriptedProvider([
            call('search_products', {'max_price': '100.00', 'available_only': True})
        ]))
        empty.answer('Quais instrumentos disponíveis custam até R$ 100?', chat_id='empty-result')
        self.assertEqual(empty.last_trace['status'], 'empty')
        self.assertEqual(empty.last_trace['tool_executions'][0]['result_count'], 0)
        self.assertEqual(empty.last_trace['tool_executions'][0]['empty_search_diagnostics']['empty_filter_candidates'],
                         ['price_bounds'])

        incorrect = ConversationAgent(snapshot, ScriptedProvider([
            call('search_products', {'max_price': '100.00', 'available_only': True})
        ]))
        incorrect.answer('Quais instrumentos disponíveis custam até R$ 1.500?', chat_id='wrong-filter')
        self.assertEqual(incorrect.last_trace['rejected_tools'][0]['reason'], 'incorrect_price_filter')
        self.assertEqual(incorrect.last_trace['status'], 'clarification')

        wrong_category = ConversationAgent(snapshot, ScriptedProvider([
            call('search_products', {'category': 'Teclados'})
        ]))
        wrong_category.answer('Quais violões disponíveis há?', chat_id='wrong-category')
        self.assertEqual(wrong_category.last_trace['rejected_tools'][0]['reason'],
                         'catalog_filter_not_grounded')

        failed = ConversationAgent(snapshot, ScriptedProvider([
            call('search_products', {'max_price': '1.500', 'available_only': True})
        ]))
        with patch('artefact.agent.Catalog.search', side_effect=OSError('sensitive execution detail')):
            failed.answer('Quais instrumentos disponíveis custam até R$ 1.500?', chat_id='tool-error')
        self.assertEqual(failed.last_trace['status'], 'tool_error')
        self.assertEqual(failed.last_trace['tool_executions'][0]['reason'], 'tool_failed')
        self.assertNotIn('sensitive execution detail', str(failed.last_trace))

    def test_unknown_tool_plan_is_reported_without_exposing_its_name(self):
        snapshot = self.snapshot()
        agent = ConversationAgent(snapshot, ScriptedProvider([call('sensitive-unknown-tool')]))
        agent.answer('Consulta de catálogo.', chat_id='invalid-plan')
        self.assertEqual(agent.last_trace['status'], 'error')
        self.assertEqual(agent.last_trace['error_code'], 'invalid_tool_plan')
        self.assertEqual(agent.last_trace['selected_tools'], ['unknown'])
        self.assertNotIn('sensitive-unknown-tool', str(agent.last_trace))

    def test_lr07_product_context_repairs_incomplete_tool_query_without_crossing_sessions(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        provider = ScriptedProvider(
            [call('search_products', {'text': 'Korg Synth 1 Pro'})],
            [call('search_products', {'text': 'Korg Synth', 'installments': 3})],
            [call('installment_quote', {'amount_brl': '900.00', 'installments': 3})],
        )
        agent = ConversationAgent(snapshot, provider)
        agent.answer('Qual o preço e a disponibilidade do Korg Synth 1 Pro?', chat_id='valid')
        followup = agent.answer('E em 3x?', chat_id='valid')
        followup_trace = dict(agent.last_trace)
        independent = agent.answer('E em 3x?', chat_id='other')

        self.assertIn('R$ 375,67', followup)
        self.assertEqual(followup_trace['tool_executions'][0]['name'], 'search_products')
        self.assertEqual(followup_trace['tool_executions'][0]['status'], 'executed')
        self.assertEqual(len(provider.calls), 3)
        self.assertNotIn('Korg Synth 1 Pro', independent)
        self.assertNotIn('3x R$ 300,00', independent)

    def test_lr07_context_does_not_survive_refusal_or_ambiguous_comparison_but_can_be_restored(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}},
            extra_products=[product_row(2, '800.00', 'Yamaha Bass 3X', 3)])
        provider = ScriptedProvider(
            [call('search_products', {'text': 'Korg Synth 1 Pro'})],
            [],
            [call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})],
            [call('search_products', {'text': 'Korg Synth 1 Pro'})],
            [call('search_products', {'text': 'Korg Synth', 'installments': 3})],
            [call('search_products', {'text': 'Korg Synth 1 Pro'})],
            [call('search_products', {'text': 'Korg Synth', 'installments': 3})],
        )
        agent = ConversationAgent(snapshot, provider)
        agent.answer('Quero o Korg Synth 1 Pro.', chat_id='revoked')
        agent.answer('Não quero mais o Korg Synth 1 Pro.', chat_id='revoked')
        refused_followup = agent.answer('E em 3x?', chat_id='revoked')
        agent.answer('Quero o Korg Synth 1 Pro.', chat_id='ambiguous')
        agent.answer('Compare Korg Synth 1 Pro e Yamaha Bass 3X.', chat_id='ambiguous')
        ambiguous_followup = agent.answer('E em 3x?', chat_id='ambiguous')
        agent.answer('Agora quero novamente o Korg Synth 1 Pro.', chat_id='revoked')
        restored_followup = agent.answer('E em 3x?', chat_id='revoked')

        self.assertNotIn('3x R$', refused_followup)
        self.assertNotIn('3x R$', ambiguous_followup)
        self.assertIn('R$ 375,67', restored_followup)

    def test_pix_policy_followup_uses_only_same_session_antecedent(self):
        snapshot = self.snapshot()
        provider = ScriptedProvider(
            [call('policy', {'topic': 'pix'})],
            [call('policy', {'topic': 'installments'})],
            [call('policy', {'topic': 'pix'})],
        )
        agent = ConversationAgent(snapshot, provider)
        agent.answer('O desconto Pix acumula com promoções?', chat_id='pix')
        same_session = agent.answer('Posso usar os dois descontos?', chat_id='pix')
        other_session = agent.answer('Posso usar os dois descontos?', chat_id='other')

        self.assertIn('5%', same_session)
        self.assertIn('não acumula', same_session.lower())
        self.assertNotIn('5%', other_session)

    def test_lr08_executes_grounded_product_and_shipping_tools_together(self):
        snapshot = self.snapshot({'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
                                 'promotions': {'is_active': '0'}})
        provider = ScriptedProvider([
            call('search_products', {'max_price': '1.500', 'available_only': True}, 'catalog'),
            call('policy', {'topic': 'shipping'}, 'shipping'),
        ])
        agent = ConversationAgent(snapshot, provider)

        answer = agent.answer('Quais instrumentos disponíveis custam até R$ 1.500? '
                              'E quais são as regras de frete?', chat_id='LR08-valid')

        self.assertIn('Korg Synth 1 Pro', answer)
        self.assertIn('Frete metropolitano', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['search_products', 'policy'])

    def test_lr08_returns_valid_partial_result_and_names_rejected_operation(self):
        snapshot = self.snapshot()
        provider = ScriptedProvider([
            call('search_products', {'category': 'Teclados'}, 'bad-catalog'),
            call('policy', {'topic': 'shipping'}, 'shipping'),
        ])
        agent = ConversationAgent(snapshot, provider)

        answer = agent.answer('Quais violões disponíveis há? E quais são as regras de frete?',
                              chat_id='LR08-partial')

        self.assertIn('Frete metropolitano', answer)
        self.assertIn('não realizei a busca de produtos', answer.lower())
        self.assertEqual(agent.last_trace['executed_tools'], ['policy'])
        self.assertEqual(agent.last_trace['rejected_tools'][0]['reason'], 'catalog_filter_not_grounded')


if __name__ == '__main__':
    unittest.main()
