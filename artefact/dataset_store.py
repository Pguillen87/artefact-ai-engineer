"""Atomic, versioned storage of complete validated CSV datasets."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from pathlib import Path
from types import MappingProxyType

from .data import HASHES, PREFIX, SCHEMAS, Snapshot, StructuralError, _check_relations, _freeze, _parse, _scan

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024


def _snapshot(files, version=0, contact='(67) 3341-4444'):
    tables = {name: _parse(name, files[name]) for name in SCHEMAS}
    _check_relations(tables)
    hashes = {name: hashlib.sha256(files[name]).hexdigest() for name in SCHEMAS}
    return Snapshot(_freeze(tables), _scan(tables['products'], tables['categories']), MappingProxyType(hashes), contact, version)


def _encode(table, rows):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=SCHEMAS[table]); writer.writeheader()
    for row in rows:
        item = dict(row)
        if table == 'products':
            from .admin import _plain
            item['specs'] = json.dumps(_plain(item['specs']), ensure_ascii=False, allow_nan=False)
        if table == 'promotions': item['is_active'] = '1' if item['is_active'] else '0'
        writer.writerow(item)
    return out.getvalue().encode('utf-8')


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


class DatasetStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute('BEGIN IMMEDIATE')
            from .migrations import ensure_schema
            ensure_schema(con, 'datasets', 1, {
                'dataset_versions': 'CREATE TABLE IF NOT EXISTS dataset_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)',
                'dataset_files': 'CREATE TABLE IF NOT EXISTS dataset_files (version_id INTEGER NOT NULL REFERENCES dataset_versions(id), name TEXT NOT NULL, content BLOB NOT NULL, digest TEXT NOT NULL, PRIMARY KEY(version_id,name))',
                'dataset_active': 'CREATE TABLE IF NOT EXISTS dataset_active (id INTEGER PRIMARY KEY CHECK(id=1), version_id INTEGER NOT NULL REFERENCES dataset_versions(id))',
            })

    def _connect(self):
        con = sqlite3.connect(str(self.path), timeout=10, factory=_Connection)
        con.execute('PRAGMA foreign_keys=ON')
        con.execute('PRAGMA journal_mode=DELETE')
        con.execute('PRAGMA synchronous=FULL')
        return con

    @staticmethod
    def _files(items, official):
        files = {}
        for name, raw in items:
            if not isinstance(name, str) or not isinstance(raw, bytes) or '/' in name or '\\' in name:
                raise StructuralError('Nome ou conteúdo de arquivo inválido')
            table = name[:-4] if name.endswith('.csv') else ''
            if table.startswith(PREFIX): table = table[len(PREFIX):]
            if table not in SCHEMAS or table in files: raise StructuralError('Arquivo desconhecido ou duplicado')
            if len(raw) > MAX_FILE_BYTES: raise StructuralError('Arquivo excede o limite')
            if official and hashlib.sha256(raw).hexdigest() != HASHES[table]: raise StructuralError(f'{table}: source hash mismatch')
            files[table] = raw
        if set(files) != set(SCHEMAS): raise StructuralError('São necessários os seis arquivos CSV')
        if sum(map(len, files.values())) > MAX_TOTAL_BYTES: raise StructuralError('Carga excede o limite')
        return files

    @staticmethod
    def _publish(con, files):
        version = con.execute('INSERT INTO dataset_versions DEFAULT VALUES').lastrowid
        con.executemany('INSERT INTO dataset_files(version_id,name,content,digest) VALUES(?,?,?,?)', [(version, name, raw, hashlib.sha256(raw).hexdigest()) for name, raw in files.items()])
        con.execute('INSERT INTO dataset_active(id,version_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id', (version,))
        return version

    def import_files(self, items, *, official=False, preserve_legacy=False, _fail_for_test=False):
        files = self._files(items, official)
        _snapshot(files)
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            if preserve_legacy:
                if con.execute('SELECT 1 FROM dataset_active WHERE id=1').fetchone():
                    raise ValueError('Migração legada só é permitida antes da primeira importação')
                names={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'active','versions'} <= names:
                    raise ValueError('Catálogo legado não encontrado')
                row=con.execute('SELECT version_id FROM active WHERE id=1').fetchone()
                if row is None: raise ValueError('Catálogo legado incompleto')
                base_snapshot = _snapshot(files)
                legacy_versions = con.execute('SELECT id,products FROM versions ORDER BY id').fetchall()
                if not any(legacy_id == row[0] for legacy_id,_ in legacy_versions): raise ValueError('Catálogo legado incompleto')
                effective_versions=[]
                for legacy_id, raw in legacy_versions:
                    products = list(base_snapshot.tables['products']) + _parse('products',raw)
                    effective = dict(files)
                    effective['products'] = _encode('products',products)
                    _snapshot(effective)
                    effective_versions.append((legacy_id, effective, len(products) == len(base_snapshot.tables['products'])))
                base_version = self._publish(con,files)
                con.execute('CREATE TABLE IF NOT EXISTS dataset_incremental_versions (version_id INTEGER PRIMARY KEY REFERENCES versions(id), dataset_version INTEGER NOT NULL REFERENCES dataset_versions(id), base_version INTEGER NOT NULL REFERENCES dataset_versions(id))')
                version = None
                for legacy_id, effective, empty in effective_versions:
                    migrated = base_version if empty else self._publish(con,effective)
                    con.execute('INSERT INTO dataset_incremental_versions VALUES(?,?,?)',(legacy_id,migrated,base_version))
                    if legacy_id == row[0]: version = migrated
                con.execute('UPDATE dataset_active SET version_id=? WHERE id=1',(version,))
            else:
                version = self._publish(con, files)
            if _fail_for_test: raise RuntimeError('injected publication failure')
            con.commit()
            return version
        except BaseException:
            con.rollback(); raise
        finally: con.close()

    def active_version(self):
        with self._connect() as con:
            row = con.execute('SELECT version_id FROM dataset_active WHERE id=1').fetchone()
            return row[0] if row else None

    def load(self, version_id=None):
        con = self._connect()
        try:
            con.execute('BEGIN')
            if version_id is None:
                row = con.execute('SELECT version_id FROM dataset_active WHERE id=1').fetchone()
                if row is None: raise ValueError('Nenhum dataset ativo')
                version_id = row[0]
            rows = con.execute('SELECT name,content,digest FROM dataset_files WHERE version_id=?', (version_id,)).fetchall()
            if len(rows) != len(SCHEMAS): raise ValueError('Versão de dataset inexistente ou incompleta')
            if any(not isinstance(raw,bytes) or hashlib.sha256(raw).hexdigest() != digest for name,raw,digest in rows): raise StructuralError('Integridade da versão do dataset comprometida')
            contact='(67) 3341-4444'
            if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_settings'").fetchone():
                setting=con.execute("SELECT value FROM public_settings WHERE key='seller_contact'").fetchone()
                if setting:
                    from .admin import _validate_contact
                    contact=_validate_contact(setting[0])
            return _snapshot({name:raw for name,raw,digest in rows}, version_id, contact)
        finally: con.close()

    def versions(self):
        with self._connect() as con:
            return [{'id': r[0], 'created': r[1], 'active': bool(r[2])} for r in con.execute('SELECT v.id,v.created,COALESCE(v.id=a.version_id,0) FROM dataset_versions v LEFT JOIN dataset_active a ON a.id=1 ORDER BY v.id DESC')]

    def activate(self, version_id):
        con = self._connect()
        try:
            con.execute('BEGIN IMMEDIATE')
            if not con.execute('SELECT 1 FROM dataset_versions WHERE id=?', (version_id,)).fetchone(): raise ValueError('Versão inexistente')
            con.execute('INSERT INTO dataset_active(id,version_id) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version_id=excluded.version_id', (version_id,))
            con.commit()
        except BaseException:
            con.rollback(); raise
        finally: con.close()
