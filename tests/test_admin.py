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

    def test_reordered_canonical_columns_are_mapped_by_header_and_reviewed(self):
        headers=['created_at','specs','status','stock_quantity','description','category_id','name','price_brl','product_id']
        values=dict(zip(['product_id','price_brl','name','category_id','description','stock_quantity','status','specs','created_at'],product()))
        stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=headers)
        writer.writeheader();writer.writerow(values)
        batch=self.admin.stage(stream.getvalue().encode())
        preview=self.admin.preview(batch)
        self.assertEqual(preview['products'][0]['name'],'Korg Piano')
        self.assertTrue(preview['issues'])
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
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

    def test_seller_contact_settings_are_public_audited_and_persistent(self):
        from unittest.mock import Mock
        from artefact.conversation import ConversationAgent
        provider=Mock();provider.request.return_value={'output':[{'type':'function_call','name':'policy','arguments':json.dumps({'topic':'contacts'}),'call_id':'x'}]}
        def answer():
            snap=published_snapshot(self.root/'serving.db',self.baseline)
            return ConversationAgent(snap,provider).answer('Qual o contato da loja?')
        self.assertTrue(answer().strip())
        for bad in ('', 'abc', '(00) 90000-0000 ext 1', '<script>'):
            with self.assertRaises(ValueError): self.admin.set_seller_contact(bad)
        self.admin.set_seller_contact('(67) 90000-0000')
        self.assertTrue(answer().strip())
        restarted=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertEqual(restarted.seller_contact(),'(67) 90000-0000')
        restarted.reset_seller_contact()
        self.assertTrue(answer().strip())
        self.assertEqual(restarted.seller_contact(), '')
        self.assertEqual([e['action'] for e in restarted.history()].count('seller_contact'),2)

    def test_named_seller_is_atomic_persistent_and_used_in_handoff(self):
        from unittest.mock import Mock
        from artefact.conversation import ConversationAgent
        self.assertEqual(self.admin.seller_name(), '')
        self.admin.set_seller('  Test Seller  ', '(67) 90000-0000')
        restarted=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertEqual(restarted.seller_name(), 'Test Seller')
        self.assertEqual(restarted.seller_contact(), '(67) 90000-0000')
        provider=Mock();provider.request.return_value={'output':[{'type':'function_call','name':'policy','arguments':json.dumps({'topic':'contacts'}),'call_id':'x'}]}
        answer=ConversationAgent(published_snapshot(self.root/'serving.db',self.baseline),provider).answer('Quero falar com um vendedor')
        self.assertIn('Test Seller',answer)
        self.assertIn('(67) 90000-0000',answer)
        for bad_name,bad_contact in (('Test Seller', 'invalid'), ('<script>', '(67) 90000-0000'), ('a'*121, '(67) 90000-0000'), ('Test Seller\nSouza', '(67) 90000-0000')):
            with self.subTest(name=bad_name), self.assertRaises(ValueError):
                restarted.set_seller(bad_name,bad_contact)
        self.assertEqual(restarted.seller_name(), 'Test Seller')
        self.assertEqual(restarted.seller_contact(), '(67) 90000-0000')
        restarted.set_seller_contact('(67) 90000-0000')
        self.assertEqual(restarted.seller_name(), 'Test Seller')
        self.assertEqual(restarted.history()[0]['action'], 'seller')

    def test_technical_review_queue_validates_and_tracks_minimal_data(self):
        batch=self.admin.stage(csv_bytes(product()));self.accept_all(batch);self.admin.approve(batch,'revisto');v=self.admin.publish(batch)
        with self.assertRaises(ValueError): self.admin.create_review(2,'specs.keys','inconsistencia_catalogo',0)
        with self.assertRaises(ValueError): self.admin.create_review(2,'secret;drop','inconsistencia_catalogo',v)
        with self.assertRaises(ValueError): self.admin.create_review(999,'specs.keys','inconsistencia_catalogo',v)
        case=self.admin.create_review(2,'specs.keys','inconsistencia_catalogo',v)
        restarted=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertEqual(restarted.review_cases()[0]['id'],case)
        self.assertEqual(restarted.review_cases()[0]['status'],'open')
        restarted.complete_review(case)
        self.assertEqual(restarted.review_cases()[0]['status'],'completed')
        with self.assertRaises(ValueError): restarted.complete_review(case)

    def test_agent_requests_review_only_after_explicit_request_and_persistence(self):
        from unittest.mock import Mock
        from artefact.conversation import ConversationAgent
        batch=self.admin.stage(csv_bytes(product()));self.accept_all(batch);self.admin.approve(batch,'ok');version=self.admin.publish(batch)
        snap=published_snapshot(self.root/'serving.db',self.baseline)
        provider=Mock();provider.request.return_value={'output':[{'type':'function_call','name':'request_technical_review','arguments':json.dumps({'product_id':2,'field':'specs.keys','reason':'inconsistencia_catalogo'}),'call_id':'a'}]}
        submit=Mock(return_value=42);agent=ConversationAgent(snap,provider,review_submit=submit)
        self.assertNotIn('registrada',agent.answer('Qual o preço?'));submit.assert_not_called()
        answer=agent.answer('Registre revisão técnica das teclas do produto 2')
        self.assertIn('registrada',answer);submit.assert_called_once_with(2,'specs.keys','inconsistencia_catalogo',version)
        submit.side_effect=OSError('indisponível')
        self.assertNotIn('registrada',agent.answer('Registre revisão técnica das teclas do produto 2'))

    def test_agent_does_not_register_negated_or_unidentified_requests(self):
        from unittest.mock import Mock
        from artefact.conversation import ConversationAgent
        batch=self.admin.stage(csv_bytes(product()));self.accept_all(batch);self.admin.approve(batch,'ok');self.admin.publish(batch)
        snap=published_snapshot(self.root/'serving.db',self.baseline)
        provider=Mock();provider.request.return_value={'output':[{'type':'function_call','name':'request_technical_review','arguments':json.dumps({'product_id':2,'field':'specs.keys','reason':'inconsistencia_catalogo'}),'call_id':'a'}]}
        submit=Mock(return_value=1);agent=ConversationAgent(snap,provider,review_submit=submit)
        for text in ('Não registre revisão técnica do produto 2','Registre revisão técnica','Registre revisão técnica do produto 3'):
            self.assertNotIn('registrada',agent.answer(text))
        submit.assert_not_called()
