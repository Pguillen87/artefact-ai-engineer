import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from artefact.agent import TelegramAdapter
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.telegram import TelegramClient, TelegramError
from test_data import ROWS, write_dataset


def tool(name, args):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': '1'}


class P1Closure(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); write_dataset(self.root)
        self.snapshot = load_dataset(self.root)

    def agent(self, outputs):
        provider = Mock(); provider.request.side_effect = outputs
        return ConversationAgent(self.snapshot, provider)

    def test_budget_dialogues_A_to_F(self):
        quote = lambda amount: {'output': [tool('installment_quote', {'amount_brl': amount, 'installments': 3})]}
        search = {'output': [tool('search_products', {'text': 'Korg Synth'})]}
        search3 = {'output': [tool('search_products', {'text': 'Korg Synth', 'installments': 3})]}
        no_tools = {'output': []}
        scenarios = [
            ('A', ['Tenho R$ 900 para gastar.', 'E em 3x sem juros?'], [no_tools, quote('900')], 'informe um único valor', '300,00'),
            ('B', ['Tenho R$ 900 para gastar.', 'Quero Korg Synth.', 'E em 3x?'], [no_tools, search, search3], '3x R$ 338,25', '3x R$ 300,00'),
            ('C', ['Tenho até R$ 900 e quero comprar um violão e um cajón.', 'Quanto fica em 3x?'], [no_tools, quote('900')], 'informe um único valor', '300,00'),
            ('D', ['Tenho até R$ 900 para gastar.', 'Korg Synth e outro produto em 3x.'], [no_tools, quote('900')], 'informe um único valor', '300,00'),
            ('E', ['Minha compra ficou em R$ 900. Quanto fica em 3x?'], [quote('900')], '3x R$ 300,00', None),
            ('F', ['Tenho R$ 900 para gastar.', 'Agora quero saber sobre teclados.', 'Quanto fica em 3x?'], [no_tools, no_tools, quote('900')], 'informe um único valor', '300,00'),
        ]
        for label, messages, outputs, expected, forbidden in scenarios:
            with self.subTest(label=label):
                agent = self.agent(outputs)
                for message in messages: answer = agent.answer(message, chat_id=7)
                if expected: self.assertIn(expected, answer)
                if forbidden: self.assertNotIn(forbidden, answer)

    def test_ignored_and_duplicate_updates_are_diagnosed(self):
        state = Mock(); state.claim_update.return_value = False
        adapter = TelegramAdapter(Mock(), Mock(), state=state, allowed_chat_id=1)
        with self.assertLogs('artefact.operations', level='INFO') as captured:
            adapter.handle({'update_id': 1, 'message': {'chat': {'id': 2, 'type': 'private'}, 'text': 'secret'}})
            adapter.handle({'update_id': 2, 'message': {'chat': {'id': 1, 'type': 'private'}, 'text': 'secret'}})
        self.assertNotIn('secret', str(captured.output))
        self.assertEqual(sum('"result": "ignored"' in line for line in captured.output), 2)

    def test_clarification_and_provider_failure_have_one_terminal_event(self):
        for response in ({'output': [tool('policy', {'topic': 'warranty'})]}, RuntimeError('secret')):
            provider = Mock()
            if isinstance(response, Exception): provider.request.side_effect = response
            else: provider.request.return_value = response
            with self.assertLogs('artefact.operations', level='INFO') as captured:
                ConversationAgent(self.snapshot, provider).answer('Como funciona a troca?')
            events = [json.loads(line.split(':', 2)[2]) for line in captured.output]
            terminal = [e for e in events if e['component'] == 'conversation' and e['stage'] == 'complete']
            self.assertEqual(len(terminal), 1)
            self.assertNotIn('secret', str(captured.output))

    def test_malformed_telegram_payload_and_transport(self):
        for response in (None, {}, {'ok': True, 'result': [None]}, {'ok': True, 'result': [{'update_id': '1'}]}):
            client = TelegramClient('synthetic', transport=Mock(return_value=response))
            with self.subTest(response=response), self.assertRaises(TelegramError): client.get_updates(0)
        client = TelegramClient('synthetic', transport=Mock(side_effect=OSError('synthetic')))
        with self.assertRaises(TelegramError): client.get_updates(0)

    def test_adversarial_tool_arguments_and_scope_less_recommendation(self):
        rejected = [
            ('Quero Yamaha.', 'search_products', {'text': 'Korg'}),
            ('Não recomende Korg Synth.', 'search_products', {'text': 'Korg Synth'}),
            ('Qual promoção do produto 9?', 'promotion', {'product_id': 1}),
            ('Quero teclado até R$ 500.', 'search_products', {'category': 'Teclados e Pianos', 'max_price': '1200'}),
            ('R$ 900 ou R$ 1200 em 3x?', 'installment_quote', {'amount_brl': '900', 'installments': 3}),
        ]
        for question, name, args in rejected:
            with self.subTest(question=question):
                agent = self.agent([{'output': [tool(name, args)]}])
                answer = agent.answer(question, chat_id=1)
                self.assertNotIn('3x R$ 300,00', answer)
                self.assertNotIn('preço cadastrado', answer.lower())
        agent = self.agent([{'output': [tool('recommend_products', {})]}])
        answer = agent.answer('Recomende opções.', chat_id=1)
        self.assertIn('qual tipo ou categoria', answer.lower())
        self.assertNotIn('Korg', answer)
        self.assertEqual(agent.last_trace['executed_tools'], [])

    def test_two_identified_products_are_not_given_an_invented_total(self):
        second = list(ROWS['products']); second[0] = '2'; second[1] = '500'; second[2] = 'Korg Alternative'
        write_dataset(self.root, extra={'products': second})
        snapshot = load_dataset(self.root)
        provider = Mock(); provider.request.return_value = {'output': [
            tool('search_products', {'text': 'Korg Synth'}),
            {**tool('search_products', {'text': 'Korg Alternative'}), 'call_id': '2'}]}
        answer = ConversationAgent(snapshot, provider).answer(
            'Meu teto é R$ 900. Quero Korg Synth e Korg Alternative.', chat_id=1)
        self.assertIn('resultados para Korg Synth dentro do orçamento', answer)
        self.assertIn('Korg Alternative:', answer)
        self.assertNotIn('total da compra', answer.lower())

    def test_invalid_provider_payload_never_calls_write(self):
        for payload in (None, {}, {'output': {}}, {'output': [None]},
                        {'output': [{'type': 'function_call', 'name': 'request_technical_review',
                                     'call_id': '1', 'arguments': '{invalid'}]}):
            with self.subTest(payload=payload):
                submit = Mock(); provider = Mock(); provider.request.return_value = payload
                answer = ConversationAgent(self.snapshot, provider, review_submit=submit).answer('Registre revisão do produto 1')
                submit.assert_not_called(); self.assertNotIn('registrada', answer.lower())

    def test_malformed_nested_update_never_calls_agent(self):
        agent = Mock(); send = Mock(); adapter = TelegramAdapter(agent, send)
        for update in (None, {}, {'update_id': 1, 'message': []},
                       {'update_id': 2, 'message': {'chat': 'bad', 'text': 'hello'}}):
            adapter.handle(update)
        agent.answer.assert_not_called(); send.assert_not_called()
