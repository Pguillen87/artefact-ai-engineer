import sqlite3
import unittest
from artefact.migrations import ensure_schema

class SchemaTests(unittest.TestCase):
    def test_records_schema_and_accepts_compatible_existing_tables(self):
        db = sqlite3.connect(':memory:')
        tables = {'thing':'CREATE TABLE IF NOT EXISTS thing (id INTEGER PRIMARY KEY, value TEXT NOT NULL)'}
        db.execute(tables['thing'])
        ensure_schema(db, 'example', 1, tables)
        self.assertEqual(db.execute("SELECT version FROM artefact_schema_versions WHERE component='example'").fetchone()[0], 1)
        ensure_schema(db, 'example', 1, tables)
        db.close()
    def test_refuses_partial_or_incompatible_schema(self):
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE thing (id TEXT)')
        with self.assertRaises(ValueError):
            ensure_schema(db, 'example', 1, {'thing':'CREATE TABLE IF NOT EXISTS thing (id INTEGER PRIMARY KEY, value TEXT NOT NULL)'})
        db.close()
    def test_transaction_rollback_removes_component_and_version(self):
        db=sqlite3.connect(':memory:')
        db.execute('BEGIN IMMEDIATE')
        ensure_schema(db,'example',1,{'thing':'CREATE TABLE IF NOT EXISTS thing (id INTEGER PRIMARY KEY)'})
        db.rollback()
        names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('thing',names)
        self.assertNotIn('artefact_schema_versions',names)
        db.close()
