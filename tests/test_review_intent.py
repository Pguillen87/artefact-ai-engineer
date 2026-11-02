import concurrent.futures,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock
from test_data import write_dataset
from test_admin import csv_bytes,product
from artefact.data import load_dataset
from artefact.admin import CatalogAdmin,published_snapshot
from artefact.conversation import ConversationAgent

class ReviewIntentTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);write_dataset(self.root);self.base=load_dataset(self.root)
  self.admin=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.base)
  row=product();batch=self.admin.stage(csv_bytes(row))
  for i in self.admin.preview(batch)['issues']:self.admin.decide(batch,i['id'],'accept','revisado')
  self.admin.approve(batch,'revisto');self.version=self.admin.publish(batch)
  self.snapshot=published_snapshot(self.root/'serving.db',self.base)
  self.submit=Mock(return_value=5);self.provider=Mock()
 def tearDown(self):self.tmp.cleanup()
 def answer(self,text,field='specs.keys',calls=None):
  call={'type':'function_call','name':'request_technical_review','arguments':json.dumps({'product_id':2,'field':field,'reason':'inconsistencia_catalogo'}),'call_id':'a'}
  self.provider.request.return_value={'output':calls if calls is not None else [call]}
  return ConversationAgent(self.snapshot,self.provider,review_submit=self.submit).answer(text)
 def test_multiple_conflicts_require_customer_field(self):
  response=self.answer('Registre revisão técnica do produto 2')
  self.assertIn('qual',response.casefold());self.submit.assert_not_called()
  response=self.answer('Registre revisão técnica das teclas do produto 2')
  self.assertIn('registrada',response);self.submit.assert_called_once_with(2,'specs.keys','inconsistencia_catalogo',self.version)
 def test_questions_negation_and_ambiguous_numbers_do_not_write(self):
  cases=['Como registrar revisão técnica das teclas do produto 2?', 'É possível registrar revisão técnica das teclas do produto 2?', 'Não registre revisão das teclas do produto 2', 'Registre revisão das teclas do pedido 2', 'Registre revisão de teclas, valor R$ 2,00', 'Registre revisão das teclas do produto 2,00 reais', 'Registre revisão técnica das teclas do produto 3 e pedido 2', 'Registre revisão das teclas dos produtos 2 e 3']
  for text in cases:
   with self.subTest(text=text):self.assertNotIn('registrada',self.answer(text))
  self.submit.assert_not_called()
 def test_compound_request_cannot_select_an_unmentioned_field(self):
  self.assertNotIn('registrada',self.answer('Registre revisão da marca do produto 2 e informe o preço',field='specs.keys'))
  self.submit.assert_not_called()
 def test_single_conflict_accepts_explicit_unqualified_review(self):
  row=product('3',name='Korg Solo');row[4]='Korg com 88 teclas'
  batch=self.admin.stage(csv_bytes(row))
  for i in self.admin.preview(batch)['issues']:self.admin.decide(batch,i['id'],'accept','revisto')
  self.admin.approve(batch,'revisto');self.admin.publish(batch);self.snapshot=published_snapshot(self.root/'serving.db',self.base)
  call={'type':'function_call','name':'request_technical_review','arguments':json.dumps({'product_id':3,'field':'specs.keys','reason':'inconsistencia_catalogo'}),'call_id':'a'}
  self.assertIn('registrada',self.answer('Registre revisão técnica do produto 3',calls=[call]))
 def test_duplicate_open_requests_are_atomic_and_new_after_completion(self):
  barrier=threading.Barrier(4)
  def submit(_):
   barrier.wait();return self.admin.create_review(2,'specs.keys','inconsistencia_catalogo',self.version)
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool: ids=list(pool.map(submit,range(4)))
  self.assertEqual(len(set(ids)),1);self.assertEqual(len(self.admin.review_cases()),1)
  self.admin.complete_review(ids[0]);new=self.admin.create_review(2,'specs.keys','inconsistencia_catalogo',self.version)
  self.assertNotEqual(new,ids[0])

 def test_explicit_name_and_compound_order_reference(self):
  result=self.answer('Registre revisão das teclas do Korg Piano e informe o pedido 2')
  self.assertIn('registrada',result)
  self.submit.assert_called_once_with(2,'specs.keys','inconsistencia_catalogo',self.version)
 def test_request_with_multiple_details_is_ambiguous(self):
  response=self.answer('Registre revisão da marca e teclas do produto 2')
  self.assertIn('qual',response.casefold());self.submit.assert_not_called()

 def test_legacy_duplicate_open_cases_are_preserved(self):
  import sqlite3
  first=self.admin.create_review(2,'specs.keys','inconsistencia_catalogo',self.version)
  con=sqlite3.connect(self.root/'admin.db')
  with con: con.execute("INSERT INTO technical_reviews(product_id,field,reason,catalog_version,status) VALUES(2,'specs.keys','inconsistencia_catalogo',?,'open')",(self.version,))
  con.close()
  restored=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.base)
  self.assertEqual(len(restored.review_cases()),2)
  self.assertEqual(restored.create_review(2,'specs.keys','inconsistencia_catalogo',self.version),first)
  self.assertEqual(len(restored.review_cases()),2)
