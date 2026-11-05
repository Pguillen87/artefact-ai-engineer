from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from artefact.commercial import extract_installment_count
from artefact.context import explicit_order_id
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.intent import requested_policy_topics
from artefact.rendering import render_results
from artefact.services import Catalog
from test_data import ROWS, write_dataset


class _FinalOnlyProvider:
    def __init__(self):
        self.calls = []

    def request(self, messages, final=False):
        self.calls.append(final)
        if not final:
            raise AssertionError('the deterministic request route should not need interpretation')
        return {'output': []}


def _snapshot(changes=None, extra=None):
    temporary = TemporaryDirectory()
    root = Path(temporary.name)
    write_dataset(root, changes, extra)
    return temporary, load_dataset(root)


def _conversation(text, changes=None, extra=None, chat_id=1):
    temporary, snapshot = _snapshot(changes, extra)
    provider = _FinalOnlyProvider()
    agent = ConversationAgent(snapshot, provider)
    answer = agent.answer(text, chat_id=chat_id)
    return temporary, snapshot, provider, agent, answer


class QA12BehaviorRegressionTests(unittest.TestCase):
    def test_compact_installment_count_keeps_product_quote_on_catalog_path(self):
        text = ('Posso parcelar o Ukulele Shelby SU-21S de R$ 159,90 em12x no cartão? '
                'Qual é o máximo permitido?')
        self.assertEqual(extract_installment_count(text), 12)
        temporary, _, provider, agent, answer = _conversation(
            text,
            {'products': {'name': 'Ukulele Shelby SU-21S', 'price_brl': '159.90'},
             'promotions': {'is_active': '0'}},
        )
        try:
            self.assertIn('R$ 159,90', answer)
            self.assertIn('12x', answer)
            self.assertIn('3x R$ 53,30', answer)
            self.assertEqual(agent.last_trace['selected_tools'], ['search_products'])
            self.assertEqual(provider.calls, [True])
        finally:
            temporary.cleanup()


    def test_product_price_and_pix_question_returns_both_catalog_facts(self):
        temporary, _, provider, agent, answer = _conversation(
            'Qual é o preço do Korg Synth e posso aplicar mais 5% no Pix?',
            {'products': {'price_brl': '2399.00'},
             'promotions': {'discount_percent': '18', 'description': 'sale'}},
        )
        try:
            self.assertIn('R$ 1.967,18', answer)
            self.assertIn('18%', answer)
            self.assertIn('não acumula', answer.lower())
            self.assertEqual(agent.last_trace['selected_tools'], ['search_products'])
            self.assertEqual(provider.calls, [True])
        finally:
            temporary.cleanup()


    def test_pix_on_named_promotion_includes_the_promotion_price(self):
        temporary, _, _, agent, answer = _conversation(
            'Posso aplicar mais 5% de Pix ao Korg Synth que já está em promoção?',
            {'products': {'price_brl': '2399.00'},
             'promotions': {'discount_percent': '18', 'description': 'sale'}},
        )
        try:
            self.assertIn('R$ 2.399,00', answer)
            self.assertIn('18%', answer)
            self.assertIn('R$ 1.967,18', answer)
            self.assertIn('não acumula', answer.lower())
            self.assertEqual(agent.last_trace['selected_tools'], ['search_products'])
        finally:
            temporary.cleanup()


    def test_accessory_and_weather_scope_stays_out_of_weather_facts(self):
        temporary, _, _, agent, answer = _conversation(
            'Vocês vendem cordas, cases e pedais? E qual é a previsão do tempo para hoje?',
            {'products': {'name': 'Violão de 7 Cordas', 'price_brl': '500.00'}},
        )
        try:
            self.assertIn('não comercializa acessórios', answer.lower())
            self.assertIn('previsão do tempo', answer.lower())
            self.assertFalse(any(token in answer.lower()
                                 for token in ('chuva', 'temperatura', 'graus celsius')))
            self.assertEqual(agent.last_trace['selected_tools'], ['policy'])
            self.assertIn('scope', requested_policy_topics('Vocês vendem cordas, cases e pedais?'))
            self.assertNotIn('scope', requested_policy_topics('Vocês têm um violão de 7 cordas?'))
            self.assertNotIn('scope', requested_policy_topics('Vocês têm um violão de sete cordas?'))
            self.assertNotIn('scope', requested_policy_topics('Vocês têm violão com cordas de nylon?'))
        finally:
            temporary.cleanup()


    def test_scope_policy_mentions_only_the_requested_boundary(self):
        temporary, _, _, _, accessory_answer = _conversation('Vocês vendem palhetas?')
        try:
            self.assertIn('não comercializa acessórios', accessory_answer.lower())
            self.assertNotIn('previsão do tempo', accessory_answer.lower())
        finally:
            temporary.cleanup()
        temporary, _, _, _, weather_answer = _conversation('Qual é a previsão do tempo?')
        try:
            self.assertIn('previsão do tempo', weather_answer.lower())
            self.assertNotIn('acessórios', weather_answer.lower())
        finally:
            temporary.cleanup()


    def test_heavy_piano_delivery_asks_store_for_special_quote_without_standard_rate(self):
        temporary, _, _, agent, answer = _conversation(
            'Entregam um piano digital em Campo Grande? Pode confirmar a taxa e o prazo?')
        try:
            self.assertIn('cotação especial com a loja', answer.lower())
            self.assertIn('não estão confirmados', answer.lower())
            self.assertNotIn('R$ 35', answer)
            self.assertNotIn('1 a 3 dias', answer)
            self.assertEqual(agent.last_trace['stage'], 'shipping_quote')
        finally:
            temporary.cleanup()


    def test_product_detail_reports_coming_soon_separately_from_stock(self):
        temporary, snapshot = _snapshot({'products': {'status': 'coming_soon', 'stock_quantity': '2'}})
        try:
            detail = Catalog(snapshot).product(1)
            answer = render_results([{'consulta': 'product_detail', 'resultado': detail}])
            self.assertIn('em breve', answer.lower())
            self.assertIn('sem pronta entrega', answer.lower())
            self.assertIn('estoque registrado de 2 unidade(s)', answer)
            self.assertIn('não há previsão de chegada', answer.lower())
        finally:
            temporary.cleanup()


    def test_order_reference_accepts_hash_and_ordinal_without_capturing_installments(self):
        cases = {
            'Consulte o pedido #3.': 3,
            'Consulte o pedido nº 3.': 3,
            'Consulte o pedido número #3.': 3,
            'Consulte o pedido 3 em 12x.': 3,
            'Consulte o pedido #3 e o pedido #4.': None,
            'Consulte o pedido 12x.': None,
        }
        self.assertEqual({text: explicit_order_id(text) for text in cases}, cases)


    def test_contact_followup_returns_only_the_requested_order(self):
        temporary, snapshot = _snapshot(
            {'orders': {'order_id': '3', 'status': 'delivered', 'total_brl': '3450.00'},
             'order_items': {'order_id': '3'}},
            {'orders': ['4', '1', '2026-01-02', 'cancelled', '900.00', 'pix', '', '', '']},
        )
        try:
            agent = ConversationAgent(snapshot, _FinalOnlyProvider())
            first = agent.answer('Pode consultar o pedido #3?', chat_id='order-chat')
            self.assertIn('e-mail ou telefone', first)
            second = agent.answer(
                'Meu contato cadastrado é fake@example.invalid. Consulte somente pedido #3.',
                chat_id='order-chat')
            self.assertIn('Pedido 3:', second)
            self.assertNotIn('Pedido 4:', second)
            self.assertIn('R$ 3.450,00', second)
            self.assertNotIn('fake@example.invalid', repr(agent.last_trace))
        finally:
            temporary.cleanup()
