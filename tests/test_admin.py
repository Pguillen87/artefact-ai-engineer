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
    def accept_all(self,batch):
        for issue in self.admin.preview(batch)['issues']:
            self.admin.decide(batch,issue['id'],'accept','Revisado individualmente')
    def test_stage_review_publish_and_rollback(self):
        batch=self.admin.stage(csv_bytes(product()))
        self.assertTrue(self.admin.preview(batch)['issues'])
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        with self.assertRaises(ValueError): self.admin.publish(batch)
        self.accept_all(batch)
        self.admin.approve(batch, 'Conferido pelo operador')
        version=self.admin.publish(batch)
        snap=published_snapshot(self.root/'serving.db',self.baseline)
        self.assertEqual(len(snap.tables['products']),2)
        self.assertEqual(str(snap.tables['products'][1]['price_brl']),'250.00')
        self.assertTrue(any(i.product_id==2 for i in snap.issues))
        self.admin.rollback(0,'retorno')
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        self.admin.rollback(version,'retorno')
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),2)
    def test_invalid_batch_is_never_staged(self):
        for raw in [csv_bytes(product(price='-1')),csv_bytes(product('1')),csv_bytes(product(),product()),csv_bytes(product(),product('3','bad')),b'wrong,header\n1,2',csv_bytes(product('3','1',''))]:
            with self.subTest(raw=raw):
                with self.assertRaises((ValueError,StructuralError)): self.admin.stage(raw)
        self.assertEqual(self.admin.batches(),[])
    def test_failure_does_not_change_active_version(self):
        batch=self.admin.stage(csv_bytes(product())); self.accept_all(batch); self.admin.approve(batch,'revisto')
        with self.assertRaises(RuntimeError): self.admin.publish(batch, _fail_for_test=True)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
        self.assertEqual(self.admin.preview(batch)['status'],'approved')
    def test_persistence_and_duplicate_published_id(self):
        batch=self.admin.stage(csv_bytes(product())); self.accept_all(batch); self.admin.approve(batch,'revisto'); self.admin.publish(batch)
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
        self.accept_all(batch); self.admin.approve(batch,'revisto'); self.admin.publish(batch)
        self.assertIn('250,00',agent.answer('Preço Korg Piano?'))

    def test_operational_databases_outside_original_data(self):
        from artefact.admin_web import validate_paths
        with self.assertRaises(ValueError): validate_paths(self.root/'admin.db',self.root/'serving.db',self.root)
        with self.assertRaises(ValueError): validate_paths(Path.cwd()/'local.db',self.root.parent/'other.db',self.root)

    def test_each_conflict_requires_specific_decision_and_revalidation(self):
        import sqlite3
        batch=self.admin.stage(csv_bytes(product()))
        issues=self.admin.preview(batch)['issues']; self.assertGreaterEqual(len(issues),2)
        with self.assertRaises(ValueError): self.admin.approve(batch,'genérico')
        with self.assertRaises(ValueError): self.admin.decide(batch,issues[0]['id'],'corrected','supostamente corrigido')
        for issue in issues: self.admin.decide(batch,issue['id'],'accept','Conferido individualmente')
        self.admin.approve(batch,'revisão completa')
        db=sqlite3.connect(self.root/'admin.db')
        with db: db.execute('UPDATE batches SET csv=? WHERE id=?',(csv_bytes(product(price='251.00')),batch))
        db.close()
        with self.assertRaises(ValueError): self.admin.publish(batch)

    def test_rollback_requires_reason_and_persists(self):
        with self.assertRaises(ValueError): self.admin.rollback(0,'')
        with self.assertRaises(ValueError): self.admin.rollback(999,'correção')
        self.admin.rollback(0,'retornar ao baseline')
        another=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertTrue(any(e['reason']=='retornar ao baseline' for e in another.history()))

    def test_missing_projection_fails_closed(self):
        with self.assertRaises((OSError,ValueError)): published_snapshot(self.root/'missing.db',self.baseline)

    def test_excessive_rows_and_deep_json_are_rejected(self):
        many=csv_bytes(*[product(str(i+10)) for i in range(1001)])
        with self.assertRaises((ValueError,StructuralError)): self.admin.stage(many)
        row=product();row[7]='{"x":'+('['*1200)+'0'+(']'*1200)+'}'
        with self.assertRaises((ValueError,StructuralError)): self.admin.stage(csv_bytes(row))

    def test_missing_pair_requires_explicit_initialization(self):
        from artefact.admin import require_explicit_initialization
        with self.assertRaises(ValueError): require_explicit_initialization(self.root/'new-admin',self.root/'new-serving',False)
        require_explicit_initialization(self.root/'new-admin',self.root/'new-serving',True)
        require_explicit_initialization(self.root/'admin.db',self.root/'serving.db',False)

    def test_migrates_existing_admin_events_without_losing_versions(self):
        import sqlite3
        old=self.root/'old-admin.db'; serving=self.root/'old-serving.db'
        con=sqlite3.connect(old)
        con.executescript("CREATE TABLE batches(id INTEGER PRIMARY KEY,csv BLOB NOT NULL,status TEXT NOT NULL,review TEXT NOT NULL DEFAULT '',created TEXT DEFAULT CURRENT_TIMESTAMP); CREATE TABLE events(id INTEGER PRIMARY KEY,action TEXT NOT NULL,batch_id INTEGER,version_id INTEGER,created TEXT DEFAULT CURRENT_TIMESTAMP); INSERT INTO events(action) VALUES('old_event');")
        con.commit();con.close()
        con=sqlite3.connect(serving)
        con.execute('CREATE TABLE versions(id INTEGER PRIMARY KEY,products BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP)')
        con.execute('CREATE TABLE active(id INTEGER PRIMARY KEY,version_id INTEGER NOT NULL)')
        con.execute('INSERT INTO versions(id,products) VALUES(0,?)',(csv_bytes(),))
        con.execute('INSERT INTO active VALUES(1,0)');con.commit();con.close()
        migrated=CatalogAdmin(old,serving,self.baseline)
        self.assertEqual(migrated.history()[0]['action'],'old_event')
        self.assertEqual(migrated.versions()[0]['id'],0)
        migrated.rollback(0,'Motivo após migração')
        self.assertEqual(migrated.history()[-1]['reason'],'Motivo após migração')

    def test_publish_rejects_changed_findings_after_approval(self):
        import sqlite3
        batch=self.admin.stage(csv_bytes(product()));self.accept_all(batch);self.admin.approve(batch,'revisado')
        altered=product();altered[4]='Korg com 61 teclas'
        db=sqlite3.connect(self.root/'admin.db')
        with db: db.execute('UPDATE batches SET csv=? WHERE id=?',(csv_bytes(altered),batch))
        db.close()
        with self.assertRaises(ValueError): self.admin.publish(batch)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
