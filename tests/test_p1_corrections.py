import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from test_data import write_dataset
from test_admin import csv_bytes, product
from artefact.admin import CatalogAdmin, published_snapshot
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.state import State


def call(name, args):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': '1'}


class P1Corrections(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); write_dataset(self.root)
        self.snapshot = load_dataset(self.root)
        self.provider = Mock()

    def plan(self, name, args):
        self.provider.request.return_value = {'output': [call(name, args)]}

    def test_budget_is_not_an_order_total(self):
        for follow in ('Quanto fica em 3x?', 'E em 3x sem juros?', 'Quanto fica em três vezes?'):
            self.provider.request.side_effect = [{'output': []}, {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}]
            agent = ConversationAgent(self.snapshot, self.provider)
            agent.answer('Tenho no máximo R$ 900.', chat_id=1)
            answer = agent.answer(follow, chat_id=1)
            self.assertNotIn('300,00', answer)
            self.provider.reset_mock(side_effect=True)

    def test_budget_in_current_turn_is_not_automatically_a_total(self):
        self.plan('installment_quote', {'amount_brl': '900', 'installments': 3})
        for message in ('Tenho orçamento de R$ 900 em 3x.', 'Tenho até R$ 900 para gastar em 3x.'):
            self.assertNotIn('300,00', ConversationAgent(self.snapshot, self.provider).answer(message))

    def test_spending_limit_is_not_a_total(self):
        self.provider.request.side_effect = [{'output': []}, {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, self.provider)
        agent.answer('Meu limite é R$ 900.', chat_id=1)
        self.assertNotIn('300,00', agent.answer('Quanto fica em 3x?', chat_id=1))

    def test_confirmed_total_and_written_followup(self):
        self.provider.request.side_effect = [{'output': []}, {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, self.provider)
        agent.answer('Minha compra custa R$ 900.', chat_id=1)
        self.assertIn('300,00', agent.answer('Quanto fica em três vezes?', chat_id=1))

    def test_policy_valid_and_product_positive(self):
        self.plan('policy', {'topic': 'exchange'})
        self.assertNotIn('esclareça', ConversationAgent(self.snapshot, self.provider).answer('Como funciona a troca?').lower())
        self.plan('search_products', {'text': 'Korg Synth'})
        self.assertIn('Korg Synth', ConversationAgent(self.snapshot, self.provider).answer('Quero Korg Synth.'))

    def test_negated_product_variants_are_not_selected(self):
        self.plan('search_products', {'text': 'Korg Synth'})
        for prefix in ('Não quero', 'Não quero o', 'Sem', 'Exceto', 'Não o', 'Não gosto de', 'Não recomende', 'Não me recomende'):
            with self.subTest(prefix=prefix):
                answer = ConversationAgent(self.snapshot, self.provider).answer(prefix + ' Korg Synth.')
                self.assertNotIn('preço cadastrado', answer.lower())

    def test_current_total_overrides_old_budget_and_chats_are_isolated(self):
        self.provider.request.side_effect = [{'output': []}, {'output': [call('installment_quote', {'amount_brl': '600', 'installments': 3})]},
                                             {'output': [call('installment_quote', {'amount_brl': '900', 'installments': 3})]}]
        agent = ConversationAgent(self.snapshot, self.provider)
        agent.answer('Tenho orçamento de R$ 900.', chat_id=1)
        self.assertIn('200,00', agent.answer('Quero parcelar R$ 600 em 3x.', chat_id=1))
        self.assertNotIn('300,00', agent.answer('Quanto fica em 3x?', chat_id=2))

    def test_empty_result_has_terminal_event(self):
        self.plan('search_products', {'text': 'Inexistente'})
        with self.assertLogs('artefact.operations', level=logging.INFO) as logs:
            answer = ConversationAgent(self.snapshot, self.provider).answer('Procuro Inexistente')
        self.assertIn('não encontrei', answer.lower())
        self.assertIn('"result": "empty"', '\n'.join(logs.output))

    def test_incompatible_policy_is_not_rendered(self):
        self.plan('policy', {'topic': 'warranty'})
        answer = ConversationAgent(self.snapshot, self.provider).answer('Quais são as condições de troca por defeito?')
        self.assertNotIn('garantia legal', answer.lower())

    def test_excluded_product_is_not_recommended(self):
        self.plan('search_products', {'text': 'Korg Synth'})
        answer = ConversationAgent(self.snapshot, self.provider).answer('Não recomende Korg Synth.')
        self.assertNotIn('preço cadastrado', answer.lower())

    def test_uncertain_write_after_commit_is_not_declared_no_effect(self):
        admin = CatalogAdmin(self.root/'admin.db', self.root/'serving.db', self.snapshot)
        batch = admin.stage(csv_bytes(product()))
        for issue in admin.preview(batch)['issues']:
            admin.decide(batch, issue['id'], 'accept', 'revisado')
        admin.approve(batch, 'revisto'); admin.publish(batch)
        snapshot = published_snapshot(self.root/'serving.db', self.snapshot)
        def submit(pid, field, reason, version):
            admin.create_review(pid, field, reason, version)
            raise RuntimeError('synthetic after commit')
        self.plan('request_technical_review', {'product_id': 2, 'field': 'specs.keys', 'reason': 'inconsistencia_catalogo'})
        with self.assertLogs('artefact.operations', level=logging.WARNING) as logs:
            answer = ConversationAgent(snapshot, self.provider, review_submit=submit).answer('Registre revisão técnica das teclas do produto 2')
        self.assertEqual(len(admin.review_cases()), 1)
        self.assertIn('antes de tentar novamente', answer)
        self.assertIn('"side_effect": "unknown"', '\n'.join(logs.output))

    def test_token_counts_accumulate(self):
        state = State(self.root/'state.db'); state.record_tokens(12, 7); state.record_tokens(5, 3)
        self.assertEqual((state.usage()['input_tokens'], state.usage()['output_tokens']), (17, 10))
