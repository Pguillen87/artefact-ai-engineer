import concurrent.futures, sqlite3, subprocess, sys, threading, unittest
from pathlib import Path
from test_admin import CatalogAdminTests, csv_bytes, product
from artefact.admin import CatalogAdmin, published_snapshot

class ConcurrencyTests(CatalogAdminTests):
    def prepare(self,pid):
        batch=self.admin.stage(csv_bytes(product(str(pid))))
        self.accept_all(batch);self.admin.approve(batch,'revisado')
        return batch
    def test_simultaneous_conflicting_publications(self):
        a=self.prepare(2);b=self.prepare(2);barrier=threading.Barrier(2)
        def publish(batch):
            barrier.wait()
            try: return self.admin.publish(batch)
            except ValueError: return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool: result=list(pool.map(publish,[a,b]))
        self.assertEqual(sum(x is not None for x in result),1)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),2)
    def test_rollback_racing_publication_has_valid_serial_outcome(self):
        batch=self.prepare(2); barrier=threading.Barrier(2)
        def pub(): barrier.wait();return self.admin.publish(batch)
        def roll(): barrier.wait();return self.admin.rollback(0,'revisão concorrente')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(pub);b=pool.submit(roll);a.result();b.result()
        versions=self.admin.versions();self.assertEqual(sum(v['active'] for v in versions),1)
        self.assertIn(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),(1,2))
    def test_concurrent_read_sees_whole_snapshot(self):
        batch=self.prepare(2);version=self.admin.publish(batch)
        def writer():
            for i in range(20): self.admin.rollback(version if i%2 else 0,'teste')
        def reader():
            for _ in range(40):
                count=len(published_snapshot(self.root/'serving.db',self.baseline).tables['products'])
                self.assertIn(count,(1,2));self.assertEqual(sum(v['active'] for v in self.admin.versions()),1)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(writer),pool.submit(reader)]
            for f in futures: f.result()
    def test_killed_writer_recovers_uncommitted_changes(self):
        script="import sqlite3,os,sys; c=sqlite3.connect(sys.argv[1]); c.execute('BEGIN IMMEDIATE'); c.execute('UPDATE active SET version_id=99 WHERE id=1'); os._exit(0)"
        subprocess.run([sys.executable,'-c',script,str(self.root/'serving.db')],check=True)
        self.assertEqual(len(published_snapshot(self.root/'serving.db',self.baseline).tables['products']),1)
    def test_effective_pragmas(self):
        con=self.admin._transaction()
        try:
            self.assertEqual(con.execute('PRAGMA main.journal_mode').fetchone()[0],'delete')
            self.assertEqual(con.execute('PRAGMA admin.journal_mode').fetchone()[0],'delete')
            self.assertEqual(con.execute('PRAGMA main.synchronous').fetchone()[0],2)
            self.assertEqual(con.execute('PRAGMA admin.synchronous').fetchone()[0],2)
            self.assertEqual(con.execute('PRAGMA foreign_keys').fetchone()[0],1)
        finally: con.rollback();con.close()
