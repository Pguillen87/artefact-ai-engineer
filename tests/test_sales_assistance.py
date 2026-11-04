import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.intent import normalize_catalog_args
from test_data import write_dataset, ROWS


class SalesAssistanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(); root = Path(self.tmp.name)
        extra = list(ROWS['products']); extra[0]='2'; extra[2]='Violao esgotado'; extra[5]='0'
        write_dataset(root, {'categories':{'name':'Violões'}, 'promotions':{'is_active':'0'}, 'products':{
            'name':'Yamaha C40 Nylon Natural','description':'Violão Yamaha','price_brl':'599.90','specs':'{}'}},extra={'products':extra})
        self.snapshot = load_dataset(root)

    def tearDown(self): self.tmp.cleanup()

    def answer(self, text, args, tool='search_products'):
        provider = Mock()
        provider.request.return_value = {'output':[{'type':'function_call','name':tool,'call_id':'test','arguments':json.dumps(args)}]}
        agent = ConversationAgent(self.snapshot,provider)
        return agent.answer(text,chat_id='test'), agent.last_trace

    def test_category_alias_searches_available_products_instead_of_blame(self):
        for spelling in ('violao','violão','Violões'):
            answer, trace = self.answer('quero um violão',{'category':spelling})
            self.assertEqual(trace['executed_tools'],['search_products'])
            self.assertIn('Yamaha C40',answer)
            self.assertIn('R$ 599,90',answer)
            self.assertNotIn('Violao esgotado',answer)
            self.assertNotIn('filtro',answer)
            self.assertIn('Qual faixa de preço',answer)

    def test_generic_text_category_is_resolved_but_wrong_category_is_not(self):
        answer, trace = self.answer('quero um violão',{'text':'violão'})
        self.assertIn('Yamaha C40',answer)
        self.assertEqual(trace['executed_tools'],['search_products'])
        _, trace = self.answer('quero um violão',{'category':'Guitarras'})
        self.assertEqual(trace['executed_tools'],[])

    def test_negation_and_invented_budgets_are_still_rejected(self):
        for text,args in [('não quero violão',{'category':'violao'}),
                          ('quero um violão',{'category':'violao','max_price':'500'})]:
            _, trace = self.answer(text,args)
            self.assertEqual(trace['executed_tools'],[])

    def test_customer_budget_is_respected_and_not_asked_again(self):
        answer, trace = self.answer('quero um violão até R$ 700',{'category':'violao','max_price':'700'})
        self.assertEqual(trace['executed_tools'],['search_products'])
        self.assertIn('Yamaha C40',answer)
        self.assertNotIn('Qual faixa de preço',answer)
        self.assertIn('conhecer melhor',answer.lower())

    def test_no_false_scarcity_or_unsupported_benefits(self):
        answer, _ = self.answer('quero um violão',{'category':'violao'})
        for claim in ('última unidade','mais vendido','melhor para iniciantes','só hoje'):
            self.assertNotIn(claim,answer.lower())
        self.assertIn('base importada',answer)

    def test_specific_model_keeps_complete_quote(self):
        answer, _ = self.answer('qual o preço do Yamaha C40 Nylon Natural?',{'text':'Yamaha C40 Nylon Natural'})
        self.assertIn('R$ 599,90',answer)
        self.assertIn('R$ 569,91',answer)

    def test_compound_or_brand_query_is_not_erased_as_category(self):
        for query in ('violão Yamaha','violão nylon','guitarra','violão de aço','violão 7 cordas'):
            args = {'text':query}
            self.assertEqual(normalize_catalog_args('search_products',args,'quero um violão',self.snapshot),args)
            self.assertEqual(normalize_catalog_args('search_products',args,'quero um '+query,self.snapshot),args)

    def test_budget_answer_uses_category_only_after_question_and_survives_context_serialization(self):
        from artefact.session_store import _encode, _decode
        provider=Mock()
        provider.request.return_value={'output':[{'type':'function_call','name':'search_products','call_id':'1','arguments':'{"category":"violao"}'}]}
        first=ConversationAgent(self.snapshot,provider)
        self.assertIn('Qual faixa de preço',first.answer('quero um violão',chat_id='same'))
        persisted=_decode(json.loads(json.dumps(_encode(first.context['same']))))
        self.assertTrue(persisted.awaiting_budget)
        provider.request.return_value={'output':[{'type':'function_call','name':'search_products','call_id':'2','arguments':'{"category":"violao","max_price":"700"}'}]}
        resumed=ConversationAgent(self.snapshot,provider);resumed.context['same']=persisted
        answer=resumed.answer('700',chat_id='same')
        self.assertEqual(resumed.last_trace['executed_tools'],['search_products'])
        self.assertIn('Yamaha C40',answer)
        self.assertNotIn('Qual faixa de preço',answer)
        self.assertNotIn('Quero Violões',resumed.answer('oi',chat_id='other'))

    def test_budget_question_expires_after_another_turn(self):
        provider=Mock()
        browse={'output':[{'type':'function_call','name':'search_products','call_id':'1','arguments':'{"category":"violao"}'}]}
        for interrupt in ('oi','quem é você','e o preço?'):
            agent=ConversationAgent(self.snapshot,provider);provider.request.return_value=browse
            agent.answer('quero um violão',chat_id='test')
            self.assertTrue(agent.context['test'].awaiting_budget)
            provider.request.return_value={'output':[]}
            agent.answer(interrupt,chat_id='test')
            self.assertFalse(agent.context['test'].awaiting_budget)
