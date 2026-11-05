import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from test_data import ROWS, write_dataset


def call(name, args, ident='1'):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': ident}


class AuditBlockers(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        second = list(ROWS['products']); second[0] = '2'; second[1] = '500'; second[2] = 'Korg Alternative'
        write_dataset(self.root, extra={'products': second})
        self.snapshot = load_dataset(self.root)

    def test_c1_unconfirmed_money_not_a_total(self):
        provider = Mock(); provider.request.return_value = {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}
        for text in ('Tenho R$ 900 disponíveis para compra em 3x.', 'Disponho de R$ 900 para comprar em 3x.', 'Tenho R$ 900 disponíveis e quero pagar em 3x.'):
            with self.subTest(text=text):
                self.assertNotIn('3x R$ 300,00', ConversationAgent(self.snapshot, provider).answer(text))

    def test_c2_conflicting_product_price_cannot_be_reparsed(self):
        provider = Mock(); provider.request.return_value = {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}
        answer = ConversationAgent(self.snapshot, provider).answer('O Korg Synth custa R$ 900 em 3x?')
        self.assertNotIn('3x R$ 300,00', answer)

    def test_c2_search_then_adversarial_quote_does_not_override(self):
        provider = Mock(); provider.request.return_value = {'output': [
            call('search_products', {'text': 'Korg Synth'}),
            call('installment_quote', {'amount_brl': '900', 'installments': 3}, '2')]}
        answer = ConversationAgent(self.snapshot, provider).answer('O Korg Synth custa R$ 900 em 3x?')
        self.assertNotIn('3x R$ 300,00', answer)

    def test_c2_direct_executor_also_rejects_product_price_claim(self):
        from artefact.agent import Agent
        planner = Mock(); planner.plan.return_value = [('installment_quote', {'amount_brl': '900', 'installments': 3})]
        result = Agent(self.snapshot, planner).answer('Quero pagar R$ 900 em 3x pelo Korg.')
        self.assertNotIn('eligible', result)

    def test_c1_explicit_total_and_simulation_remain_valid(self):
        provider = Mock(); provider.request.return_value = {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}
        for text in ('Minha compra ficou em R$ 900. Quanto fica em 3x?', 'Simule um total de R$ 900 em três parcelas.'):
            with self.subTest(text=text):
                self.assertIn('3x R$ 300,00', ConversationAgent(self.snapshot, provider).answer(text))
        # A product linked to a divergent amount now requires clarification unless
        # the customer explicitly says the simulation is hypothetical.
        answer = ConversationAgent(self.snapshot, provider).answer('Simule um total de R$ 900 em 3x para Korg Synth.')
        self.assertIn('diverge', answer.lower())
        self.assertNotIn('3x R$ 300,00', answer)

    def test_c3_no_context_from_negated_product(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Não quero Korg Synth.', chat_id=1)
        answer = agent.answer('E em 3x?', chat_id=1)
        self.assertNotIn('3x R$', answer)

    def test_c3_multiple_searches_do_not_select_last(self):
        for names in [('Korg Synth', 'Korg Alternative'), ('Korg Alternative', 'Korg Synth')]:
            with self.subTest(order=names):
                provider = Mock(); provider.request.side_effect = [
                    {'output': [call('search_products', {'text': names[0]}), call('search_products', {'text': names[1]}, '2')]},
                    {'output': [call('search_products', {'text': names[1], 'installments': 3})]}]
                agent = ConversationAgent(self.snapshot, provider)
                agent.answer('Quero Korg Synth e Korg Alternative.', chat_id=1)
                self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=1))

    def test_c3_one_search_for_two_mentions_is_not_selection(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Quero Korg Synth e Korg Alternative.', chat_id=1)
        self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=1))

    def test_c3_multi_product_turn_cannot_quote_arbitrary_selection(self):
        provider = Mock(); provider.request.return_value = {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}
        answer = ConversationAgent(self.snapshot, provider).answer('Korg Synth e Korg Alternative em 3x?')
        self.assertNotIn('3x R$', answer)
        from artefact.agent import Agent
        planner = Mock(); planner.plan.return_value = [('search_products', {'text': 'Korg Synth', 'installments': 3})]
        result = Agent(self.snapshot, planner).answer('Korg Synth e Korg Alternative em 3x?')
        self.assertNotIn('installment_quote', result)
