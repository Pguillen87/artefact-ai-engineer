"""Full-flow regressions for the approved F1/F2 structural decisions."""
import json
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import test_p2_corrections as fixtures
from artefact.agent import Agent
from artefact.conversation import ConversationAgent
from artefact.context import resolve_commercial_basis
from artefact.rendering import render_results
from artefact.agent import WRITE_ISOLATION
from baseline_support import load_original_baseline


class StructuralCommercialTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'price_brl': '1127.00', 'name': 'Teclado Sintetizador Korg Synth 1 Pro'},
            'promotions': {'is_active': '0'},
        })

    def plans(self, amount='900.00'):
        return [('installment_quote', {'amount_brl': amount, 'installments': 3}),
                ('search_products', {'text': 'Korg', 'installments': 3})]

    def run_plan(self, text, plan, conversational):
        if conversational:
            agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider([
                fixtures.call(*plan)]))
            answer = agent.answer(text, chat_id='case')
            return answer, agent.last_trace
        planner = Mock()
        planner.plan.return_value = [plan]
        result = Agent(self.snapshot, planner).answer(text, structured=True)
        return render_results(result) if isinstance(result, list) else result, result

    def test_abbreviated_conflict_is_consistent_in_both_executors(self):
        for text in ('Simule R$ 900 em três vezes para o Korg.',
                     'Quero o Korg por R$ 900 em 3x.'):
            for conversational in (False, True):
                for plan in self.plans():
                    with self.subTest(text=text, route=conversational, plan=plan):
                        answer, evidence = self.run_plan(text, plan, conversational)
                        self.assertNotIn('parcelamento permitido', answer.lower())
                        self.assertIn('900,00', answer)
                        self.assertIn('1.127,00', answer)
                        self.assertIn('diverge', answer)

    def test_direct_catalog_quote_works_with_either_tool(self):
        for route in (False, True):
            for plan in self.plans('1127.00'):
                with self.subTest(route=route, plan=plan):
                    answer, _ = self.run_plan('Quanto fica o Korg em três vezes?', plan, route)
                    self.assertIn('375,67 + R$ 375,67 + R$ 375,66', answer)
                    self.assertNotIn('Esclareça', answer)

    def test_negated_conditional_or_disputed_catalog_never_quotes(self):
        texts = ('Quero o Korg. Não confirme o preço do catálogo e simule em 3x.',
                 'Se confirmar o preço do catálogo do Korg, simule em 3x.',
                 'O preço do catálogo do Korg está incorreto. Simule em 3x.',
                 'Não simule o Korg em 3x.')
        for text in texts:
            for route in (False, True):
                for plan in self.plans('1127.00'):
                    with self.subTest(text=text, route=route, plan=plan):
                        answer, _ = self.run_plan(text, plan, route)
                        self.assertNotIn('parcelamento permitido', answer.lower())

    def test_hypothesis_example_is_independent_of_commercial_catalog(self):
        for text in ('Simule R$ 900 em três parcelas, apenas como exemplo para o Korg.',
                     'Para o Korg, simule R$ 900 em 3x de forma hipotética.'):
            for route in (False, True):
                for plan in self.plans():
                    with self.subTest(text=text, route=route, plan=plan):
                        answer, _ = self.run_plan(text, plan, route)
                        self.assertIn('hipot', answer.lower())
                        self.assertIn('3x R$ 300,00', answer)
                        self.assertNotIn('1.127', answer)

    def test_budget_never_becomes_total_in_followup(self):
        for first in ('R$ 900 disponíveis para compra. Quanto fica em 3x?',
                      'Tenho R$ 900 disponíveis para comprar um Korg. Quanto fica em 3x?'):
            provider = fixtures.ScriptedProvider(
                [fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})],
                [fixtures.call('installment_quote', {'amount_brl': '900.00', 'installments': 3})])
            agent = ConversationAgent(self.snapshot, provider)
            for text in (first, 'E em 3x?'):
                with self.subTest(text=text):
                    answer = agent.answer(text, chat_id='budget')
                    self.assertNotIn('3x R$ 300,00', answer)

    def test_restriction_survives_followup_and_explicit_reselection_clears_it(self):
        for restriction in ('Não confirme o preço do catálogo do Korg.',
                            'Se confirmar o preço do catálogo do Korg, simule em 3x.',
                            'Não quero mais o Korg.'):
            provider = fixtures.ScriptedProvider(*[
                [fixtures.call('search_products', {'text': 'Korg', 'installments': 3})]
                for _ in range(4)])
            agent = ConversationAgent(self.snapshot, provider)
            agent.answer(restriction, chat_id='restriction')
            followup = agent.answer('E em três vezes?', chat_id='restriction')
            self.assertNotIn('parcelamento permitido', followup.lower(), restriction)
            self.assertNotIn('search_products', agent.last_trace['executed_tools'])
            resumed = agent.answer('Quero consultar novamente o Korg Synth 1 Pro em 3x.', chat_id='restriction')
            self.assertIn('375,67', resumed, restriction)
            self.assertNotIn('parcelamento permitido', agent.answer('E em 3x?', chat_id='other').lower())

    def test_product_and_shipping_amounts_have_independent_roles(self):
        for text in ('Quero o Korg por R$ 900 em 3x, e o frete custa R$ 35?',
                     'O frete custa R$ 35? Quero o Korg por R$ 900 em 3x.'):
            basis = resolve_commercial_basis(text, self.snapshot)
            self.assertEqual(basis.amount, Decimal('900'))
            self.assertEqual(basis.catalog_amount, Decimal('1127'))
            for name, args in self.plans():
                agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider([
                    fixtures.call(name, args, 'price'), fixtures.call('policy', {'topic': 'shipping'}, 'shipping')]))
                answer = agent.answer(text, chat_id='multi')
                self.assertIn('diverge', answer)
                self.assertIn('Frete metropolitano', answer)
                self.assertNotIn('parcelamento permitido', answer.lower())
                self.assertEqual(agent.last_trace['executed_tools'], ['policy'])
                self.assertEqual(agent.last_trace['status'], 'partial')

    def test_policy_polarity_is_scoped_to_the_requested_operation(self):
        for text in ('Não quero saber de frete.', 'Se eu pedir, consulte o frete.',
                     'A palavra frete aparece no texto.'):
            for route in (False, True):
                answer, evidence = self.run_plan(text, ('policy', {'topic': 'shipping'}), route)
                self.assertNotIn('Frete metropolitano', answer, text)
        text = 'Não quero saber de garantia, mas quais são as regras de frete?'
        for route in (False, True):
            answer, _ = self.run_plan(text, ('policy', {'topic': 'shipping'}), route)
            self.assertIn('Frete metropolitano', answer)

    def test_distinctive_partial_name_is_not_confused_with_shared_model_words(self):
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'price_brl': '1127.00', 'name': 'Teclado Sintetizador Korg Synth 1 Pro'},
            'promotions': {'is_active': '0'},
        }, extra_products=[fixtures.product_row(2, '2000', 'Teclado Roland Synth 3'),
                           fixtures.product_row(3, '5000', 'Guitarra Fender American Pro II')])
        for text in ('Quanto fica o Korg Synth 1 Pro em 3x?', 'Quanto fica o Korg em 3x?'):
            decision = resolve_commercial_basis(text, self.snapshot)
            self.assertEqual(decision.product['product_id'] if decision.product else None, 1)
            self.assertEqual(decision.catalog_amount, Decimal('1127'))
        # A shared reference is ambiguous; historical selection must not pick one.
        agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider(
            [fixtures.call('search_products', {'text': 'Korg'})],
            [fixtures.call('search_products', {'text': 'Roland'})],
            [fixtures.call('search_products', {'text': 'Korg', 'installments': 3})]))
        agent.answer('Quero Korg ou Roland.', chat_id='ambiguous')
        agent.answer('Aquele teclado.', chat_id='ambiguous')
        self.assertNotIn('parcelamento permitido', agent.answer('E em 3x?', chat_id='ambiguous').lower())

    def test_real_baseline_partial_names_and_price_authority(self):
        self.snapshot = load_original_baseline(self)
        for route in (False, True):
            for plan in self.plans('1127.00'):
                answer, _ = self.run_plan('Quanto fica o Korg Synth 1 Pro em 3x?', plan, route)
                self.assertIn('375,67 + R$ 375,67 + R$ 375,66', answer)
        for plan in self.plans():
            answer, _ = self.run_plan('Quero o Korg por R$ 900 em 3x.', plan, True)
            self.assertIn('diverge', answer)
            self.assertNotIn('parcelamento permitido', answer.lower())

    def test_final_executor_blocks_writes_inserted_by_a_plan_transform(self):
        from artefact.agent import prepare_commercial_call
        write = ('request_technical_review', {'product_id': 1, 'field': 'brand', 'reason': 'inconsistencia_catalogo'})
        for with_read in (False, True):
            callback = Mock(return_value=123)
            calls = [fixtures.call('search_products', {'text': 'Korg'}, 'catalog')]
            if with_read:
                calls.append(fixtures.call('policy', {'topic': 'shipping'}, 'shipping'))
            agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider(calls), review_submit=callback)
            def injected(name, args, *rest):
                return write if name == 'search_products' else prepare_commercial_call(name, args, *rest)
            with patch('artefact.agent.prepare_commercial_call', side_effect=injected):
                answer = agent.answer('O Korg está disponível? Quais as regras de frete?', chat_id='injection')
            callback.assert_not_called()
            if with_read:
                self.assertEqual(answer, WRITE_ISOLATION)
            self.assertNotIn('registrada', answer)

    def test_multiple_money_roles_and_validated_arguments_are_observable(self):
        decision = resolve_commercial_basis('Quero o Korg por R$ 900 em 3x; o frete custa R$ 35?', self.snapshot)
        self.assertEqual([(v.role, v.amount) for v in decision.values],
                         [('claimed_product_price', Decimal('900')), ('shipping_amount', Decimal('35'))])
        self.assertFalse(decision.quote_allowed)
        for route in (False, True):
            for bad in (('installment_quote', {'amount_brl': '999', 'installments': 3}),
                        ('search_products', {'text': 'Korg', 'installments': 4}),
                        ('search_products', {'text': 'Unknown', 'installments': 3})):
                answer, _ = self.run_plan('Quanto fica o Korg em 3x?', bad, route)
                self.assertNotIn('parcelamento permitido', answer.lower())

    def test_direct_executor_keeps_shipping_when_product_quote_is_rejected(self):
        for plan in self.plans():
            planner = Mock()
            planner.plan.return_value = [plan, ('policy', {'topic': 'shipping'})]
            result = Agent(self.snapshot, planner).answer('Quero o Korg por R$ 900 em 3x; quais as regras de frete?', structured=True)
            self.assertIsInstance(result, list)
            self.assertEqual([r['consulta'] for r in result], ['price_mismatch', 'policy'])
            answer = render_results(result)
            self.assertIn('diverge', answer)
            self.assertIn('Frete metropolitano', answer)
            self.assertNotIn('parcelamento permitido', answer.lower())

    def test_semantic_total_still_requires_an_unambiguous_installment_request(self):
        for text in ('Quero pagar R$ 900 3x', 'Quero pagar 3x R$ 900',
                     'Simule R$ 900 e 1200 em 3x'):
            for route in (False, True):
                answer, _ = self.run_plan(text, self.plans()[0], route)
                self.assertNotIn('parcelamento permitido', answer.lower(), text)

    def test_product_model_number_is_not_the_requested_installment_count(self):
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'price_brl': '1127.00', 'name': 'Teclado Yamaha Bass 3X'},
            'promotions': {'is_active': '0'}})
        for route in (False, True):
            answer, _ = self.run_plan('Quanto fica o Teclado Yamaha Bass 3X em 6x?',
                                      ('search_products', {'text': 'Yamaha Bass 3X', 'installments': 6}), route)
            self.assertIn('6x', answer)
            self.assertIn('187,84', answer)

    def test_independent_negative_clause_does_not_revoke_a_valid_product_request(self):
        for text in ('Não quero saber de frete, mas quanto fica o Korg em 3x?',
                     'Não quero saber de frete; quanto fica o Korg em 3x?'):
            for route in (False, True):
                answer, _ = self.run_plan(text, self.plans('1127.00')[1], route)
                self.assertIn('375,67', answer, text)
                self.assertNotIn('Frete metropolitano', answer)

    def test_restriction_retains_identity_without_becoming_selection(self):
        decision = resolve_commercial_basis('Não confirme o preço do catálogo do Korg.', self.snapshot)
        self.assertIsNotNone(decision.product)
        self.assertEqual(decision.product['product_id'], 1)
        self.assertEqual(decision.restriction, 'negative')
        self.assertFalse(decision.quote_allowed)

    def test_rejected_product_does_not_block_an_independent_affirmative_replacement(self):
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'price_brl': '1127.00', 'name': 'Korg Synth 1 Pro'},
            'promotions': {'is_active': '0'}},
            extra_products=[fixtures.product_row(2, '1500', 'Yamaha PSR')])
        for route in (False, True):
            answer, _ = self.run_plan('Não quero Korg Synth 1 Pro; quanto fica o Yamaha PSR em 3x?',
                                      ('search_products', {'text': 'Yamaha PSR', 'installments': 3}), route)
            self.assertIn('3x R$ 500,00', answer)
            self.assertNotIn('Korg Synth 1 Pro:', answer)

    def test_shipping_mention_cannot_hide_a_commercial_restriction(self):
        for text in ('O preço do catálogo do Korg com frete está incorreto. Simule em 3x.',
                     'Quanto fica o Korg em 3x? Não use o preço do catálogo com frete.'):
            for route in (False, True):
                for plan in self.plans('1127.00'):
                    with self.subTest(text=text, route=route, plan=plan):
                        answer, _ = self.run_plan(text, plan, route)
                        self.assertNotIn('parcelamento permitido', answer.lower())

    def test_policy_refusals_with_pronouns_and_interest_are_not_requests(self):
        for text in ('Não me informe sobre frete.', 'Não me mostre a política de frete.',
                     'Não tenho interesse em saber sobre frete.', 'Frete: não me informe sobre isso.'):
            for route in (False, True):
                with self.subTest(text=text, route=route):
                    answer, _ = self.run_plan(text, ('policy', {'topic': 'shipping'}), route)
                    self.assertNotIn('Frete metropolitano', answer)
        for text in ('Meu instrumento pode ser trocado só porque não gostei?',
                     'Não gostei do instrumento. Quais as regras de troca?'):
            answer, _ = self.run_plan(text, ('policy', {'topic': 'exchange'}), True)
            self.assertIn('troca', answer.lower())

    def test_revoked_stated_price_and_unrelated_hypothesis_do_not_authorize_total(self):
        agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider(
            [], [fixtures.call('installment_quote', {'amount_brl': '900', 'installments': 3})]))
        agent.answer('Não quero um instrumento de R$ 900.', chat_id='revoked')
        answer = agent.answer('Esse valor em 3x?', chat_id='revoked')
        self.assertNotIn('parcelamento permitido', answer.lower())
        text = 'Faça uma simulação hipotética do frete. Quero o Korg por R$ 900 em 3x.'
        for route in (False, True):
            answer, _ = self.run_plan(text, self.plans()[0], route)
            self.assertNotIn('parcelamento permitido', answer.lower())


if __name__ == '__main__':
    unittest.main()
