import json
import csv
import sqlite3
import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from test_data import write_dataset
from artefact.data import load_dataset
from artefact.services import Catalog
from artefact.conversation import ConversationAgent
from artefact.agent import TelegramAdapter
from artefact.telegram import TelegramClient, TelegramError
from artefact.intent import validate_catalog_intent


def tool(name, args, ident='1'):
    return {'type': 'function_call', 'name': name, 'arguments': json.dumps(args), 'call_id': ident}


class ArchitectureContracts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); write_dataset(root)
        self.snapshot = load_dataset(root)

    def test_search_requires_real_criteria_and_accepts_independent_filters(self):
        catalog = Catalog(self.snapshot)
        for args in ({}, {'text': ''}, {'text': '  '}, {'category': ' '}):
            with self.subTest(args=args), self.assertRaises(ValueError): catalog.search(**args)
        self.assertEqual(len(catalog.search(category='Teclados e Pianos')), 1)
        self.assertEqual(len(catalog.search(max_price=Decimal('1200'))), 1)
        self.assertEqual(len(catalog.search(available_only=True)), 1)
        self.assertEqual(len(catalog.search(text='Korg', category='Teclados e Pianos', max_price=Decimal('1200'))), 1)
        self.assertEqual(catalog.search(text='produto inexistente'), ())

    def test_write_must_be_isolated_before_execution(self):
        submit = Mock(return_value=7)
        provider = Mock(); provider.request.return_value = {'output': [
            tool('request_technical_review', {'product_id':1,'field':'specs.keys','reason':'inconsistencia_catalogo'}),
            tool('unknown', {}, '2')]}
        answer = ConversationAgent(self.snapshot, provider, review_submit=submit).answer(
            'Registre revisão técnica das teclas do produto 1', chat_id=1)
        submit.assert_not_called(); self.assertNotIn('registrada', answer.lower())

    def test_confirmed_write_is_not_reported_as_generic_failure_if_render_fails(self):
        submit=Mock(return_value=7)
        provider=Mock(); provider.request.return_value={'output':[tool('request_technical_review',
            {'product_id':1,'field':'specs.keys','reason':'inconsistencia_catalogo'})]}
        with patch('artefact.conversation.render_results',side_effect=ValueError('synthetic failure')):
            with self.assertLogs('artefact.operations',level='INFO') as captured:
                answer=ConversationAgent(self.snapshot,provider,review_submit=submit).answer('Registre revisão das teclas do produto 1',chat_id=1)
        submit.assert_called_once(); self.assertIn('registrada',answer.lower())
        self.assertIn('"side_effect": "committed"',str(captured.output))

    def test_wrong_product_proposal_is_not_authorized(self):
        provider = Mock(); provider.request.return_value = {'output': [tool('search_products', {'text':'Korg'})]}
        answer = ConversationAgent(self.snapshot, provider).answer('Vocês têm um teclado Yamaha?', chat_id=1)
        self.assertNotIn('Korg Synth', answer)

    def test_partial_model_name_is_not_treated_as_exact_product(self):
        provider=Mock(); provider.request.return_value={'output':[tool('search_products',{'category':'Teclados e Pianos'})]}
        answer=ConversationAgent(self.snapshot,provider).answer('Procuro Korg Synthesizer',chat_id=1)
        self.assertNotIn('Korg Synth:',answer)
        self.assertIn('esclareça',answer.lower())
        provider.request.return_value={'output':[tool('search_products',{'text':'Korg'})]}
        answer=ConversationAgent(self.snapshot,provider).answer('Procuro Korg Synthesizer',chat_id=1)
        self.assertNotIn('Korg Synth:',answer)
        self.assertIn('esclareça',answer.lower())

    def test_empty_search_does_not_return_catalog(self):
        provider=Mock(); provider.request.return_value={'output':[tool('search_products',{})]}
        answer=ConversationAgent(self.snapshot,provider).answer('Quero consultar um produto',chat_id=1)
        self.assertIn('esclareça',answer.lower()); self.assertNotIn('Korg Synth:',answer)

    def test_ambiguous_category_requires_its_qualifier(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);write_dataset(root,extra={'categories':['2','Instrumentos de Sopro (Madeiras)','']})
            with (root/'categories.csv').open('a',encoding='utf-8',newline='') as stream:
                csv.writer(stream).writerow(['3','Instrumentos de Sopro (Metais)',''])
            snapshot=load_dataset(root)
            wood={'category':'Instrumentos de Sopro (Madeiras)'}
            brass={'category':'Instrumentos de Sopro (Metais)'}
            self.assertFalse(validate_catalog_intent('search_products',wood,'Quero instrumentos de sopro metais',snapshot))
            self.assertTrue(validate_catalog_intent('search_products',brass,'Quero instrumentos de sopro metais',snapshot))
            self.assertFalse(validate_catalog_intent('search_products',wood,'Quero instrumentos de sopro',snapshot))

    def test_followup_uses_typed_total_and_new_installment_count(self):
        provider = Mock(); provider.request.side_effect = [
            {'output':[tool('installment_quote', {'amount_brl':'900','installments':2})]},
            {'output':[tool('installment_quote', {'amount_brl':'900','installments':3})]}]
        agent = ConversationAgent(self.snapshot, provider)
        self.assertIn('2x R$ 450,00', agent.answer('R$ 900 em 2x?', chat_id=1))
        self.assertIn('3x R$ 300,00', agent.answer('Esse valor em 3x?', chat_id=1))

    def test_tool_price_category_id_and_ambiguity_are_grounded(self):
        cases = [
            ('Quero teclado até R$ 500', 'search_products', {'category':'Teclados e Pianos','max_price':'1200'}),
            ('Quero teclado até R$ 500', 'search_products', {'category':'Teclados e Pianos'}),
            ('Quero teclado até R$ 1200', 'search_products', {'max_price':'1200'}),
            ('Quais teclados disponíveis?', 'search_products', {'category':'Teclados e Pianos'}),
            ('Teclados entre 800 e 2000', 'search_products', {'category':'Teclados e Pianos','max_price':'800'}),
            ('Qual o Korg de R$ 900 em 3x?', 'search_products', {'text':'Korg','max_price':'900'}),
            ('Qual promoção do produto 9?', 'promotion', {'product_id':1}),
            ('Qual promoção do produto 99?', 'promotion', {'product_id':99}),
            ('Quero Korg ou Yamaha', 'search_products', {'text':'Korg'}),
            ('Não quero Korg', 'search_products', {'text':'Korg'}),
            ('Não quero Korg Synth', 'search_products', {'text':'Synth'}),
        ]
        for text, name, args in cases:
            with self.subTest(text=text):
                provider=Mock(); provider.request.return_value={'output':[tool(name,args)]}
                answer=ConversationAgent(self.snapshot,provider).answer(text,chat_id=1)
                self.assertIn('esclareça',answer.lower())
                self.assertNotIn('Korg Synth:',answer)
        provider=Mock(); provider.request.return_value={'output':[tool('search_products',{'category':'Teclados e Pianos','max_price':'1200'})]}
        answer=ConversationAgent(self.snapshot,provider).answer('Quero teclado até R$ 1.200',chat_id=1)
        self.assertIn('Korg Synth:',answer)
        provider.request.return_value={'output':[tool('recommend_products',{})]}
        self.assertIn('Korg Synth:',ConversationAgent(self.snapshot,provider).answer('Recomende opções',chat_id=1))
        provider.request.return_value={'output':[tool('search_products',{'available_only':True})]}
        self.assertIn('Korg Synth:',ConversationAgent(self.snapshot,provider).answer('Quais produtos disponíveis?',chat_id=1))
        provider.request.return_value={'output':[tool('search_products',{'category':'Teclados e Pianos','min_price':'800','max_price':'2000'})]}
        self.assertIn('Korg Synth:',ConversationAgent(self.snapshot,provider).answer('Teclados entre 800 e 2000',chat_id=1))
        provider.request.return_value={'output':[tool('search_products',{'category':'Teclados e Pianos','max_price':'1200','installments':3})]}
        self.assertIn('3x R$ 338,25',ConversationAgent(self.snapshot,provider).answer('Quero teclado até R$ 1.200, em 3x',chat_id=1))
        provider.request.return_value={'output':[tool('search_products',{'category':'Teclados e Pianos','max_price':'1200'})]}
        self.assertIn('Korg Synth',ConversationAgent(self.snapshot,provider).answer('Quero um sintetizador até R$ 1.200',chat_id=1))
        provider.request.return_value={'output':[tool('promotion',{'product_id':1})]}
        self.assertIn('Korg Synth',ConversationAgent(self.snapshot,provider).answer('Qual promoção do Korg Synth?',chat_id=1))
        provider.request.return_value={'output':[tool('search_products',{'text':'Korg'})]}
        self.assertIn('Korg Synth',ConversationAgent(self.snapshot,provider).answer('Korg tem garantia até 2 anos?',chat_id=1))
        self.assertIn('Korg Synth',ConversationAgent(self.snapshot,provider).answer('O Korg tem 61 ou 88 teclas?',chat_id=1))

    def test_standalone_amount_context_and_topic_change(self):
        provider=Mock(); provider.request.side_effect=[
            {'output':[]}, {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]},
            {'output':[]}, {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('R$ 900.',chat_id=1)
        self.assertIn('3x R$ 300,00',agent.answer('E em 3x?',chat_id=1))
        agent.answer('Agora quero saber sobre teclados.',chat_id=1)
        self.assertIn('informe um único valor',agent.answer('E em 3x?',chat_id=1))

    def test_product_question_without_product_clears_old_amount(self):
        provider=Mock(); provider.request.side_effect=[{'output':[]},
            {'output':[tool('policy',{'topic':'pix'})]},
            {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('R$ 900.',chat_id=1)
        agent.answer('Qual promoção?',chat_id=1)
        self.assertIn('informe um único valor',agent.answer('E em 3x?',chat_id=1))

    def test_unmarked_second_amount_does_not_become_unambiguous_context(self):
        provider=Mock(); provider.request.side_effect=[{'output':[]},
            {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('Estou comparando R$ 900 e 1200.',chat_id=1)
        self.assertIn('informe um único valor',agent.answer('E em 3x?',chat_id=1))
        provider=Mock(); provider.request.return_value={'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]}
        self.assertIn('informe um único valor',ConversationAgent(self.snapshot,provider).answer('R$ 900 e 1200 em 3x?',chat_id=1))

    def test_product_context_and_budget_are_distinct(self):
        provider=Mock(); provider.request.side_effect=[
            {'output':[tool('search_products',{'text':'Korg'})]},
            {'output':[tool('search_products',{'text':'Korg'})]},
            {'output':[tool('search_products',{'text':'Korg'})]},
            {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]},
        ]
        agent=ConversationAgent(self.snapshot,provider)
        self.assertIn('Korg Synth:',agent.answer('Procuro Korg',chat_id=1))
        self.assertIn('Korg Synth:',agent.answer('E o preço dele?',chat_id=1))
        self.assertIn('Korg Synth:',agent.answer('Meu orçamento é R$ 900, quero Korg',chat_id=1))
        self.assertIn('informe um único valor',agent.answer('Esse valor em 3x?',chat_id=1))

    def test_explicit_product_is_retained_even_without_initial_tool(self):
        provider=Mock(); provider.request.side_effect=[{'output':[]}, {'output':[tool('search_products',{'text':'Korg Synth'})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('Quero Korg Synth',chat_id=1)
        self.assertIn('Korg Synth:',agent.answer('Qual preço?',chat_id=1))

    def test_explicit_product_and_followup_do_not_leak_similar_model(self):
        from test_data import ROWS
        with TemporaryDirectory() as directory:
            root=Path(directory); other=list(ROWS['products']); other[0]='2'; other[2]='Korg Synth Pro'
            write_dataset(root,extra={'products':other}); provider=Mock()
            provider.request.side_effect=[{'output':[tool('search_products',{'text':'Korg'})]},
                                          {'output':[tool('search_products',{'text':'Korg Synth'})]}]
            agent=ConversationAgent(load_dataset(root),provider)
            first=agent.answer('Qual preço do Korg Synth?',chat_id=1)
            second=agent.answer('Qual preço?',chat_id=1)
            self.assertIn('Korg Synth:',first); self.assertNotIn('Korg Synth Pro:',first)
            self.assertIn('Korg Synth:',second); self.assertNotIn('Korg Synth Pro:',second)

    def test_telegram_rejects_malformed_updates(self):
        client=TelegramClient('synthetic',transport=Mock(return_value={'ok':True,'result':[None]}))
        with self.assertRaises(TelegramError): client.get_updates(0)

    def test_product_name_count_does_not_override_current_installments(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); write_dataset(root,{'products':{'name':'Yamaha Bass 3X'}})
            provider=Mock(); provider.request.side_effect=[
                {'output':[tool('search_products',{'text':'Yamaha Bass 3X'})]},
                {'output':[tool('search_products',{'text':'Yamaha Bass 3X','installments':6})]}]
            agent=ConversationAgent(load_dataset(root),provider)
            self.assertIn('Yamaha Bass 3X',agent.answer('Preço Yamaha Bass 3X?',chat_id=1))
            self.assertIn('6x',agent.answer('Produto em 6x?',chat_id=1))

    def test_product_followup_in_installments_uses_published_price(self):
        provider=Mock(); provider.request.side_effect=[
            {'output':[tool('search_products',{'text':'Korg Synth'})]},
            {'output':[tool('search_products',{'text':'Korg Synth','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        self.assertIn('Korg Synth',agent.answer('Preço Korg Synth?',chat_id=1))
        self.assertIn('3x R$ 338,25',agent.answer('E em 3x?',chat_id=1))

    def test_per_installment_reference_does_not_become_total(self):
        provider=Mock(); provider.request.side_effect=[
            {'output':[tool('installment_quote',{'amount_brl':'150','installments':3})]},
            {'output':[tool('installment_quote',{'amount_brl':'450','installments':2})]}]
        agent=ConversationAgent(self.snapshot,provider)
        self.assertIn('3x R$ 150,00',agent.answer('3x de R$ 150?',chat_id=1))
        self.assertIn('informe um único valor',agent.answer('Esse valor em 2x?',chat_id=1))
        provider=Mock(); provider.request.side_effect=[{'output':[]},
            {'output':[tool('installment_quote',{'amount_brl':'150','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('Cada parcela é de R$ 150.',chat_id=1)
        self.assertIn('informe um único valor',agent.answer('E em 3x?',chat_id=1))

    def test_logs_classify_failures_without_sensitive_text(self):
        secret='sensitive-token-customer-content'
        provider=Mock(); provider.request.side_effect=TimeoutError(secret)
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            answer=ConversationAgent(self.snapshot,provider).answer('consulta '+secret,chat_id=1)
        self.assertIn('não consegui',answer)
        self.assertIn('"category": "provider"',captured.output[0]); self.assertNotIn(secret,str(captured.output))
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            ConversationAgent(self.snapshot,Mock(),snapshot_loader=Mock(side_effect=sqlite3.OperationalError(secret))).answer('oi?',chat_id=1)
        self.assertIn('"category": "persistence"',captured.output[0]); self.assertNotIn(secret,str(captured.output))
        provider=Mock(); provider.request.return_value={'output':[tool('request_technical_review',{'product_id':1,'field':'specs.keys','reason':'inconsistencia_catalogo'})]}
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            answer=ConversationAgent(self.snapshot,provider,review_submit=Mock(side_effect=OSError(secret))).answer('Registre revisão das teclas do produto 1',chat_id=1)
        self.assertIn('não foi possível confirmar',answer.lower()); self.assertNotIn(secret,str(captured.output))
        events=[json.loads(line.split(':',2)[2]) for line in captured.output]
        self.assertEqual(len({e['operation_id'] for e in events}),1)
        uncertain=[e for e in events if e['result']=='uncertain']
        self.assertTrue(uncertain); self.assertTrue(all(e['side_effect']=='unknown' for e in uncertain))
        sender=Mock(side_effect=OSError(secret)); agent=Mock(); agent.answer.return_value='ok'
        state=Mock(); state.claim_update.return_value=True
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            with self.assertRaises(OSError):
                TelegramAdapter(agent,sender,state=state).handle({'update_id':1,'message':{'chat':{'id':1,'type':'private'},'text':secret}})
        self.assertIn('"category": "transport"',captured.output[0]); self.assertNotIn(secret,str(captured.output))
        state.claim_update.side_effect=sqlite3.OperationalError(secret)
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            with self.assertRaises(sqlite3.OperationalError):
                TelegramAdapter(agent,sender,state=state).handle({'update_id':2,'message':{'chat':{'id':1,'type':'private'},'text':secret}})
        self.assertIn('"category": "persistence"',str(captured.output)); self.assertNotIn(secret,str(captured.output))
        for name,args,expected in [('search_products',{'text':'Korg'},'validation'),('unknown',{},'tool')]:
            with self.subTest(category=expected):
                provider=Mock(); provider.request.return_value={'output':[tool(name,args)]}
                with self.assertLogs('artefact.operations',level='INFO') as captured:
                    ConversationAgent(self.snapshot,provider).answer('Yamaha '+secret,chat_id=1)
                self.assertIn('"category": "'+expected+'"',str(captured.output))
                self.assertNotIn(secret,str(captured.output))

    def test_telegram_and_conversation_events_share_correlation(self):
        provider=Mock(); provider.request.return_value={'output':[tool('policy',{'topic':'pix'})]}
        state=Mock(); state.claim_update.return_value=True
        adapter=TelegramAdapter(ConversationAgent(self.snapshot,provider),Mock(),state=state)
        with self.assertLogs('artefact.operations',level='INFO') as captured:
            adapter.handle({'update_id':1,'message':{'chat':{'id':1,'type':'private'},'text':'Qual política de Pix?'}})
        events=[json.loads(line.split(':',2)[2]) for line in captured.output]
        self.assertTrue({'conversation','telegram'}.issubset({e['component'] for e in events}))
        self.assertEqual(len({e['operation_id'] for e in events}),1)

    def test_fictional_order_followup_uses_unique_identification_and_isolated_chat(self):
        provider=Mock(); provider.request.side_effect=[
            {'output':[]}, {'output':[tool('orders',{'full_name':'Synthetic Customer'})]},
            {'output':[tool('orders',{'full_name':'Synthetic Customer'})]},
            {'output':[tool('orders',{'full_name':'Synthetic Customer'})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('Meu nome é Synthetic Customer.',chat_id=1)
        self.assertIn('Pedido 1:',agent.answer('Quais são meus pedidos?',chat_id=1))
        self.assertNotIn('Pedido 1:',agent.answer('Quais são meus pedidos?',chat_id=2))
        self.assertNotIn('Pedido 1:',agent.answer('Meu nome é Unknown User e quero meus pedidos',chat_id=1))
        provider=Mock(); provider.request.return_value={'output':[tool('orders',{'full_name':'Synthetic Customer'})]}
        self.assertNotIn('Pedido 1:',ConversationAgent(self.snapshot,provider).answer('Meu nome é Synthetic Customer Junior, quero meus pedidos',chat_id=1))
        self.assertNotIn('Pedido 1:',ConversationAgent(self.snapshot,provider).answer('Não sou Synthetic Customer, quero meus pedidos',chat_id=1))

    def test_order_topic_invalidates_previous_commercial_amount(self):
        provider=Mock(); provider.request.side_effect=[{'output':[]},
            {'output':[tool('orders',{'full_name':'Synthetic Customer'})]},
            {'output':[tool('installment_quote',{'amount_brl':'900','installments':3})]}]
        agent=ConversationAgent(self.snapshot,provider)
        agent.answer('Meu nome é Synthetic Customer, considero R$ 900.',chat_id=1)
        self.assertNotIn('Synthetic Customer',repr(agent.context))
        self.assertIn('Pedido 1:',agent.answer('Quais são meus pedidos?',chat_id=1))
        self.assertIn('informe um único valor',agent.answer('E em 3x?',chat_id=1))

    def test_optional_telegram_allowlist_is_validated_and_enforced(self):
        from artefact.runtime import read_config
        env={'ENABLE_REAL_LLM':'1','OPENAI_API_KEY':'dummy','TELEGRAM_BOT_TOKEN':'dummy',
             'ARTEFACT_DATA_DIR':'data','OPENAI_MODEL':'test','OPENAI_BUDGET_USD':'1',
             'OPENAI_MAX_CALLS':'1','OPENAI_INPUT_USD_PER_MILLION':'1',
             'OPENAI_OUTPUT_USD_PER_MILLION':'1','ARTEFACT_STATE_PATH':'state.db'}
        self.assertIsNone(read_config(env).allowed_chat_id)
        env['TELEGRAM_ALLOWED_CHAT_ID']='123'; self.assertEqual(read_config(env).allowed_chat_id,123)
        env['TELEGRAM_ALLOWED_CHAT_ID']='-1'
        with self.assertRaises(ValueError): read_config(env)
        agent=Mock(); send=Mock(); adapter=TelegramAdapter(agent,send,allowed_chat_id=123)
        adapter.handle({'update_id':1,'message':{'chat':{'id':456,'type':'private'},'text':'oi'}})
        agent.answer.assert_not_called(); send.assert_not_called()
