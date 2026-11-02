import csv
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from test_data import ROWS, write_dataset
from artefact.conversation import ConversationAgent
from artefact.data import load_dataset
from artefact.services import Catalog


class EffectiveBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def snapshot(self, changes=None, extra=None):
        write_dataset(self.root, changes, extra)
        return load_dataset(self.root)

    def test_promotional_checkout_controls_both_bounds(self):
        snap = self.snapshot({'products': {'price_brl': '549.00'}, 'promotions': {'discount_percent': '20'}})
        catalog = Catalog(snap)
        item = catalog.search(max_price=Decimal('500'))[0]
        self.assertEqual(item['price_quote']['checkout_price_brl'], Decimal('439.20'))
        self.assertEqual(catalog.search(max_price=Decimal('439.19')), ())
        self.assertEqual(catalog.search(min_price=Decimal('439.21')), ())
        self.assertEqual(len(catalog.search(min_price=Decimal('439.20'), max_price=Decimal('439.20'))), 1)

    def test_inactive_promotion_uses_list_price_and_does_not_assume_pix(self):
        snap = self.snapshot({'products': {'price_brl': '500.00'}, 'promotions': {'is_active': '0'}})
        catalog = Catalog(snap)
        self.assertEqual(catalog.search(max_price=Decimal('475')), ())
        self.assertEqual(len(catalog.search(max_price=Decimal('500'))), 1)
        self.assertEqual(catalog.product(1)['price_quote']['pix_price_brl'], Decimal('475.00'))
        without_promotions = replace(snap, tables={**snap.tables, 'promotions': ()})
        self.assertEqual(Catalog(without_promotions).search(max_price=Decimal('475')), ())
        self.assertEqual(len(Catalog(without_promotions).search(max_price=Decimal('500'))), 1)

    def test_conflicting_missing_and_unavailable_prices_cannot_satisfy_budget(self):
        snap = self.snapshot({'products': {'price_brl': '549.00'}},
                             {'promotions': ['2','1','20','second sale','1']})
        catalog = Catalog(snap)
        self.assertEqual(catalog.search(max_price=Decimal('600')), ())
        self.assertEqual(catalog.search(text='Korg')[0]['price_quote']['status'], 'ambiguous')
        products = tuple({**dict(row), 'price_brl': None} for row in snap.tables['products'])
        missing = replace(snap, tables={**snap.tables, 'products': products})
        self.assertEqual(Catalog(missing).search(max_price=Decimal('600')), ())
        invalid_products = tuple({**dict(row), 'price_brl': Decimal('100.001')} for row in snap.tables['products'])
        invalid = replace(snap, tables={**snap.tables, 'products': invalid_products})
        self.assertEqual(Catalog(invalid).search(max_price=Decimal('600')), ())
        unavailable = self.snapshot({'products': {'status': 'coming_soon', 'stock_quantity': '2'}})
        self.assertEqual(Catalog(unavailable).search(max_price=Decimal('2000')), ())
        self.assertFalse(Catalog(unavailable).search(text='Korg')[0]['available'])

    def test_recommendation_order_uses_checkout_not_list_or_pix(self):
        other = list(ROWS['products']); other[0]='2'; other[1]='500'; other[2]='Korg Alternative'
        snap = self.snapshot({'products': {'price_brl':'549'}, 'promotions': {'discount_percent':'20'}},
                             {'products': other})
        results = Catalog(snap).search(text='Korg', available_only=True)
        self.assertEqual([item['product_id'] for item in results], [2,1])
        self.assertEqual([item['price_quote']['checkout_price_brl'] for item in results],
                         [Decimal('500'), Decimal('439.20')])

    def test_unknown_price_does_not_displace_cheapest_known_alternative(self):
        other = list(ROWS['products']); other[0]='2'; other[1]='500'; other[2]='Korg Alternative'
        write_dataset(self.root, extra={'products': other, 'promotions':['2','1','20','conflict','1']})
        third = list(ROWS['products']); third[0]='3'; third[1]='250'; third[2]='Korg Budget'
        with (self.root/'products.csv').open('a',encoding='utf-8',newline='') as stream:
            csv.writer(stream).writerow(third)
        results = Catalog(load_dataset(self.root)).search(text='Korg',available_only=True,limit=2)
        self.assertEqual([item['product_id'] for item in results],[2,3])

    def test_original_snapshot_promotional_item_qualifies_for_budget(self):
        root = Path(r'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data')
        if not root.exists(): self.skipTest('local original baseline unavailable')
        snapshot = load_dataset(root,verify_baseline=True)
        product = Catalog(snapshot).product(121)
        results = Catalog(snapshot).search(text=product['name'],max_price=Decimal('500'),available_only=True)
        self.assertEqual([item['product_id'] for item in results],[121])
        self.assertEqual(results[0]['price_quote']['checkout_price_brl'],Decimal('439.20'))
        class Provider:
            def request(self,messages):
                return {'output':[{'type':'function_call','name':'search_products',
                    'arguments':json.dumps({'text':product['name'],'max_price':'500','available_only':True}), 'call_id':'original'}]}
        answer=ConversationAgent(snapshot,Provider()).answer('O '+product['name']+' está disponível por até R$ 500?',chat_id=1)
        for value in ('R$ 549,00','20%','R$ 439,20','vigência atual não foi confirmada'):
            self.assertIn(value,answer)

    def test_agent_renders_price_discount_and_snapshot_caveat(self):
        snap = self.snapshot({'products': {'price_brl':'549'}, 'promotions': {'discount_percent':'20'}})
        class Provider:
            def request(self, messages):
                return {'output':[{'type':'function_call','name':'search_products',
                    'arguments':json.dumps({'category':'Teclados e Pianos','max_price':'500','available_only':True}),
                    'call_id':'a'}]}
        answer = ConversationAgent(snap, Provider()).answer('Quais teclados disponíveis até R$ 500?', chat_id=1)
        for value in ('R$ 549,00', '20%', 'R$ 439,20', 'não acumula', 'vigência atual não foi confirmada', 'estoque registrado'):
            self.assertIn(value, answer)


if __name__ == '__main__':
    unittest.main()
