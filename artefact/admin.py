"""Administrative staging and a separate, published-only catalog projection."""
from __future__ import annotations
import csv
import io
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from .data import SCHEMAS, Snapshot, StructuralError, _freeze, _parse, _scan

MAX_BYTES = 1024 * 1024
MAX_ROWS = 1000


def _encode_products(rows):
    out = io.StringIO(newline='')
    writer = csv.DictWriter(out, fieldnames=SCHEMAS['products'])
    writer.writeheader()
    for row in rows:
        item = dict(row)
        item['specs'] = json.dumps(_plain(item['specs']), ensure_ascii=False, allow_nan=False)
        writer.writerow(item)
    return out.getvalue().encode('utf-8')


def _plain(value):
    from collections.abc import Mapping
    if isinstance(value, Mapping): return {k: _plain(v) for k,v in value.items()}
    if isinstance(value, (tuple, list)): return [_plain(v) for v in value]
    return value


def _validate(raw, baseline, existing=()):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES: raise ValueError('Arquivo excede o limite de 1 MiB')
    rows = _parse('products', raw)
    if not rows or len(rows) > MAX_ROWS: raise ValueError('O lote deve conter de 1 a 1000 produtos')
    if any(row['price_brl'].as_tuple().exponent < -2 or row['price_brl'] > 100000000 for row in rows): raise ValueError('Preço deve ter até duas casas decimais')
    categories = {row['category_id'] for row in baseline.tables['categories']}
    if any(row['category_id'] not in categories for row in rows): raise ValueError('Categoria inexistente')
    ids = {row['product_id'] for row in baseline.tables['products']} | {row['product_id'] for row in existing}
    if any(row['product_id'] in ids for row in rows): raise ValueError('ID já existente no catálogo')
    return rows


class _Connection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()


def _connect(path):
    con = sqlite3.connect(str(path), timeout=10, factory=_Connection)
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=DELETE')
    return con


def published_snapshot(path, baseline):
    """Read one active immutable version; never opens the administration database."""
    path = Path(path)
    if not path.exists(): return baseline
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        con.execute('BEGIN')
        row = con.execute('SELECT v.products FROM active a JOIN versions v ON v.id=a.version_id WHERE a.id=1').fetchone()
        if row is None: raise ValueError('Catálogo publicado inválido')
        products = _parse('products', row[0])
        tables = dict(baseline.tables)
        tables['products'] = tuple(baseline.tables['products']) + tuple(_freeze(p) for p in products)
        issues = baseline.issues + _scan(products, baseline.tables['categories'])
        return Snapshot(MappingProxyType(tables), issues, baseline.hashes)
    finally:
        con.close()


class CatalogAdmin:
    def __init__(self, admin_path, serving_path, baseline):
        self.admin_path = Path(admin_path).resolve()
        self.serving_path = Path(serving_path).resolve()
        self.baseline = baseline
        if self.admin_path == self.serving_path: raise ValueError('Bancos devem ser separados')
        self.admin_path.parent.mkdir(parents=True, exist_ok=True)
        self.serving_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.admin_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, csv BLOB NOT NULL, status TEXT NOT NULL, review TEXT NOT NULL DEFAULT "", created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            con.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, action TEXT NOT NULL, batch_id INTEGER, version_id INTEGER, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
        with _connect(self.serving_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, products BLOB NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            con.execute('CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY CHECK(id=1), version_id INTEGER NOT NULL REFERENCES versions(id))')
            con.execute('INSERT OR IGNORE INTO versions(id,products) VALUES(0,?)', (_encode_products([]),))
            con.execute('INSERT OR IGNORE INTO active(id,version_id) VALUES(1,0)')

    def _existing(self):
        snapshot = published_snapshot(self.serving_path, self.baseline)
        return snapshot.tables['products'][len(self.baseline.tables['products']):]

    def stage(self, raw):
        _validate(raw, self.baseline, self._existing())
        with _connect(self.admin_path) as con:
            return con.execute('INSERT INTO batches(csv,status) VALUES(?,?)', (raw,'staged')).lastrowid

    def batches(self):
        with _connect(self.admin_path) as con:
            return [{'id':r[0], 'status':r[1], 'created':r[2]} for r in con.execute('SELECT id,status,created FROM batches ORDER BY id DESC')]

    def preview(self, batch):
        with _connect(self.admin_path) as con:
            row = con.execute('SELECT csv,status,review FROM batches WHERE id=?',(batch,)).fetchone()
        if not row: raise ValueError('Lote inexistente')
        products = _parse('products', row[0])
        return {'status':row[1], 'review':row[2], 'products':products,
                'issues':[asdict(i) for i in _scan(products,self.baseline.tables['categories'])]}

    def approve(self, batch, review):
        if not isinstance(review,str) or not review.strip() or len(review)>1000: raise ValueError('Informe a revisão humana')
        with _connect(self.admin_path) as con:
            cur=con.execute("UPDATE batches SET status='approved',review=? WHERE id=? AND status='staged'",(review.strip(),batch))
            if cur.rowcount != 1: raise ValueError('Lote não está aguardando revisão')
            con.execute("INSERT INTO events(action,batch_id) VALUES('approve',?)",(batch,))

    def _transaction(self):
        con=_connect(self.serving_path)
        con.execute('ATTACH DATABASE ? AS admin', (str(self.admin_path),))
        con.execute('BEGIN IMMEDIATE')
        return con

    def publish(self, batch, *, _fail_for_test=False):
        con=self._transaction()
        try:
            row=con.execute("SELECT csv FROM admin.batches WHERE id=? AND status='approved'",(batch,)).fetchone()
            if not row: raise ValueError('Aprovação humana necessária')
            old=con.execute('SELECT v.products FROM active a JOIN versions v ON v.id=a.version_id WHERE a.id=1').fetchone()
            existing=_parse('products',old[0])
            added=_validate(row[0],self.baseline,existing)
            version=con.execute('INSERT INTO versions(products) VALUES(?)',(_encode_products(existing+added),)).lastrowid
            con.execute('UPDATE active SET version_id=? WHERE id=1',(version,))
            con.execute("UPDATE admin.batches SET status='published' WHERE id=?",(batch,))
            con.execute("INSERT INTO admin.events(action,batch_id,version_id) VALUES('publish',?,?)",(batch,version))
            if _fail_for_test: raise RuntimeError('injected publication failure')
            con.commit()
            return version
        except BaseException:
            con.rollback(); raise
        finally: con.close()

    def versions(self):
        with _connect(self.serving_path) as con:
            active=con.execute('SELECT version_id FROM active WHERE id=1').fetchone()[0]
            return [{'id':r[0], 'created':r[1], 'active':r[0]==active} for r in con.execute('SELECT id,created FROM versions ORDER BY id DESC')]

    def rollback(self, version):
        con=self._transaction()
        try:
            if not con.execute('SELECT 1 FROM versions WHERE id=?',(version,)).fetchone(): raise ValueError('Versão inexistente')
            con.execute('UPDATE active SET version_id=? WHERE id=1',(version,))
            con.execute("INSERT INTO admin.events(action,version_id) VALUES('activate',?)",(version,))
            con.commit()
        except BaseException:
            con.rollback(); raise
        finally: con.close()
