"""Four final P2 guarantees; offline plans and observable effects only."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import test_p2_corrections as fixtures
from artefact.agent import Agent
from artefact.conversation import ConversationAgent
from artefact.rendering import render_results


class FinalFocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = fixtures.P2CorrectionTests.snapshot(self, {
            'products': {'name': 'Korg Synth', 'price_brl': '1127.00'},
            'promotions': {'is_active': '0'}}, extra_products=[
                fixtures.product_row(2, '1500', 'Yamaha PSR'),
                fixtures.product_row(3, '400', 'Roland Go')])

    def ask(self, text, plans, conversation):
        if conversation:
            agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider([
                fixtures.call(n, a, str(i)) for i, (n, a) in enumerate(plans)]))
            answer = agent.answer(text, chat_id='focal')
            return answer, agent.last_trace
        planner = Mock()
        planner.plan.return_value = plans
        result = Agent(self.snapshot, planner).answer(text, structured=True)
        return render_results(result) if isinstance(result, list) else result, result

    def test_budget_is_mandatory_even_when_provider_omits_upper_bound(self):
        for route in (False, True):
            for tool in ('search_products', 'recommend_products'):
                args = {'category': 'Teclados e Pianos'}
                if tool == 'search_products':
                    args['installments'] = 3
                with self.subTest(route=route, tool=tool):
                    answer, evidence = self.ask('Tenho orçamento de R$ 500 para teclados. Mostre opções em 3x.', [(tool, args)], route)
                    self.assertIn('Roland Go', answer)
                    self.assertNotIn('Korg Synth:', answer)
                    self.assertNotIn('Yamaha PSR:', answer)
                    if route:
                        self.assertEqual(evidence['executed_tools'], [tool])

    def test_refusal_filters_search_and_recommendations_without_blocking_others(self):
        for route in (False, True):
            for tool in ('search_products', 'recommend_products'):
                with self.subTest(route=route, tool=tool):
                    answer, _ = self.ask('Não quero o Korg. Recomende opções da categoria Teclados e Pianos.',
                                         [(tool, {'category': 'Teclados e Pianos'})], route)
                    self.assertIn('Yamaha PSR', answer)
                    self.assertIn('Roland Go', answer)
                    self.assertNotIn('Korg Synth:', answer)

    def test_refusal_survives_relevant_followup_and_explicit_reselection_restores(self):
        provider = fixtures.ScriptedProvider(
            [fixtures.call('recommend_products', {'category': 'Teclados e Pianos'})],
            [fixtures.call('search_products', {'text': 'Korg', 'installments': 3})])
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Não quero Korg. Recomende opções de teclados.', chat_id='scope')
        second = agent.answer('Mostre outras opções de teclados.', chat_id='scope')
        self.assertNotIn('Korg Synth:', second)
        self.assertIn('Já mostrei todas as opções', second)
        third = agent.answer('Quero novamente o Korg em 3x.', chat_id='scope')
        self.assertIn('375,67', third)

    def test_non_installment_search_cannot_leak_restricted_prices(self):
        for route in (False, True):
            for text in ('Quero o Korg. Não confirme o preço do catálogo e simule em 3x.',
                         'Quero o Korg por R$ 900. Não confirme o preço do catálogo e simule em 3x.'):
                with self.subTest(route=route, text=text):
                    answer, _ = self.ask(text, [('search_products', {'text': 'Korg'})], route)
                    self.assertNotIn('1.127', answer)
                    self.assertNotIn('1.070', answer)
                    self.assertNotIn('preço Pix', answer)

    def test_independent_availability_remains_visible_without_restricted_price(self):
        for route in (False, True):
            answer, _ = self.ask('Quero o Korg. Não confirme o preço do catálogo. Está disponível?',
                                 [('search_products', {'text': 'Korg'})], route)
            self.assertIn('disponível', answer)
            self.assertNotIn('1.127', answer)
            self.assertNotIn('Pix', answer)

    def test_no_need_to_confirm_is_not_a_price_prohibition(self):
        for route in (False, True):
            answer, _ = self.ask('Quanto fica o Korg em 3x? Não precisa confirmar o preço do catálogo.',
                                 [('search_products', {'text': 'Korg', 'installments': 3})], route)
            self.assertIn('375,67', answer)

    def test_explicit_correction_replaces_the_disputed_basis_in_context(self):
        for correction in ('R$ 900 estava errado. Use o preço efetivo atual do catálogo.',
                           'Não use aquele preço. Na verdade, use o preço atual do catálogo.'):
            provider = fixtures.ScriptedProvider(
                [fixtures.call('search_products', {'text': 'Korg', 'installments': 3})],
                [fixtures.call('search_products', {'text': 'Korg'})],
                [fixtures.call('search_products', {'text': 'Korg', 'installments': 3})])
            agent = ConversationAgent(self.snapshot, provider)
            agent.answer('Quero o Korg por R$ 900 em 3x.', chat_id='correction')
            second = agent.answer(correction, chat_id='correction')
            self.assertIn('1.127', second)
            self.assertNotIn('diverge', second)
            self.assertIn('375,67', agent.answer('E em 3x?', chat_id='correction'))

    def test_same_turn_product_replacement_respects_clause_order(self):
        for separator in (', ', '. ', ' e ', ', mas '):
            text = 'Não quero o Korg' + separator + 'prefiro o Yamaha PSR em 3x.'
            for route in (False, True):
                with self.subTest(separator=separator, route=route):
                    answer, _ = self.ask(text, [('search_products', {'text': 'Yamaha PSR', 'installments': 3})], route)
                    self.assertIn('3x R$ 500,00', answer)
                    self.assertNotIn('Korg Synth:', answer)

    def test_no_tool_cannot_claim_a_simulation_or_success(self):
        text = 'Quero o Korg Synth por R$ 900 em 3x.'
        answer, trace = self.ask(text, [], True)
        self.assertEqual(trace['executed_tools'], [])
        self.assertNotEqual(trace['status'], 'success')
        self.assertNotIn('A simulação desta resposta usa', answer)
        self.assertNotIn('parcelamento permitido', answer)

    def test_conflict_does_not_suppress_independent_category_search(self):
        for route in (False, True):
            answer, evidence = self.ask('Quero o Korg por R$ 900, mas mostre também teclados disponíveis.', [
                ('installment_quote', {'amount_brl': '900', 'installments': 3}),
                ('search_products', {'category': 'Teclados e Pianos', 'available_only': True})], route)
            self.assertIn('diverge', answer)
            self.assertIn('Yamaha PSR', answer)
            self.assertIn('Roland Go', answer)

    def test_category_plan_cannot_broaden_an_identified_product_quote(self):
        for route in (False, True):
            answer, _ = self.ask('Korg teclado em 3x?', [
                ('search_products', {'category': 'Teclados e Pianos', 'installments': 3})], route)
            self.assertIn('Korg Synth', answer)
            self.assertNotIn('Roland Go', answer)
            self.assertNotIn('Yamaha PSR', answer)

    def test_explicit_multiple_product_query_is_preserved_without_basket(self):
        for route in (False, True):
            answer, _ = self.ask('Mostre o Korg Synth e o Yamaha PSR.', [
                ('search_products', {'text': 'Korg Synth'}),
                ('search_products', {'text': 'Yamaha PSR'})], route)
            self.assertIn('Korg Synth', answer)
            self.assertIn('Yamaha PSR', answer)
            self.assertNotIn('total da compra', answer)

    def test_one_shipping_need_produces_one_specific_clarification(self):
        text = 'Quero o Korg por R$ 900 em 3x. Quais as regras de frete?'
        answer, trace = self.ask(text, [('policy', {'topic': 'warranty'})], True)
        self.assertEqual(trace['executed_tools'], [])
        # Two distinct needs: product basis and shipping. Shipping must not get
        # both a specific and a second generic policy clarification.
        self.assertEqual(answer.lower().count('pergunta sobre frete'), 1)
        self.assertEqual(answer.lower().count('esclareça'), 2)
        self.assertIn('frete', answer)
        self.assertNotIn('Não consegui validar a consulta da política', answer)

    def test_direct_executor_checks_isolation_after_injected_transformation(self):
        from artefact.agent import prepare_commercial_call
        planner = Mock()
        planner.plan.return_value = [('search_products', {'text': 'Korg'}), ('policy', {'topic': 'shipping'})]
        callback = Mock(return_value=123)
        def injected(name, args, *rest):
            if name == 'search_products':
                return 'request_technical_review', {'product_id': 1, 'field': 'specs.keys', 'reason': 'inconsistencia_catalogo'}
            return prepare_commercial_call(name, args, *rest)
        with patch('artefact.agent.prepare_commercial_call', side_effect=injected):
            Agent(self.snapshot, planner, callback).answer('Registre revisão das teclas do Korg Synth e consulte frete.', structured=True)
        callback.assert_not_called()

    def test_same_category_followup_keeps_budget_and_refusal_without_verb_dependency(self):
        provider = fixtures.ScriptedProvider(*[
            [fixtures.call('recommend_products', {'category': 'Teclados e Pianos'})] for _ in range(3)])
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Tenho orçamento de R$ 500 para teclados. Não quero Korg. Recomende opções de teclados.', chat_id='follow')
        second = agent.answer('Quais teclados você recomenda?', chat_id='follow')
        self.assertIn('Roland Go', second)
        self.assertNotIn('Korg Synth:', second)
        self.assertNotIn('Yamaha PSR:', second)
        other = agent.answer('Quais teclados você recomenda?', chat_id='different')
        self.assertIn('Korg Synth:', other)
        self.assertIn('Yamaha PSR:', other)

    def test_restricted_price_cannot_leak_through_promotion(self):
        for route in (False, True):
            answer, _ = self.ask('Quero o Korg Synth. Não confirme o preço do catálogo. Ele tem promoção?',
                                 [('promotion', {'product_id': 1})], route)
            self.assertNotIn('1.127', answer)
            self.assertNotIn('1.070', answer)

    def test_explicit_above_budget_options_revoke_only_the_ceiling(self):
        for route in (False, True):
            answer, _ = self.ask('Tenho orçamento de R$ 500 para teclados. Mostre opções acima de R$ 1.000.',
                                 [('search_products', {'category': 'Teclados e Pianos', 'min_price': '1000'})], route)
            self.assertIn('Yamaha PSR', answer)
            self.assertIn('Korg Synth', answer)
            self.assertNotIn('Roland Go', answer)

    def test_excluded_product_does_not_block_installments_for_valid_options(self):
        for route in (False, True):
            answer, _ = self.ask('Não quero Korg. Mostre opções de teclados em 3x.', [
                ('search_products', {'category': 'Teclados e Pianos', 'installments': 3})], route)
            self.assertNotIn('Korg Synth:', answer)
            self.assertIn('Yamaha PSR', answer)
            self.assertIn('3x R$ 500,00', answer)

    def test_revoking_price_restriction_does_not_require_reselecting_an_unrefused_product(self):
        agent = ConversationAgent(self.snapshot, fixtures.ScriptedProvider(
            [fixtures.call('search_products', {'text': 'Korg'})],
            [fixtures.call('search_products', {'text': 'Korg'})]))
        first = agent.answer('Não use o preço do catálogo do Korg.', chat_id='price')
        self.assertNotIn('1.127', first)
        second = agent.answer('Na verdade, use o preço atual do catálogo.', chat_id='price')
        self.assertIn('1.127', second)


if __name__ == '__main__':
    unittest.main()
