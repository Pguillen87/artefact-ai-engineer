import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from artefact.agent import Agent, WRITE_ISOLATION
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.intent import validate_catalog_intent
from test_data import write_dataset


class _Planner:
    def __init__(self, calls):
        self.calls = calls

    def plan(self, _text):
        return self.calls


def _call(name, args, call_id='1'):
    return {'type': 'function_call', 'name': name,
            'arguments': json.dumps(args), 'call_id': call_id}


class SharedPlanPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        write_dataset(root)
        self.snapshot = load_dataset(root)

    def test_direct_and_conversation_paths_share_grounding_rejection(self):
        text = 'Quero teclado.'
        proposed = [('search_products', {'category': 'Guitarras'})]
        provider = Mock()
        provider.request.return_value = {'output': [_call(*proposed[0])]}

        with patch('artefact.agent.validate_catalog_intent', wraps=validate_catalog_intent) as validate:
            direct = Agent(self.snapshot, _Planner(proposed)).answer(text)
            self.assertEqual(validate.call_count, 1)

        with patch('artefact.agent.validate_catalog_intent', wraps=validate_catalog_intent) as validate:
            conversational = ConversationAgent(self.snapshot, provider).answer(text, chat_id=1)
            self.assertEqual(validate.call_count, 1)

        self.assertEqual(direct, conversational)
        self.assertIn('Qual instrumento ou modelo', direct)
        self.assertNotIn('Korg Synth', direct)

    def test_direct_and_conversation_paths_execute_the_same_grounded_search(self):
        text = 'Quero teclado até R$ 1.200.'
        proposed = [('search_products', {
            'category': 'Teclados e Pianos', 'max_price': '1200'})]
        provider = Mock()
        provider.request.return_value = {'output': [_call(*proposed[0])]}

        with patch('artefact.agent.validate_catalog_intent', wraps=validate_catalog_intent) as validate:
            direct_result = Agent(self.snapshot, _Planner(proposed)).answer(text, structured=True)
            self.assertEqual(validate.call_count, 1)

        with patch('artefact.agent.validate_catalog_intent', wraps=validate_catalog_intent) as validate:
            conversational = ConversationAgent(self.snapshot, provider).answer(text, chat_id=1)
            self.assertEqual(validate.call_count, 1)

        direct_product = direct_result[0]['resultado'][0]
        self.assertEqual(direct_product['name'], 'Korg Synth')
        self.assertIn('Korg Synth', conversational)
        self.assertIn('R$ 1.127,50', conversational)

    def test_write_isolation_is_identical_across_both_entry_points(self):
        proposed = [
            ('request_technical_review', {
                'product_id': 1, 'field': 'specs.keys', 'reason': 'inconsistencia_catalogo'}),
            ('policy', {'topic': 'pix'}),
        ]
        provider = Mock()
        provider.request.return_value = {'output': [
            _call(*proposed[0], call_id='write'), _call(*proposed[1], call_id='read')]}
        submit = Mock(return_value=17)

        direct = Agent(self.snapshot, _Planner(proposed), review_submit=submit).answer(
            'Registre revisão das teclas do produto 1 e consulte Pix')
        conversation = ConversationAgent(self.snapshot, provider, review_submit=submit).answer(
            'Registre revisão das teclas do produto 1 e consulte Pix', chat_id=1)

        self.assertEqual(direct, WRITE_ISOLATION)
        self.assertEqual(conversation, WRITE_ISOLATION)
        submit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
