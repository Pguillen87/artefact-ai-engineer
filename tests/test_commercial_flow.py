import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from test_data import write_dataset
from artefact.data import load_dataset
from artefact.conversation import ConversationAgent


def _call(name, arguments, call_id):
    return {
        'type': 'function_call',
        'name': name,
        'arguments': json.dumps(arguments),
        'call_id': call_id,
    }


class SyntheticProvider:
    def __init__(self, *calls):
        self.calls = calls
        self.request_count = 0

    def request(self, messages):
        self.request_count += 1
        return {'output': list(self.calls)}


class CommercialConversationFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _answer(self, question, changes=None, extra=None, *calls, expected_calls=None):
        write_dataset(self.root, changes, extra)
        snapshot = load_dataset(self.root)
        provider = SyntheticProvider(*calls)
        answer = ConversationAgent(snapshot, provider).answer(question, chat_id=10)
        self.assertLessEqual(provider.request_count, 2)
        if expected_calls is not None:
            self.assertEqual(provider.request_count, expected_calls)
        return answer

    def test_product_search_renders_original_and_calculated_promotional_price(self):
        answer = self._answer(
            'Qual é o preço e a promoção do Korg Synth?',
            {'products': {'price_brl': '359.00'}},
            None,
            _call('search_products', {'text': 'Korg Synth'}, 'a'),
        )
        self.assertIn('R$ 359,00', answer)
        self.assertIn('10%', answer)
        self.assertIn('R$ 323,10', answer)
        self.assertIn('snapshot', answer.lower())

    def test_pix_price_is_calculated_only_without_a_promotion(self):
        answer = self._answer(
            'Quanto fica o Korg Synth no Pix?',
            {'products': {'price_brl': '359.00'}, 'promotions': {'is_active': '0'}},
            None,
            _call('search_products', {'text': 'Korg Synth'}, 'a'),
        )
        self.assertIn('R$ 359,00', answer)
        self.assertIn('5%', answer)
        self.assertIn('R$ 341,05', answer)

    def test_pix_is_not_added_to_the_promotional_price(self):
        answer = self._answer(
            'Posso aplicar mais 5% de Pix ao Korg Synth que já está em promoção?',
            {'products': {'price_brl': '359.00'}},
            None,
            _call('search_products', {'text': 'Korg Synth'}, 'a'),
            _call('policy', {'topic': 'pix'}, 'b'),
        )
        self.assertIn('R$ 323,10', answer)
        self.assertIn('não acumula', answer.lower())
        self.assertNotIn('R$ 306,95', answer)

    def test_direct_installment_tool_returns_exact_allowed_or_rejected_quotes(self):
        cases = [
            ('150.00', 2, True, '2x R$ 75,00'),
            ('90.00', 2, False, '1x R$ 90,00'),
            ('480.00', 6, True, '6x R$ 80,00'),
            ('479.99', 6, False, '5x'),
            ('1200.00', 12, True, '12x R$ 100,00'),
            ('1199.88', 12, False, '11x'),
        ]
        for amount, count, allowed, expected in cases:
            with self.subTest(amount=amount, count=count):
                text = f'Posso parcelar R$ {amount.replace(".", ",")} em {count} vezes sem juros?'
                answer = self._answer(
                    text,
                    None,
                    None,
                    _call('installment_quote', {'amount_brl': amount, 'installments': count}, 'a'),
                )
                if allowed:
                    self.assertIn('permitido', answer.lower())
                    self.assertIn(expected, answer)
                else:
                    self.assertIn('não é permitido', answer.lower())
                    self.assertIn(expected, answer)

    def test_product_installments_and_shipping_share_one_catalog_snapshot(self):
        answer = self._answer(
            'Qual o preço do Korg Synth, em 2x, e qual a regra de frete para Campo Grande?',
            {'products': {'price_brl': '359.00'}},
            None,
            _call('search_products', {'text': 'Korg Synth', 'installments': 2}, 'a'),
            _call('policy', {'topic': 'shipping'}, 'b'),
        )
        self.assertIn('R$ 323,10', answer)
        self.assertIn('2x R$ 161,55', answer)
        self.assertIn('frete', answer.lower())
        self.assertIn('R$ 35', answer)

    def test_conflicting_promotions_are_not_quoted(self):
        answer = self._answer(
            'Qual o preço promocional do Korg Synth?',
            {'products': {'price_brl': '359.00'}},
            {'promotions': ['2', '1', '20', 'second synthetic sale', '1']},
            _call('search_products', {'text': 'Korg Synth'}, 'a'),
        )
        self.assertIn('promoções conflitantes', answer.lower())
        self.assertNotIn('R$ 323,10', answer)
        self.assertNotIn('R$ 287,20', answer)

    def test_unavailable_product_is_not_offered_with_a_calculated_price(self):
        answer = self._answer(
            'Posso comprar o Korg Synth em promoção?',
            {'products': {'status': 'coming_soon', 'stock_quantity': '2', 'price_brl': '359.00'}},
            None,
            _call('search_products', {'text': 'Korg Synth'}, 'a'),
        )
        self.assertIn('indisponível', answer.lower())
        self.assertNotIn('R$ 323,10', answer)
        self.assertNotIn('R$ 341,05', answer)

    def test_missing_product_price_is_not_synthesized(self):
        write_dataset(self.root)
        snapshot = load_dataset(self.root)
        products = tuple({**dict(row), 'price_brl': None} for row in snapshot.tables['products'])
        tables = {**snapshot.tables, 'products': products}
        provider = SyntheticProvider(_call('search_products', {'text': 'Korg'}, 'a'))
        answer = ConversationAgent(replace(snapshot, tables=tables), provider).answer('Qual preço do Korg?', chat_id=10)
        self.assertIn('preço', answer.lower())
        self.assertIn('não está disponível', answer.lower())
        self.assertNotIn('R$ 1.127,50', answer)

    def test_future_or_historical_amount_is_not_recomputed_as_product_financing(self):
        for text in (
            'Quanto fica o pedido 20 em 2 vezes?',
            'O total histórico de R$ 1.400 do pedido 20 pode ser recalculado em 2x?',
            'Posso parcelar R$ 150 ou R$ 90 em 2x?',
        ):
            with self.subTest(text=text):
                answer = self._answer(
                    text,
                    None,
                    None,
                    _call('installment_quote', {'amount_brl': '150.00', 'installments': 2}, 'a'),
                )
                self.assertNotIn('2x R$ 75,00', answer)
                self.assertIn('informe', answer.lower())

        answer = self._answer(
            'Reparcelar o pedido 20 em 2x?', None, None,
            _call('search_products', {'text': 'Korg', 'installments': 2}, 'a'),
        )
        self.assertNotIn('parcelamento permitido', answer.lower())
        self.assertNotIn('2x R$ 75,00', answer)

    def test_model_cannot_replace_user_amount_with_a_different_value(self):
        answer = self._answer(
            'Posso parcelar R$ 150,00 em 2x?', None, None,
            _call('installment_quote', {'amount_brl': '9000.00', 'installments': 2}, 'a'),
        )
        self.assertIn('R$ 150,00', answer)
        self.assertIn('2x R$ 75,00', answer)
        self.assertNotIn('R$ 9.000,00', answer)
        self.assertNotIn('2x R$ 4.500,00', answer)

    def test_warranty_answer_mentions_certificate_without_promising_two_years(self):
        answer = self._answer(
            'Este modelo tem dois anos de garantia do fabricante?', None, None,
            _call('policy', {'topic': 'warranty'}, 'a'),
        )
        self.assertIn('certificado', answer.lower())
        self.assertIn('dois anos', answer.lower())
        self.assertIn('garantia legal', answer.lower())
        self.assertIn('direitos legais', answer.lower())

    def test_exchange_answer_preserves_guarantee_path_after_commercial_window(self):
        answer = self._answer(
            'O produto apresentou defeito após 30 dias. E agora?', None, None,
            _call('policy', {'topic': 'exchange'}, 'a'),
        )
        self.assertIn('fabricante', answer.lower())
        self.assertIn('garantia legal de 90 dias', answer.lower())
        self.assertNotIn('troca imediata', answer.lower())

    def test_exchange_policy_covers_preference_custom_final_sale_and_misuse_cases(self):
        cases = [
            ('Comprei na loja há 6 dias. Posso trocar por outra cor?', 'exchange',
             ('preferência', 'disponibilidade', 'diferença de preço')),
            ('Meu instrumento personalizado sob encomenda pode ser trocado só porque não gostei?', 'exchange',
             ('personalizado', 'preferência', 'defeito')),
            ('Comprei em venda final, mas ele veio com defeito. Tenho algum direito?', 'exchange',
             ('venda final', 'defeito', 'garantia')),
            ('Meu produto caiu no chão e quebrou. Isso entra na garantia?', 'warranty',
             ('mau uso', 'avaliação')),
            ('Dano por mau uso tem cobertura?', 'warranty',
             ('mau uso', 'avaliação')),
        ]
        for question, topic, expected_terms in cases:
            with self.subTest(question=question):
                answer = self._answer(question, None, None,
                                      _call('policy', {'topic': topic}, 'a'))
                for term in expected_terms:
                    self.assertIn(term, answer.lower())

    def test_regret_policy_explains_request_window_without_claiming_a_return(self):
        answer = self._answer(
            'Recebi minha compra online há 6 dias e me arrependi. Posso devolver?', None, None,
            _call('policy', {'topic': 'regret'}, 'a'),
        )
        self.assertIn('7 dias corridos', answer.lower())
        self.assertIn('recebimento', answer.lower())
        self.assertIn('solicitar', answer.lower())
        self.assertNotIn('devolução iniciada', answer.lower())
        self.assertNotIn('devolução aprovada', answer.lower())

    def test_scope_policy_handles_accessories_and_external_weather_requests(self):
        cases = [
            ('Vocês vendem palhetas, cabos e amplificadores?',
             ('instrumentos', 'não comercializa acessórios')),
            ('Qual a previsão do tempo para amanhã?',
             ('fora do escopo', 'previsão do tempo')),
        ]
        for question, expected_terms in cases:
            with self.subTest(question=question):
                answer = self._answer(question, None, None,
                                      _call('policy', {'topic': 'scope'}, 'a'))
                for term in expected_terms:
                    self.assertIn(term, answer.lower())

    def test_payment_mix_policy_handles_exact_and_above_threshold(self):
        cases = [
            'Posso dividir R$ 2.000 entre Pix e cartão?',
            'Posso dividir R$ 2.000,01 entre Pix e cartão?',
        ]
        for question in cases:
            with self.subTest(question=question):
                answer = self._answer(question, None, None,
                                      _call('policy', {'topic': 'payment_mix'}, 'a'))
                self.assertIn('acima de R$ 2.000,00', answer)
                self.assertIn('exatamente R$ 2.000,00 não se qualifica', answer)
                self.assertIn('confirmada com a loja', answer)

    def test_shipping_policy_covers_forecast_heavy_items_and_exact_date_limits(self):
        cases = [
            ('Qual o prazo normal de entrega em Campo Grande?',
             ('1 a 3 dias úteis', 'previsão')),
            ('Vou comprar um piano digital pesado; o frete é o mesmo?',
             ('cotação especial', 'confirmar com a loja')),
            ('Você garante que meu pedido chega amanhã?',
             ('sem garantia de data exata', 'previsão')),
        ]
        for question, expected_terms in cases:
            with self.subTest(question=question):
                answer = self._answer(question, None, None,
                                      _call('policy', {'topic': 'shipping'}, 'a'))
                for term in expected_terms:
                    self.assertIn(term, answer.lower())


if __name__ == '__main__':
    unittest.main()
