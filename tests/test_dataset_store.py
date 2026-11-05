import tempfile
import unittest
from pathlib import Path
from test_data import write_dataset, ROWS
from artefact.data import StructuralError

class DatasetStoreTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
  write_dataset(self.root)
  from artefact.dataset_store import DatasetStore
  self.store=DatasetStore(self.root/'dataset.db')
 def tearDown(self): self.tmp.cleanup()
 def files(self): return [(p.name,p.read_bytes()) for p in self.root.glob('*.csv')]
 def test_import_restart_version_and_rollback(self):
  self.assertIsNone(self.store.active_version())
  first=self.store.import_files(self.files())
  write_dataset(self.root, {'products': {'name':'Second'}})
  second=self.store.import_files(self.files())
  self.assertEqual(self.store.load(first).tables['products'][0]['name'], ROWS['products'][2])
  self.assertEqual(self.store.load().catalog_version,second)
  self.store.activate(first)
  from artefact.dataset_store import DatasetStore
  self.assertEqual(DatasetStore(self.root/'dataset.db').load().catalog_version,first)
 def test_bad_import_never_changes_active(self):
  version=self.store.import_files(self.files())
  for files in (self.files()[:-1], self.files()+[self.files()[0]], [(n,b'bad') if n=='products.csv' else (n,b) for n,b in self.files()]):
   with self.assertRaises((ValueError, StructuralError)): self.store.import_files(files)
   self.assertEqual(self.store.active_version(),version)
 def test_failure_during_publication_is_atomic(self):
  version=self.store.import_files(self.files())
  with self.assertRaises(RuntimeError): self.store.import_files(self.files(), _fail_for_test=True)
  self.assertEqual(self.store.active_version(),version)
  self.assertEqual(len(self.store.versions()),1)

 def test_relation_hash_duplicate_key_and_size_validation(self):
  version=self.store.import_files(self.files())
  write_dataset(self.root,{'products':{'category_id':'999'}})
  with self.assertRaises(StructuralError): self.store.import_files(self.files())
  write_dataset(self.root,extra={'products':ROWS['products']})
  with self.assertRaises(StructuralError): self.store.import_files(self.files())
  write_dataset(self.root)
  with self.assertRaises(StructuralError): self.store.import_files(self.files(), official=True)
  items=self.files(); items[0]=(items[0][0],b'x'*(10*1024*1024+1))
  with self.assertRaises(StructuralError): self.store.import_files(items)
  self.assertEqual(self.store.active_version(),version)

class IncrementalIntegrationTests(unittest.TestCase):
 def test_incremental_publication_persists_full_version_and_rollback(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  from test_admin import csv_bytes, product
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); write_dataset(root)
   store=DatasetStore(root/'serving.db')
   base=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   admin=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   batch=admin.stage(csv_bytes(product()))
   for issue in admin.preview(batch)['issues']: admin.decide(batch,issue['id'],'accept','Revisado')
   admin.approve(batch,'Revisado')
   with self.assertRaises(RuntimeError): admin.publish(batch,_fail_for_test=True)
   self.assertEqual(store.active_version(),base)
   version=admin.publish(batch)
   published=store.active_version()
   self.assertEqual(len(store.load(published).tables['products']),2)
   admin=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   self.assertEqual(len(admin._existing()),1)
   admin.rollback(0,'Reverter')
   self.assertEqual(store.active_version(),base)
   self.assertEqual(len(store.load(published).tables['products']),2)
   admin.rollback(version,'Restaurar')
   self.assertEqual(store.active_version(),published)
 def test_stale_incremental_publication_does_not_replace_new_base(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  from test_admin import csv_bytes, product
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); write_dataset(root)
   store=DatasetStore(root/'serving.db')
   store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   admin=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   batch=admin.stage(csv_bytes(product()))
   for issue in admin.preview(batch)['issues']: admin.decide(batch,issue['id'],'accept','Revisado')
   admin.approve(batch,'Revisado')
   write_dataset(root,{'products':{'name':'Nova base'}})
   latest=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   with self.assertRaises(ValueError): admin.publish(batch)
   self.assertEqual(store.active_version(),latest)

class LegacyMigrationTests(unittest.TestCase):
 def test_explicit_migration_preserves_legacy_incremental_and_history(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  from artefact.data import load_dataset
  from test_admin import csv_bytes, product
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); write_dataset(root)
   legacy=CatalogAdmin(root/'admin.db',root/'serving.db',load_dataset(root))
   batch=legacy.stage(csv_bytes(product()))
   for issue in legacy.preview(batch)['issues']: legacy.decide(batch,issue['id'],'accept','Revisado')
   legacy.approve(batch,'Revisado'); legacy.publish(batch)
   store=DatasetStore(root/'serving.db')
   files=[(p.name,p.read_bytes()) for p in root.glob('*.csv')]
   with self.assertRaises(RuntimeError): store.import_files(files,preserve_legacy=True,_fail_for_test=True)
   self.assertIsNone(store.active_version())
   self.assertEqual(store.versions(),[])
   version=store.import_files(files,preserve_legacy=True)
   self.assertEqual(len(store.load(version).tables['products']),2)
   migrated=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   self.assertEqual(len(migrated._existing()),1)
   self.assertTrue(any(e['action']=='publish' for e in migrated.history()))
   migrated.rollback(0,'Voltar à base anterior')
   self.assertEqual(len(store.load().tables['products']),1)
   migrated.rollback(1,'Restaurar incremental')
   self.assertEqual(store.active_version(),version)
   write_dataset(root,{'products':{'name':'New baseline'}})
   new=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   fresh=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   self.assertEqual(len(fresh._existing()),0)
   self.assertEqual(len(store.load(new).tables['products']),1)
   self.assertEqual(len(store.load(version).tables['products']),2)
   store.activate(version)
   restored=CatalogAdmin(root/'admin.db',root/'serving.db',store.load(),dataset_store=store)
   self.assertEqual(len(restored._existing()),1)
 def test_legacy_conflict_rejects_entire_import(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  from artefact.data import load_dataset
  from test_admin import csv_bytes, product
  with tempfile.TemporaryDirectory() as d:
   root=Path(d); write_dataset(root)
   legacy=CatalogAdmin(root/'admin.db',root/'serving.db',load_dataset(root))
   batch=legacy.stage(csv_bytes(product()))
   for issue in legacy.preview(batch)['issues']: legacy.decide(batch,issue['id'],'accept','Revisado')
   legacy.approve(batch,'Revisado'); legacy.publish(batch)
   write_dataset(root,extra={'products':product()})
   store=DatasetStore(root/'serving.db')
   with self.assertRaises(StructuralError): store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')],preserve_legacy=True)
   self.assertIsNone(store.active_version())
   self.assertEqual(store.versions(),[])

class DatasetIntegrityTests(unittest.TestCase):
 def test_corrupted_stored_bytes_are_never_loaded(self):
  import sqlite3
  from artefact.dataset_store import DatasetStore
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);write_dataset(root); path=root/'catalog.db'; store=DatasetStore(path)
   version=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   con=sqlite3.connect(path)
   con.execute("UPDATE dataset_files SET content=CAST(replace(CAST(content AS TEXT),'Korg','Yamaha') AS BLOB) WHERE name='products'"); con.commit(); con.close()
   with self.assertRaises(StructuralError): store.load(version)
 def test_incomplete_schema_is_refused(self):
  import sqlite3
  from artefact.dataset_store import DatasetStore
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/'catalog.db'
   con=sqlite3.connect(path); con.execute('CREATE TABLE dataset_versions(id INTEGER PRIMARY KEY)'); con.commit(); con.close()
   with self.assertRaises((ValueError, sqlite3.Error)): DatasetStore(path)
 def test_concurrent_readers_only_see_complete_versions(self):
  from concurrent.futures import ThreadPoolExecutor
  from artefact.dataset_store import DatasetStore
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);store=DatasetStore(root/'catalog.db')
   files=[(p.name,p.read_bytes()) for p in root.glob('*.csv')]
   store.import_files(files)
   def publish():
    for _ in range(12): store.import_files(files)
   def read():
    return [store.load() for _ in range(30)]
   with ThreadPoolExecutor(max_workers=3) as pool:
    writer=pool.submit(publish); readers=[pool.submit(read) for _ in range(2)]
    writer.result()
    snapshots=[snapshot for reader in readers for snapshot in reader.result()]
   self.assertTrue(all(set(s.tables)==set(('categories','customers','products','orders','order_items','promotions')) and len(s.tables['products'])==1 for s in snapshots))
 def test_seller_contact_is_read_from_published_settings(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);store=DatasetStore(root/'catalog.db')
   store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   admin=CatalogAdmin(root/'admin.db',root/'catalog.db',store.load(),dataset_store=store)
   admin.set_seller('Test Seller', '(67) 90000-0000')
   self.assertEqual(store.load().seller_contact,'(67) 90000-0000')
   self.assertEqual(store.load().seller_name,'Test Seller')

class StaleAdminTests(unittest.TestCase):
 def test_stale_admin_cannot_publish_after_new_base_and_refresh(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  from test_admin import csv_bytes, product
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);write_dataset(root);store=DatasetStore(root/'catalog.db')
   files=[(p.name,p.read_bytes()) for p in root.glob('*.csv')];store.import_files(files)
   old=CatalogAdmin(root/'admin.db',root/'catalog.db',store.load(),dataset_store=store)
   batch=old.stage(csv_bytes(product()))
   for issue in old.preview(batch)['issues']: old.decide(batch,issue['id'],'accept','Revisado')
   old.approve(batch,'Revisado')
   write_dataset(root,{'products':{'name':'New baseline'}})
   new=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   CatalogAdmin(root/'admin.db',root/'catalog.db',store.load(),dataset_store=store)
   with self.assertRaises(ValueError): old.publish(batch)
   with self.assertRaises(ValueError): old.rollback(0,'obsolete view')
   self.assertEqual(store.active_version(),new)
class DatasetReviewTests(unittest.TestCase):
 def test_review_uses_active_complete_dataset_and_rejects_old_version(self):
  from artefact.dataset_store import DatasetStore
  from artefact.admin import CatalogAdmin
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);write_dataset(root); store=DatasetStore(root/'catalog.db')
   files=[(p.name,p.read_bytes()) for p in root.glob('*.csv')]
   first=store.import_files(files)
   admin=CatalogAdmin(root/'admin.db',root/'catalog.db',store.load(),dataset_store=store)
   self.assertGreater(admin.create_review(1,'brand','inconsistencia_catalogo',first),0)
   write_dataset(root,{'products':{'name':'Yamaha Keyboard','description':'Teclado Korg'}})
   second=store.import_files([(p.name,p.read_bytes()) for p in root.glob('*.csv')])
   with self.assertRaises(ValueError): admin.create_review(1,'brand','inconsistencia_catalogo',first)
   case=admin.create_review(1,'brand','inconsistencia_catalogo',second)
   self.assertEqual(admin.review_cases()[0]['id'],case)
   self.assertEqual(admin.review_cases()[0]['catalog_version'],second)
