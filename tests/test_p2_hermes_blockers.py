import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import test_p2_corrections as fixtures
from artefact.agent import Agent
from artefact.conversation import ConversationAgent, _confirms_catalog_price
from baseline_support import load_original_baseline


class HermesCommercialBlockerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
            'promotions': {'is_active': '0'},
        })

    def ask(self, message, plans):
        agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider(plans))
        answer = agent.answer(message, chat_id='hermes-regression')
        return answer, agent

    def alternative_plans(self):
        return [
            fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3}),
            fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3}),
        ]

    def test_divergent_amount_for_product_clarifies_for_both_plans(self):
        for text in ('Simule R$ 900 em 3x para o Korg Synth 1 Pro.',
                     'Quero o Korg Synth 1 Pro por R$ 900 em 3x.'):
            for plan in self.alternative_plans():
                with self.subTest(message=text, tool=plan['name']):
                    answer, agent = self.ask(text, [plan])
                    self.assertIn('diverge', answer.lower())
                    self.assertIn('R$ 900,00', answer)
                    self.assertIn('R$ 1.127,00', answer)
                    self.assertNotIn('parcelamento permitido', answer.lower())
                    self.assertNotIn('3x R$ 300,00', answer)
                    self.assertNotIn('3x (R$ 375,67', answer)
                    self.assertEqual(agent.last_trace['commercial_basis']['kind'], 'ambiguous')
                    self.assertEqual(agent.last_trace['commercial_basis']['amount_brl'], '900.00')
                    self.assertEqual(agent.last_trace['commercial_basis']['catalog_amount_brl'], '1127.00')
                    self.assertNotIn(plan['name'], agent.last_trace['executed_tools'])
                    self.assertEqual(agent.last_trace['rejected_tools'][0]['reason'], 'conflicting_price_basis')

    def test_negated_confirmation_never_authorizes_catalog_quote(self):
        text = ('Quero o Korg Synth 1 Pro. Não confirme o preço do catálogo '
                'e simule R$ 900 em 3x.')
        for plan in self.alternative_plans():
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(text, [plan])
                self.assertIn('diverge', answer.lower())
                self.assertNotIn('simulação desta resposta usa somente', answer.lower())
                self.assertNotIn('parcelamento permitido', answer.lower())

    def test_confirmation_requires_positive_unconditional_authority(self):
        for text in ('Não confirme o preço do catálogo.',
                     'Se confirmar o preço do catálogo, simule.',
                     'Talvez confirme o preço do catálogo.',
                     'Confirme que o preço do catálogo está incorreto.',
                     'O preço oficial do catálogo está correto?'):
            with self.subTest(text=text):
                self.assertFalse(_confirms_catalog_price(text))
        for text in ('Confirme o preço correto do catálogo e simule em 3x.',
                     'Use o preço do catálogo e simule em 3x sem juros.'):
            with self.subTest(text=text):
                self.assertTrue(_confirms_catalog_price(text))

    def test_mismatch_preserves_independent_shipping_operation(self):
        plans = [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3}, 'p'),
                 fixtures.call('policy', {'topic': 'shipping'}, 's')]
        answer, agent = self.ask('O Korg Synth 1 Pro custa R$ 900 em 3x? E quais são as regras de frete?', plans)
        self.assertIn('diverge', answer.lower())
        self.assertIn('Frete metropolitano', answer)
        self.assertIn('policy', agent.last_trace['executed_tools'])
        self.assertNotIn('parcelamento permitido', answer.lower())

    def test_explicit_hypothetical_amount_is_labeled_and_plan_independent(self):
        text = ('Faça uma simulação puramente hipotética de R$ 900 em 3x '
                'para o Korg Synth 1 Pro, independente do preço do catálogo.')
        for plan in self.alternative_plans():
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(text, [plan])
                self.assertIn('hipot', answer.lower())
                self.assertIn('3x R$ 300,00', answer)
                self.assertNotIn('1.127', answer)
                self.assertEqual(agent.last_trace['commercial_basis']['kind'], 'hypothetical')
                self.assertEqual(agent.last_trace['executed_tools'], ['installment_quote'])
                self.assertEqual(agent.last_trace['tool_executions'][0]['amount_brl'], '900.00')
                self.assertEqual(agent.last_trace['tool_executions'][0]['installments'], 3)

    def test_hypothetical_word_in_unrelated_sentence_does_not_authorize_quote(self):
        text = 'O frete hipotético é incerto. Simule R$ 900 em 3x para o Korg Synth 1 Pro.'
        for plan in self.alternative_plans():
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(text, [plan])
                self.assertNotIn('3x R$ 300,00', answer)
                self.assertNotEqual(agent.last_trace['commercial_basis']['kind'], 'hypothetical')

    def test_hypothetical_total_without_product_accepts_explicit_wording(self):
        plan = fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})
        for text in ('Faça uma simulação puramente hipotética de R$ 900 em 3x.',
                     'Simule hipoteticamente R$ 900 em 3x.',
                     'Simule R$ 900 em 3x de forma hipotética.',
                     'Simule R$ 900.00 em 3x de forma hipotética.'):
            with self.subTest(text=text):
                answer, agent = self.ask(text, [plan])
                self.assertIn('hipot', answer.lower())
                self.assertIn('3x R$ 300,00', answer)
                self.assertEqual(agent.last_trace['commercial_basis']['kind'], 'hypothetical')

    def test_ambiguous_product_amount_does_not_become_a_known_total(self):
        plan = fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})
        answer, agent = self.ask('Quero o Korg Synth 1 Pro por R$ 900 em 3x.', [plan])
        self.assertIn('diverge', answer.lower())
        self.assertEqual(agent.last_trace['commercial_basis']['amount_role'], 'ambiguous')

    def test_direct_executor_rejects_product_amount_conflict_for_search_plan(self):
        planner = type('Planner', (), {'plan': lambda self, text: [
            ('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})]})()
        result = Agent(self.snapshot, planner).answer('Quero o Korg Synth 1 Pro por R$ 900 em 3x.', structured=True)
        self.assertIsInstance(result, str)
        self.assertNotIn('parcelamento permitido', result.lower())

    def test_direct_product_search_without_installments_cannot_hide_conflict(self):
        planner = type('Planner', (), {'plan': lambda self, text: [
            ('search_products', {'text': 'Korg Synth 1 Pro'})]})()
        result = Agent(self.snapshot, planner).answer('Quero o Korg Synth 1 Pro por R$ 900 em 3x.', structured=True)
        self.assertIsInstance(result, str)
        self.assertIn('esclareça', result.lower())

    def test_original_catalog_divergence_is_plan_independent(self):
        self.snapshot = load_original_baseline(self)
        name = 'Teclado Sintetizador Korg Synth 1 Pro'
        plans = [fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3}),
                 fixtures.call('search_products', {'text': name, 'installments': 3})]
        for plan in plans:
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(f'Simule R$ 900 em 3x para o {name}.', [plan])
                self.assertIn('R$ 900,00', answer)
                self.assertIn('R$ 1.127,00', answer)
                self.assertIn('diverge', answer.lower())
                self.assertNotIn('parcelamento permitido', answer.lower())
                self.assertEqual(agent.last_trace['commercial_basis']['catalog_amount_brl'], '1127.00')

    def test_catalog_confirmation_prices_exactly_to_catalog(self):
        plan = fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})
        answer, agent = self.ask('Confirme o preço do catálogo do Korg Synth 1 Pro e simule em 3x.', [plan])
        self.assertIn('R$ 1.127,00', answer)
        self.assertIn('R$ 375,67 + R$ 375,67 + R$ 375,66', answer)
        self.assertEqual(agent.last_trace['commercial_basis']['kind'], 'catalog')
        self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])

    def test_negated_hypothetical_does_not_establish_hypothetical_basis(self):
        text = 'Não é uma simulação hipotética: simule R$ 900 em 3x para o Korg Synth 1 Pro.'
        for plan in self.alternative_plans():
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(text, [plan])
                self.assertNotIn('3x R$ 300,00', answer)
                self.assertNotEqual(agent.last_trace['commercial_basis']['kind'], 'hypothetical')

    def test_direct_catalog_route_ignores_wrong_model_arguments(self):
        text = ('O Korg Synth 1 Pro custa R$ 900. Confirme o preço do catálogo '
                'e simule em 3x.')
        for plan in (fixtures.call('search_products', {'text': 'Produto inexistente', 'installments': 3}),
                     fixtures.call('installment_quote', {'amount_brl': '999.00', 'installments': 3})):
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(text, [plan])
                self.assertIn('diverge', answer.lower())
                self.assertIn('1.127,00', answer)
                self.assertIn('375,67', answer)
                self.assertNotIn('3x R$ 300,00', answer)
                self.assertNotIn('3x R$ 333,00', answer)
                self.assertEqual(agent.last_trace['executed_tools'], ['search_products'])
                self.assertTrue(any(item['reason'] == 'identified_product_installment_calculation'
                                    for item in agent.last_trace['plan_overrides']))

    def test_price_conflict_without_shipping_tool_discloses_omission(self):
        plan = fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})
        answer, agent = self.ask('O Korg Synth 1 Pro custa R$ 900 em 3x? E quais são as regras de frete?', [plan])
        self.assertIn('diverge', answer.lower())
        self.assertIn('não consegui', answer.lower())
        self.assertIn('frete', answer.lower())
        self.assertNotIn('policy', agent.last_trace['executed_tools'])

    def test_hypothetical_followup_does_not_switch_to_catalog(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})],
            [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})],
        )
        agent = ConversationAgent(self.snapshot, provider)
        first = agent.answer('Quero o Korg Synth 1 Pro. Faça uma simulação puramente hipotética de R$ 900 em 3x.', chat_id='hyp')
        following = agent.answer('E em 3x?', chat_id='hyp')
        self.assertIn('hipot', first.lower())
        self.assertIn('hipot', following.lower())
        self.assertIn('3x R$ 300,00', following)
        self.assertNotIn('1.127', following)

    def test_hypothetical_per_installment_followup_keeps_total(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('installment_quote', {'amount_brl': '300.00', 'installments': 3})],
            [fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})],
        )
        agent = ConversationAgent(self.snapshot, provider)
        first = agent.answer('Faça uma simulação hipotética de 3x de R$ 300.', chat_id='per-part')
        following = agent.answer('E em 3x?', chat_id='per-part')
        self.assertIn('3x R$ 300,00', first)
        self.assertIn('3x R$ 300,00', following)
        self.assertIn('hipot', following.lower())

    def test_conflicting_product_followup_never_silently_quotes_catalog(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})],
            [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})],
        )
        agent = ConversationAgent(self.snapshot, provider)
        first = agent.answer('Quero o Korg Synth 1 Pro por R$ 900 em 3x.', chat_id='conflict')
        following = agent.answer('E em 3x?', chat_id='conflict')
        self.assertIn('diverge', first.lower())
        self.assertNotIn('parcelamento permitido', following.lower())
        self.assertNotIn('3x R$ 300,00', following)

    def test_budget_and_explicit_total_have_distinct_bases(self):
        plan = fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})
        budget, budget_agent = self.ask('Tenho até R$ 900 para gastar em 3x.', [plan])
        total, total_agent = self.ask('Minha compra ficou em R$ 900. Simule em 3x.', [plan])
        self.assertNotIn('3x R$ 300,00', budget)
        self.assertEqual(budget_agent.last_trace['commercial_basis']['kind'], 'budget')
        self.assertIn('3x R$ 300,00', total)
        self.assertEqual(total_agent.last_trace['commercial_basis']['kind'], 'total')

    def test_separate_shipping_plan_is_validated_even_during_price_conflict(self):
        plans = [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3}, 'p'),
                 fixtures.call('policy', {'topic': 'warranty'}, 'wrong')]
        answer, agent = self.ask('O Korg Synth 1 Pro custa R$ 900 em 3x? Quais são as regras de frete?', plans)
        self.assertIn('diverge', answer.lower())
        self.assertIn('não consegui', answer.lower())
        self.assertNotIn('policy', agent.last_trace['executed_tools'])
        self.assertTrue(agent.last_trace['rejected_tools'])

    def test_budget_and_divergent_product_price_do_not_authorize_catalog_quote(self):
        message = 'Quero o Korg Synth 1 Pro por R$ 900 em 3x e tenho orçamento de até R$ 1.500.'
        plans = [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro',
                     'max_price': '1500.00', 'installments': 3}),
                 fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})]
        for plan in plans:
            with self.subTest(tool=plan['name']):
                answer, agent = self.ask(message, [plan])
                self.assertNotIn('parcelamento permitido', answer.lower())
                self.assertNotIn('3x R$ 300,00', answer)
                self.assertEqual(agent.last_trace['commercial_basis']['kind'], 'ambiguous')

    def test_independent_recommendation_is_not_discarded_by_price_conflict(self):
        plans = [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3}, 'p'),
                 fixtures.call('recommend_products', {}, 'alternatives')]
        answer, agent = self.ask('O Korg Synth 1 Pro custa R$ 900 em 3x? Recomende alternativas.', plans)
        self.assertIn('diverge', answer.lower())
        self.assertIn('Alternativas disponíveis', answer)
        self.assertIn('recommend_products', agent.last_trace['executed_tools'])
        self.assertNotIn('search_products', agent.last_trace['executed_tools'])

    def test_independent_promotion_is_not_discarded_by_price_conflict(self):
        plans = [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3}, 'p'),
                 fixtures.call('promotion', {'product_id': 1}, 'promotion')]
        answer, agent = self.ask('O Korg Synth 1 Pro custa R$ 900 em 3x? Ele tem promoção?', plans)
        self.assertIn('diverge', answer.lower())
        self.assertIn('promotion', agent.last_trace['executed_tools'])
        self.assertNotIn('search_products', agent.last_trace['executed_tools'])

    def test_per_installment_value_is_not_a_total_or_catalog_price(self):
        plan = fixtures.call('installment_quote', {'amount_brl': '300.00', 'installments': 3})
        answer, agent = self.ask('Simule 3x de R$ 300 para o Korg Synth 1 Pro.', [plan])
        self.assertNotIn('parcelamento permitido', answer.lower())
        self.assertNotIn('1.127,00: parcelamento', answer)
        self.assertEqual(agent.last_trace['commercial_basis']['amount_role'], 'per_installment')

    def test_invented_search_for_hypothetical_total_is_rejected(self):
        plan = fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})
        answer, agent = self.ask('Faça uma simulação puramente hipotética de R$ 900 em 3x.', [plan])
        self.assertNotIn('Korg Synth 1 Pro:', answer)
        self.assertNotIn('search_products', agent.last_trace['executed_tools'])

    def test_rejected_product_does_not_reappear_after_conflict(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro'})],
            [fixtures.call('search_products', {'text': 'Korg Synth 1 Pro', 'installments': 3})],
        )
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Não quero o Korg Synth 1 Pro.', chat_id='refused')
        following = agent.answer('E em 3x?', chat_id='refused')
        self.assertNotIn('parcelamento permitido', following.lower())


if __name__ == '__main__':
    unittest.main()
