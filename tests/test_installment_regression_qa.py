import unittest
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from artefact.commercial import extract_installment_request
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from test_data import write_dataset


class InstallmentRegressionQA(unittest.TestCase):
    def test_value_per_installment_is_not_the_total(self):
        result = extract_installment_request('Quanto fica 3 vezes de R$ 150?')
        self.assertEqual(result, (Decimal('450'), 3))

    def test_final_punctuation_does_not_invalidate_amount(self):
        result = extract_installment_request('Quero pagar 3x de R$ 150,00.')
        self.assertEqual(result, (Decimal('450.00'), 3))

    def test_integrated_eight_cases_and_context(self):
        class Provider:
            def __init__(self, calls): self.calls = iter(calls)
            def request(self, messages):
                name, args = next(self.calls)
                return {'output': [{'type': 'function_call', 'name': name,
                                    'arguments': json.dumps(args), 'call_id': '1'}]}
        def call(amount, count=3):
            return ('installment_quote', {'amount_brl': amount, 'installments': count})
        policy = ('policy', {'topic': 'hours'})
        with TemporaryDirectory() as directory:
            root = Path(directory); write_dataset(root); snapshot = load_dataset(root)
            cases = [
                ('Quero pagar 3x de R$ 150,00.', '150.00', 'R$ 450,00', '3x R$ 150,00'),
                ('Quero parcelar R$ 150,00 em 3x.', '150.00', 'R$ 150,00', '3x R$ 50,00'),
                ('Quanto fica 3 vezes de R$ 150?', '150', 'R$ 450,00', '3x R$ 150,00'),
                ('Posso pagar R$ 450,00 em 3 parcelas?', '450.00', 'R$ 450,00', '3x R$ 150,00'),
            ]
            for text, amount, total, parts in cases:
                with self.subTest(text=text):
                    answer = ConversationAgent(snapshot, Provider([call(amount)])).answer(text, chat_id=1)
                    self.assertIn(total, answer); self.assertIn(parts, answer)
            dialogues = [
                (['Estou considerando um instrumento de R$ 900.', 'Quanto ficaria esse valor em 3x?'], '900', '3x R$ 300,00'),
                (['Estou comparando R$ 900 e R$ 1.200.', 'E em 3x, quanto fica?'], '900', 'informe um único valor'),
                (['Meu orçamento para um violão é R$ 900.', 'Agora quero saber sobre teclados.', 'Quanto fica em 3x?'], '900', 'informe um único valor'),
                (['Meu orçamento para um violão é R$ 900.', 'Quanto fica esse valor em 3x para teclados?'], '900', 'informe um único valor'),
                (['Quero saber sobre um produto de R$ 900.', 'Na verdade, R$ 600 em 3x.'], '600', '3x R$ 200,00'),
            ]
            for messages, amount, expected in dialogues:
                with self.subTest(messages=messages):
                    provider = Provider([policy] * (len(messages)-1) + [call(amount)])
                    agent = ConversationAgent(snapshot, provider)
                    for message in messages: answer = agent.answer(message, chat_id=7)
                    self.assertIn(expected, answer)

    def test_punctuation_and_adversarial_arguments(self):
        for suffix in ('.', '?', '!', ''):
            with self.subTest(suffix=suffix):
                self.assertEqual(extract_installment_request('Parcelar R$ 1.200,00' + suffix + ' em 3x'), (Decimal('1200'), 3))
                self.assertEqual(extract_installment_request('Quero pagar 3x de R$ 150,00' + suffix), (Decimal('450'), 3))
        for text in ('Parcelar R$ 150 ou R$ 200 em 3x?', 'Parcelar R$ 150 em 2x ou 3x?',
                     'Frete de R$ 150 em 3x?', 'Pedido histórico de R$ 150 em 3x?',
                     'Quero pagar 3x R$ 150', 'Quero pagar R$ 150 3x'):
            with self.subTest(text=text): self.assertIsNone(extract_installment_request(text))

    def test_tool_cannot_substitute_amount_and_below_minimum_is_rejected(self):
        class Provider:
            def __init__(self, amount): self.amount = amount
            def request(self, messages):
                return {'output': [{'type':'function_call','name':'installment_quote',
                    'arguments':json.dumps({'amount_brl':self.amount,'installments':3}),'call_id':'1'}]}
        with TemporaryDirectory() as directory:
            root=Path(directory); write_dataset(root); snapshot=load_dataset(root)
            answer=ConversationAgent(snapshot,Provider('450')).answer('Quanto fica 3 vezes de R$ 150?',chat_id=1)
            self.assertIn('não consegui', answer)
            answer=ConversationAgent(snapshot,Provider('40')).answer('Quero pagar 3x de R$ 40.',chat_id=1)
            self.assertIn('não é permitido', answer)
            self.assertIn('R$ 120,00', answer)

    def test_product_installments_use_catalog_price_not_budget(self):
        class Provider:
            def request(self,messages):
                return {'output':[{'type':'function_call','name':'search_products','call_id':'1',
                    'arguments':json.dumps({'text':'Korg','installments':3})}]}
        with TemporaryDirectory() as directory:
            root=Path(directory); write_dataset(root); agent=ConversationAgent(load_dataset(root),Provider())
            answer=agent.answer('Meu orçamento é R$ 900. Quanto custa Korg em 3x?',chat_id=1)
            self.assertIn('R$ 1.014,75',answer)
            self.assertIn('3x R$ 338,25',answer)
            self.assertNotIn('3x R$ 300,00',answer)

    def test_context_is_not_shared_between_chats(self):
        class Provider:
            def __init__(self): self.calls=0
            def request(self,messages):
                self.calls+=1
                if self.calls == 1: return {'output': []}
                return {'output':[{'type':'function_call','name':'installment_quote','call_id':'1',
                    'arguments':json.dumps({'amount_brl':'900','installments':3})}]}
        with TemporaryDirectory() as directory:
            root=Path(directory); write_dataset(root); agent=ConversationAgent(load_dataset(root),Provider())
            agent.answer('Estou considerando R$ 900.',chat_id=1)
            self.assertIn('informe um único valor',agent.answer('Quanto fica esse valor em 3x?',chat_id=2))
