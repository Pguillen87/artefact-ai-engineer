import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from test_data import write_dataset


def call(name, args):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': '1'}


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); write_dataset(root); self.snapshot = load_dataset(root)

    def test_available_money_at_start_is_not_total(self):
        provider = Mock(); provider.request.return_value = {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}
        answer = ConversationAgent(self.snapshot, provider).answer('R$ 900 disponíveis para compra; quanto fica em 3x?')
        self.assertNotIn('3x R$ 300,00', answer)
        for text in ('Tenho R$ 900 disponíveis para compra em 3x.', 'R$ 900 é o máximo que posso gastar em 3x?',
                     'Posso gastar até R$ 900 em 3x?', 'Meu limite é R$ 900 em 3x?'):
            with self.subTest(text=text):
                self.assertNotIn('3x R$ 300,00', ConversationAgent(self.snapshot, provider).answer(text))

    def test_refused_product_cannot_be_followup_reference(self):
        for text in ('Não quero mais Korg Synth.', 'Não quero esse Korg Synth.', 'Quero Korg Synth, mas não quero mais esse.'):
            with self.subTest(text=text):
                provider = Mock(); provider.request.side_effect = [
                    {'output': [call('search_products', {'text': 'Korg Synth'})]},
                    {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
                agent = ConversationAgent(self.snapshot, provider)
                agent.answer(text, chat_id=1)
                self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=1))

    def test_revocation_and_explicit_reselection(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]}, {'output': []},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]},
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        self.assertIn('Korg Synth', agent.answer('Quero Korg Synth.', chat_id=1))
        agent.answer('Não quero mais esse.', chat_id=1)
        self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=1))
        self.assertIn('Korg Synth', agent.answer('Mudei de ideia. Quero Korg Synth.', chat_id=1))
        self.assertIn('3x R$', agent.answer('E em 3x?', chat_id=1))

    def test_budget_and_rejection_are_not_tool_authority(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]},
            {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        answer = agent.answer('Tenho R$ 900 disponíveis. Não quero mais Korg Synth em 3x.', chat_id=1)
        self.assertNotIn('3x R$', answer)
        self.assertNotIn('3x R$ 300,00', agent.answer('E em 3x?', chat_id=1))

    def test_explicit_total_and_simulation(self):
        provider = Mock(); provider.request.return_value = {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}
        for text in ('Minha compra ficou em R$ 900; quanto fica em 3x?', 'Simule R$ 900 em 3 parcelas.'):
            with self.subTest(text=text):
                self.assertIn('3x R$ 300,00', ConversationAgent(self.snapshot, provider).answer(text))

    def test_no_cross_chat_selection(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Quero Korg Synth.', chat_id=1)
        self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=2))

    def test_tool_result_alone_does_not_select_a_mention(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Korg Synth.', chat_id=1)
        self.assertNotIn('3x R$', agent.answer('E em 3x?', chat_id=1))

    def test_selected_product_explicit_followup_uses_snapshot_checkout(self):
        provider = Mock(); provider.request.side_effect = [
            {'output': [call('search_products', {'text': 'Korg Synth'})]},
            {'output': [call('search_products', {'text': 'Korg Synth', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        agent.answer('Quero Korg Synth.', chat_id=1)
        self.assertIn('3x R$ 338,25', agent.answer('Quanto fica esse produto em 3x?', chat_id=1))
