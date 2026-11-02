import sqlite3, subprocess, sys, unittest
from pathlib import Path
from test_admin import CatalogAdminTests
from artefact.admin import CatalogAdmin, published_snapshot

class RecoveryTests(CatalogAdminTests):
    def test_real_hot_journals_are_recovered_on_admin_restart(self):
        batch=self.admin.stage(__import__('test_admin').csv_bytes(__import__('test_admin').product()))
        self.accept_all(batch);self.admin.approve(batch,'revisto');version=self.admin.publish(batch)
        before=self.admin.history()
        script='''import sqlite3,sys,os
c=sqlite3.connect(sys.argv[1]);c.execute("ATTACH DATABASE ? AS admin",(sys.argv[2],))
c.execute("PRAGMA main.cache_size=1");c.execute("PRAGMA admin.cache_size=1")
c.execute("BEGIN IMMEDIATE")
c.execute("UPDATE active SET version_id=0 WHERE id=1")
for i in range(150):
 c.execute("INSERT INTO versions(products) VALUES(?)",(b'x'*8192,))
 c.execute("INSERT INTO admin.events(action,reason) VALUES('interrupted',?)",('x'*8192,))
os._exit(0)
'''
        subprocess.run([sys.executable,'-c',script,str(self.root/'serving.db'),str(self.root/'admin.db')],check=True)
        magic=bytes.fromhex('d9d505f920a163d7')
        for name in ('serving.db-journal','admin.db-journal'):
            journal=self.root/name
            self.assertTrue(journal.exists(),name)
            self.assertGreater(journal.stat().st_size,512,name)
            self.assertEqual(journal.read_bytes()[:8],magic,name)
        restarted=CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertEqual(restarted.history(),before)
        self.assertEqual([v['id'] for v in restarted.versions() if v['active']],[version])
        self.assertEqual({v['id'] for v in restarted.versions()},{0,version})
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),2)
    def test_missing_database_is_not_recreated(self):
        (self.root/'serving.db').unlink()
        with self.assertRaises(ValueError): CatalogAdmin(self.root/'admin.db',self.root/'serving.db',self.baseline)
        self.assertFalse((self.root/'serving.db').exists())

    def test_recovery_never_creates_missing_files(self):
        from artefact.admin import _recover_existing
        absent=self.root/'absent.db'
        for admin,serving in ((absent,self.root/'serving.db'),(self.root/'admin.db',absent)):
            with self.assertRaises(sqlite3.OperationalError) as caught: _recover_existing(admin,serving)
            self.assertIn('recuperação',str(caught.exception))
            self.assertNotIn(str(self.root),str(caught.exception))
            self.assertFalse(absent.exists())
