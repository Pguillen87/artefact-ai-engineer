"""Offline regressions for the two failures found by the Gemini campaign."""
import csv
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import test_p2_corrections as fixtures
from artefact.agent import Agent
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.rendering import render_results
from artefact.services import Catalog
from test_data import write_dataset


class GeminiRecommendationAndEmptySearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_dataset(self.root, {'products': {'name': 'Korg Synth', 'price_brl': '1127.00'}})
        with (self.root / 'products.csv').open('a', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(fixtures.product_row(2, '1500', 'Yamaha PSR'))
            writer.writerow(fixtures.product_row(3, '400', 'Roland Go'))
        self.snapshot = load_dataset(self.root)

    def _ask(self, text, tool, arguments, conversational, snapshot=None):
        snapshot = snapshot or self.snapshot
        if conversational:
            agent = ConversationAgent(snapshot, fixtures.ScriptedProvider(
                [fixtures.call(tool, arguments, 'focused')]))
            answer = agent.answer(text, chat_id='focused')
            return answer, agent
        planner = Mock()
        planner.plan.return_value = [(tool, arguments)]
        agent = Agent(snapshot, planner)
        evidence = agent.answer(text, structured=True)
        agent.test_evidence = evidence
        answer = render_results(evidence) if isinstance(evidence, list) else evidence
        return answer, agent

    def _snapshot(self, products, categories=()):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        first = products[0]
        write_dataset(Path(root.name), {
            'products': {'name': first[2], 'price_brl': first[1]},
            'promotions': {'is_active': '0'}})
        with (Path(root.name) / 'products.csv').open('a', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            for product_id, price, name, category_id in products[1:]:
                row = fixtures.product_row(product_id, price, name)
                row[3] = str(category_id)
                writer.writerow(row)
        if categories:
            with (Path(root.name) / 'categories.csv').open('a', encoding='utf-8', newline='') as stream:
                csv.writer(stream).writerows(categories)
        return load_dataset(root.name)

    def _ask_with_search_calls(self, *args, **kwargs):
        calls = []
        original = Catalog.search

        def record(catalog, *search_args, **search_kwargs):
            calls.append(dict(search_kwargs))
            return original(catalog, *search_args, **search_kwargs)

        with patch.object(Catalog, 'search', record):
            answer, agent = self._ask(*args, **kwargs)
        return answer, agent, calls

    def test_r1_refused_product_can_ground_recommendation_in_its_category(self):
        text = 'Não quero mais esse Korg Synth. Me recomende outras opções.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                if conversational:
                    provider = fixtures.ScriptedProvider(
                        [fixtures.call('search_products', {'text': 'Korg Synth'}, 'price')],
                        [fixtures.call('recommend_products', {'text': 'Korg Synth'}, 'alternatives')])
                    agent = ConversationAgent(self.snapshot, provider)
                    calls = []
                    original = Catalog.search
                    def record(catalog, *search_args, **search_kwargs):
                        calls.append(dict(search_kwargs))
                        return original(catalog, *search_args, **search_kwargs)
                    with patch.object(Catalog, 'search', record):
                        agent.answer('Qual o preço do Korg Synth?', chat_id='r1')
                        agent.answer('E em três vezes?', chat_id='r1')
                        answer = agent.answer(text, chat_id='r1')
                    self.assertEqual(agent.last_trace['executed_tools'], ['recommend_products'])
                    self.assertEqual(calls[-1]['category'], 'Teclados e Pianos')
                    self.assertIn(1, calls[-1]['excluded_ids'])
                else:
                    answer, _, calls = self._ask_with_search_calls(
                        text, 'recommend_products', {'text': 'Korg Synth'}, False)
                if conversational:
                    calls = None
                self.assertIn('yamaha psr', answer.casefold())
                self.assertIn('roland go', answer.casefold())
                self.assertNotIn('Korg Synth:', answer)
                if calls is not None:
                    self.assertEqual(calls[-1]['category'], 'Teclados e Pianos')
                    self.assertIn(1, calls[-1]['excluded_ids'])

    def test_e1_empty_budget_search_names_the_applied_category_and_limit(self):
        text = 'Tenho orçamento de R$ 500 para teclados. Mostre opções.'
        empty_snapshot = self._snapshot([
            (1, '1127.00', 'Korg Synth', 1),
            (2, '1500.00', 'Yamaha PSR', 1),
        ])
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, agent = self._ask(text, 'search_products', {'category': 'Teclados e Pianos'},
                                          conversational, snapshot=empty_snapshot)
                self.assertIn('500', answer)
                self.assertIn('teclado', answer.casefold())
                self.assertIn('não encontrei', answer.casefold())
                self.assertNotIn('informações suficientes', answer.casefold())
                if conversational:
                    self.assertEqual(agent.last_trace['tool_executions'][0]['result_count'], 0)
                    self.assertEqual(agent.last_trace['status'], 'empty')
                else:
                    self.assertEqual(agent.test_evidence[0]['resultado'], [])
                    self.assertEqual(agent.test_evidence[0]['search_context']['max_price_brl'], '500.00')

    def test_r2_refusal_with_no_remaining_alternatives_is_explained(self):
        snapshot = self._snapshot([(1, '1127.00', 'Korg Synth', 1)])
        text = 'Não quero mais esse Korg Synth. Me recomende outras opções.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, agent = self._ask(text, 'recommend_products', {'text': 'Korg Synth'}, conversational,
                                          snapshot=snapshot)
                self.assertIn('não encontrei', answer.casefold())
                self.assertIn('alternativas', answer.casefold())
                self.assertNotIn('Korg Synth:', answer)
                if conversational:
                    self.assertEqual(agent.last_trace['executed_tools'], ['recommend_products'])
                    self.assertEqual(agent.last_trace['tool_executions'][0]['result_count'], 0)

    def test_r3_recommendations_after_refusal_keep_the_existing_budget(self):
        text = 'Tenho orçamento de R$ 500 para teclados. Não quero o Korg. Recomende outras opções.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, agent, calls = self._ask_with_search_calls(
                    text, 'recommend_products', {}, conversational)
                self.assertIn('Roland Go', answer)
                self.assertNotIn('Korg Synth:', answer)
                self.assertNotIn('Yamaha PSR:', answer)
                if conversational:
                    self.assertEqual(agent.last_trace['executed_tools'], ['recommend_products'])
                self.assertEqual(calls[-1]['max_price'], Decimal('500'))
                self.assertIn(1, calls[-1]['excluded_ids'])

    def test_r4_affirmative_new_category_replaces_a_negated_category(self):
        snapshot = self._snapshot([
            (1, '1127.00', 'Korg Synth', 1),
            (2, '900.00', 'Violao Nylon', 2),
        ], categories=[['2', 'Violões', 'instrumentos de cordas']])
        text = 'Não quero mais teclados. Me mostre violões.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, _ = self._ask(text, 'recommend_products', {}, conversational, snapshot=snapshot)
                self.assertIn('Violao Nylon', answer)
                self.assertNotIn('Korg Synth:', answer)

    def test_r5_recommendation_without_product_or_category_asks_for_scope(self):
        text = 'Me recomende outras opções.'
        with patch.object(Catalog, 'search', wraps=Catalog.search) as search:
            for conversational in (False, True):
                with self.subTest(conversational=conversational):
                    answer, agent = self._ask(text, 'recommend_products', {}, conversational)
                    self.assertIn('qual tipo ou categoria', answer.casefold())
                    self.assertIn('não executei', answer.casefold())
                    if conversational:
                        self.assertEqual(agent.last_trace['executed_tools'], [])
        search.assert_not_called()

    def test_r6_explicit_reselection_restores_a_previously_refused_product(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('recommend_products', {'text': 'Korg Synth'}, 'refuse')],
            [fixtures.call('search_products', {'text': 'Korg Synth'}, 'reselect')])
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Não quero mais esse Korg Synth. Me recomende outras opções.', chat_id='r6')
        answer = agent.answer('Na verdade, quero novamente o Korg Synth.', chat_id='r6')
        self.assertIn('Korg Synth:', answer)
        self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])
        self.assertNotIn(1, agent.context['r6'].commercial.scope.excluded_ids)

    def test_e2_budgeted_search_presents_an_eligible_result(self):
        answer, _ = self._ask('Tenho orçamento de R$ 500 para teclados. Mostre opções.', 'search_products',
                              {'category': 'Teclados e Pianos'}, False)
        self.assertIn('Roland Go', answer)

    def test_e3_budgeted_search_never_presents_above_ceiling(self):
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, _ = self._ask('Tenho orçamento de R$ 500 para teclados. Mostre opções.', 'search_products',
                                      {'category': 'Teclados e Pianos'}, conversational)
                self.assertIn('Roland Go', answer)
                self.assertNotIn('Korg Synth:', answer)
                self.assertNotIn('Yamaha PSR:', answer)

    def test_e4_search_failure_is_not_reported_as_an_empty_catalog(self):
        for conversational in (False, True):
            with self.subTest(conversational=conversational), patch.object(Catalog, 'search', side_effect=OSError('offline')):
                answer, agent = self._ask('Quero opções de teclados.', 'search_products',
                                          {'category': 'Teclados e Pianos'}, conversational)
                self.assertIn('não consegui consultar', answer.casefold())
                self.assertNotIn('não encontrei', answer.casefold())
                if conversational:
                    self.assertEqual(agent.last_trace['status'], 'tool_error')

    def test_e5_unresolved_budget_does_not_execute_a_search(self):
        text = 'Tenho um orçamento para teclados. Mostre opções.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, agent = self._ask(text, 'search_products',
                                          {'category': 'Teclados e Pianos'}, conversational)
                self.assertIn('qual é o valor do orçamento', answer.casefold())
                self.assertIn('não executei', answer.casefold())
                if conversational:
                    self.assertEqual(agent.last_trace['executed_tools'], [])

    def test_e6_explicit_request_above_budget_keeps_the_approved_override(self):
        text = 'Tenho orçamento de R$ 500 para teclados. Mostre opções acima de R$ 1.000.'
        for conversational in (False, True):
            with self.subTest(conversational=conversational):
                answer, _ = self._ask(text, 'search_products', {
                    'category': 'Teclados e Pianos', 'min_price': '1000'}, conversational)
                self.assertIn('Yamaha PSR', answer)
                self.assertIn('Korg Synth', answer)
                self.assertNotIn('Roland Go', answer)


if __name__ == '__main__':
    unittest.main()
