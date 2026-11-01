import csv, io, json, tempfile, unittest
from pathlib import Path
from test_data import write_dataset, ROWS
from artefact.data import load_dataset, StructuralError
from artefact.admin import CatalogAdmin, published_snapshot

def csv_bytes(*rows):
    stream=io.StringIO(newline=''); writer=csv.writer(stream)
    writer.writerow(['product_id','price_brl','name','category_id','description','stock_quantity','status','specs','created_at'])
    writer.writerows(rows)
    return stream.getvalue().encode()

def product(id='2', price='250.00', name='Korg Piano'):
    return [id,price,name,'1','Teclado Roland com 88 teclas','12','active',json.dumps({'keys':'61'}),'2026-01-01']

class CatalogAdminTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        write_dataset(self.root); self.baseline=load_dataset(self.root)
        self.admin=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
    def tearDown(self): self.tmp.cleanup()
    def test_stage_review_publish_and_rollback(self):
        batch=self.admin.stage(csv_bytes(product()))
        self.assertTrue(self.admin.preview(batch)['issues'])
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        with self.assertRaises(ValueError): self.admin.publish(batch)
        self.admin.approve(batch, 'Conferido pelo operador')
        version=self.admin.publish(batch)
        snap=published_snapshot(self.root/'serving.db',self.baseline)
        self.assertEqual(len(snap.tables['products']),2)
        self.assertEqual(str(snap.tables['products'][1]['price_brl']),'250.00')
        self.assertTrue(any(i.product_id==2 for i in snap.issues))
        self.admin.rollback(0)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        self.admin.rollback(version)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),2)
    def test_invalid_batch_is_never_staged(self):
        for raw in [csv_bytes(product(price='-1')),csv_bytes(product('1')),csv_bytes(product(),product()),csv_bytes(product(),product('3','bad')),b'wrong,header\n1,2',csv_bytes(product('3','1',''))]:
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError,StructuralError)): self.admin.stage(raw)
        self.assertEqual(self.admin.batches(),[])
    def test_failure_does_not_change_active_version(self):
        batch=self.admin.stage(csv_bytes(product())); self.admin.approve(batch,'revisto')
        with self.assertRaises(RuntimeError): self.admin.publish(batch, _fail_for_test=True)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        self.assertEqual(self.admin.preview(batch)['status'],'approved')
    def test_persistence_and_duplicate_published_id(self):
        batch=self.admin.stage(csv_bytes(product())); self.admin.approve(batch,'revisto'); self.admin.publish(batch)
        another=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),2)
        with self.assertRaises(ValueError): another.stage(csv_bytes(product()))

    def test_agent_reads_only_published_and_one_version_per_turn(self):
        from unittest.mock import Mock
        from artefact.conversation import ConversationAgent
        provider=Mock()
        provider.request.return_value={'output':[{'type':'function_call','name':'search_products','arguments':json.dumps({'text':'Korg Piano'}),'call_id':'a'}]}
        agent=ConversationAgent(self.baseline,provider, snapshot_loader=lambda: published_snapshot(self.root/'serving.db',self.baseline))
        batch=self.admin.stage(csv_bytes(product()))
        self.assertNotIn('250,00',agent.answer('Preço Korg Piano?'))
        self.admin.approve(batch,'revisto'); self.admin.publish(batch)
        self.assertIn('250,00',agent.answer('Preço Korg Piano?'))

    def test_operational_databases_outside_original_data(self):
        from artefact.admin_web import validate_paths
        with self.assertRaises(ValueError): validate_paths(self.root/'admin.db',self.root/'serving.db',self.root)
        with self.assertRaises(ValueError): validate_paths(Path.cwd()/'local.db',self.root.parent/'other.db',self.root)
