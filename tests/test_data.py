import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from decimal import Decimal

from artefact.data import load_dataset, StructuralError

HEADERS = {
 'categories': ['category_id','name','description'],
 'customers': ['customer_id','name','phone','email','city'],
 'products': ['product_id','price_brl','name','category_id','description','stock_quantity','status','specs','created_at'],
 'orders': ['order_id','customer_id','order_date','status','total_brl','payment_method','tracking_code','estimated_delivery','notes'],
 'order_items': ['order_id','quantity','product_id'],
 'promotions': ['promotion_id','product_id','discount_percent','description','is_active'],
}
ROWS = {
 'categories': ['1','Teclados e Pianos','instrumentos'],
 'customers': ['1','Synthetic Customer','000','fake@example.invalid','Test City'],
 'products': ['1','1127.50','Korg Synth','1','Sintetizador Roland com 88 teclas','2','active',json.dumps({'keys':'61'}),'2023-03-05'],
 'orders': ['1','1','2026-01-01','pending','1127.50','pix','','',''],
 'order_items': ['1','1','1'],
 'promotions': ['1','1','10','sale','1'],
}

def write_dataset(path, changes=None, extra=None):
 changes = changes or {}
 for table, header in HEADERS.items():
  with (path / f'{table}.csv').open('w', encoding='utf-8', newline='') as stream:
   writer = csv.writer(stream); writer.writerow(header)
   row = list(ROWS[table]);
   for field,value in changes.get(table, {}).items(): row[header.index(field)] = value
   writer.writerow(row)
   if extra and table in extra: writer.writerow(extra[table])

class DataTests(unittest.TestCase):
 def test_load_and_report_local_conflict_without_blocking_price(self):
  with TemporaryDirectory() as d:
   root=Path(d); write_dataset(root); snapshot=load_dataset(root)
   product=snapshot.tables['products'][0]
   self.assertEqual(product['price_brl'],Decimal('1127.50'))
   self.assertEqual(product['stock_quantity'],2)
   issues={(i.field,i.rule) for i in snapshot.issues}
   self.assertIn(('brand','brand_conflict'),issues)
   self.assertIn(('specs.keys','numeric_conflict'),issues)
   self.assertTrue(all(i.confidence for i in snapshot.issues))
 def test_invalid_structural_data_is_rejected(self):
  cases=[{'products':{'price_brl':'-1'}},{'products':{'specs':'[]'}},{'products':{'stock_quantity':'1.5'}},{'products':{'category_id':'999'}},{'orders':{'total_brl':'NaN'}},{'promotions':{'is_active':'yes'}},{'order_items':{'quantity':'0'}}]
  for changes in cases:
   with self.subTest(changes=changes), TemporaryDirectory() as d:
    root=Path(d);write_dataset(root,changes)
    with self.assertRaises(StructuralError): load_dataset(root)
 def test_duplicate_key_and_missing_header_are_rejected(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root,extra={'products':ROWS['products']})
   with self.assertRaises(StructuralError): load_dataset(root)
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);(root/'orders.csv').write_text('bad,header\n1,2\n')
   with self.assertRaises(StructuralError): load_dataset(root)
 def test_real_baseline(self):
  root=Path(r'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data')
  if not root.exists(): self.skipTest('local private baseline unavailable')
  before={p.name:p.read_bytes() for p in root.glob('*.csv')}
  snapshot=load_dataset(root, verify_baseline=True)
  product=next(p for p in snapshot.tables['products'] if p['product_id']==142)
  self.assertEqual(product['price_brl'],Decimal('1127'))
  self.assertTrue(any(i.product_id==142 and i.field=='specs.keys' for i in snapshot.issues))
  self.assertTrue(any(i.product_id==142 and i.rule=='brand_conflict' for i in snapshot.issues))
  self.assertEqual(before,{p.name:p.read_bytes() for p in root.glob('*.csv')})

class AdditionalDataTests(unittest.TestCase):
 def test_wrong_category_and_no_false_brand_for_multiple_mentions(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root, {'products':{'name':'Guitarra Korg', 'description':'Works with Roland and Yamaha equipment'}})
   issues=load_dataset(root).issues
   self.assertTrue(any(i.rule=='category_conflict' for i in issues))
   self.assertFalse(any(i.rule=='brand_conflict' for i in issues))
 def test_invalid_json_date_reference_and_header(self):
  cases=[{'products':{'specs':'{'}},{'orders':{'order_date':'2026-02-30'}},{'promotions':{'product_id':'99'}},{'order_items':{'order_id':'99'}},{'products':{'name':''}}]
  for changes in cases:
   with self.subTest(changes=changes), TemporaryDirectory() as d:
    root=Path(d);write_dataset(root,changes)
    with self.assertRaises(StructuralError): load_dataset(root)
 def test_snapshot_is_deeply_immutable(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);snapshot=load_dataset(root)
   with self.assertRaises(TypeError): snapshot.tables['products'][0]['specs']['keys']='88'
 def test_original_hash_mismatch_is_rejected(self):
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   for table in HEADERS: (root / f'{table}.csv').rename(root / f'desafio_tecnico_ai_eng - {table}.csv')
   with self.assertRaises(StructuralError): load_dataset(root,verify_baseline=True)

class ReportTests(unittest.TestCase):
 def test_report_contains_only_sanitized_product_findings(self):
  import subprocess, sys
  with TemporaryDirectory() as d:
   root=Path(d);write_dataset(root)
   result=subprocess.run([sys.executable,'-m','artefact.report',str(root)],capture_output=True,text=True,check=True)
   report=json.loads(result.stdout)
   self.assertEqual(report['counts']['customers'],1)
   self.assertTrue(any(i['field']=='specs.keys' for i in report['issues']))
   self.assertNotIn('Synthetic Customer',result.stdout)

class StrictJSONTests(unittest.TestCase):
 def test_nonstandard_and_duplicate_json_keys_rejected(self):
  for value in ['{"keys":NaN}', '{"keys":61,"keys":88}']:
   with self.subTest(value=value), TemporaryDirectory() as d:
    root=Path(d);write_dataset(root,{'products':{'specs':value}})
    with self.assertRaises(StructuralError): load_dataset(root)
