"""G1: local monetary roles and mandatory product ceiling, both executors."""
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import test_p2_corrections as fixtures
from artefact.agent import Agent
from artefact.context import resolve_commercial_basis
from artefact.conversation import ConversationAgent
from artefact.rendering import render_results
from artefact.services import Catalog


class G1MoneyScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'name': 'Korg Synth', 'price_brl': '1127.00'},
            'promotions': {'is_active': '0'}}, extra_products=[
                fixtures.product_row(2, '1500', 'Yamaha PSR'),
                fixtures.product_row(3, '400', 'Roland Go')])

    def execute(self, text, conversational, shipping=False, tool='search_products', bound=None):
        plans = [(tool, {'category': 'Teclados e Pianos'})]
        if bound is not None:
            plans[0][1]['max_price'] = bound
        if 'disponíveis' in text and tool == 'search_products':
            plans[0][1]['available_only'] = True
        if shipping:
            plans.append(('policy', {'topic': 'shipping'}))
        agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider([
            fixtures.call(n, a, str(i)) for i, (n, a) in enumerate(plans)]))
        captured = []
        search = Catalog.search
        def record(catalog, *args, **kwargs):
            result = search(catalog, *args, **kwargs)
            captured.append((kwargs, result))
            return result
        with patch.object(Catalog, 'search', record):
            if conversational:
                answer = agent.answer(text, chat_id='g1')
                basis = agent.context['g1'].commercial
                roles = [(v['amount_brl'], v['role']) for v in agent.last_trace['commercial_basis']['values']]
                self.assertEqual(roles, [(str(v.amount), v.role) for v in basis.values])
            else:
                planner = Mock()
                planner.plan.return_value = plans
                result = Agent(self.snapshot, planner).answer(text, structured=True)
                answer = render_results(result) if isinstance(result, list) else result
                basis = resolve_commercial_basis(text, self.snapshot)
        return answer, basis, captured

    def assert_ceiling(self, text):
        for route in (False, True):
            with self.subTest(text=text, route=route):
                answer, basis, calls = self.execute(text, route)
                self.assertEqual(basis.scope.max_price, Decimal('500'))
                self.assertEqual([(v.amount, v.role) for v in basis.values],
                                 [(Decimal('500'), 'budget'), (Decimal('35'), 'shipping_amount')]
                                 if text.index('500') < text.index('35') else
                                 [(Decimal('35'), 'shipping_amount'), (Decimal('500'), 'budget')])
                self.assertEqual(len(calls), 1)
                args, rows = calls[0]
                self.assertEqual(args['max_price'], Decimal('500'))
                self.assertEqual([row['product_id'] for row in rows], [3])
                self.assertIn('Roland Go', answer)
                self.assertNotIn('Korg Synth:', answer)
                self.assertNotIn('Yamaha PSR:', answer)

    def test_a_original_same_clause_has_distinct_roles(self):
        self.assert_ceiling('Tenho orçamento de R$ 500, frete de R$ 35 e quero opções de teclados.')

    def test_b_reversed_order_and_separate_sentences(self):
        self.assert_ceiling('O frete é R$ 35. Meu orçamento para o teclado é R$ 500.')

    def test_variants_with_conjunctions_and_local_qualifiers(self):
        for text in ('Meu orçamento é R$ 500 e o frete é R$ 35; quero opções de teclados.',
                     'Frete de R$ 35, orçamento de R$ 500 para teclados.',
                     'Tenho R$ 500 disponíveis para comprar teclados, frete de R$ 35.'):
            self.assert_ceiling(text)

    def test_c_independent_policy_preserved_with_ceiling(self):
        for route in (False, True):
            answer, basis, calls = self.execute('Quero teclados de até R$ 500. Quais são as regras de frete?', route, shipping=True, bound='500')
            self.assertEqual(basis.scope.max_price, Decimal('500'))
            self.assertEqual(len(calls), 1)
            self.assertEqual([r['product_id'] for r in calls[0][1]], [3])
            self.assertIn('Roland Go', answer)
            self.assertIn('Frete metropolitano', answer)

    def test_d_shipping_value_is_not_a_product_ceiling(self):
        for route in (False, True):
            answer, basis, calls = self.execute('O frete custa R$ 35. Quais teclados estão disponíveis?', route)
            self.assertIsNone(basis.scope.max_price)
            self.assertEqual([(v.amount, v.role) for v in basis.values], [(Decimal('35'), 'shipping_amount')])
            self.assertEqual({r['product_id'] for r in calls[0][1]}, {1, 2, 3})
            self.assertIn('Korg Synth', answer)

    def test_e_total_including_shipping_requires_product_allocation(self):
        for route in (False, True):
            answer, basis, calls = self.execute('Meu limite total, incluindo frete, é R$ 500. Mostre teclados.', route)
            self.assertEqual(calls, [])
            self.assertEqual(basis.restriction, 'budget_includes_shipping')
            self.assertIn('frete', answer.lower())
            self.assertIn('produtos', answer.lower())
            self.assertNotIn('Roland Go:', answer)

    def test_f_unresolved_budget_never_executes_without_ceiling(self):
        for text in ('Tenho orçamento de R$ 500 ou R$ 700, frete de R$ 35. Mostre teclados.',
                     'Tenho um orçamento definido. O frete é R$ 35. Mostre teclados.'):
            for route in (False, True):
                with self.subTest(text=text, route=route):
                    answer, basis, calls = self.execute(text, route, shipping=True)
                    self.assertEqual(calls, [])
                    self.assertEqual(basis.restriction, 'budget_unresolved')
                    self.assertIsNone(basis.scope.max_price)
                    self.assertIn('orçamento', answer.lower())
                    self.assertIn('Frete metropolitano', answer)
                    self.assertNotIn('Korg Synth:', answer)

    def test_scope_applies_to_recommendation_without_optional_bound(self):
        for route in (False, True):
            answer, basis, calls = self.execute('Tenho orçamento de R$ 500, frete de R$ 35 e quero opções de teclados.', route, tool='recommend_products')
            self.assertEqual(basis.scope.max_price, Decimal('500'))
            self.assertEqual([r['product_id'] for r in calls[0][1]], [3])
            self.assertNotIn('Korg Synth:', answer)


if __name__ == '__main__':
    unittest.main()
