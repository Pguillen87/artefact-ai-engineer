"""Administrative staging and a separate, published-only catalog projection."""
from __future__ import annotations
import csv
import io
import hashlib
from hmac import compare_digest as hmac_compare
import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from .data import SCHEMAS, Snapshot, StructuralError, _freeze, _parse, _scan

MAX_BYTES = 1024 * 1024
MAX_ROWS = 1000
DEFAULT_SELLER_CONTACT = '(67) 3341-4444'


def _validate_contact(value):
    if not isinstance(value,str) or not re.fullmatch(r'\([1-9][0-9]\) [2-9][0-9]{3,4}-[0-9]{4}',value):
        raise ValueError('Contato inválido; use (DD) NNNN-NNNN ou (DD) NNNNN-NNNN')
    return value


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
    try:
        reader=csv.reader(io.StringIO(raw.decode('utf-8-sig'),newline=''),strict=True)
        for line,_ in enumerate(reader):
            if line > MAX_ROWS: raise ValueError('O lote excede 1000 produtos')
        rows = _parse('products', raw)
    except (UnicodeError,csv.Error,RecursionError) as exc:
        raise StructuralError('CSV ou JSON inválido ou complexo demais') from exc
    for row in rows:
        stack=[(row['specs'],0)]; nodes=0
        while stack:
            value,depth=stack.pop();nodes+=1
            if depth>32 or nodes>10000: raise ValueError('Especificações complexas demais')
            if isinstance(value,dict): stack.extend((v,depth+1) for v in value.values())
            elif isinstance(value,list): stack.extend((v,depth+1) for v in value)
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
    con.execute('PRAGMA synchronous=FULL')
    return con


def _recover_existing(admin_path, serving_path):
    """Trigger SQLite hot-journal recovery without allowing missing files to be created."""
    con = None
    try:
        con = sqlite3.connect(serving_path.as_uri() + '?mode=rw', uri=True, timeout=10)
        con.execute('ATTACH DATABASE ? AS admin', (admin_path.as_uri() + '?mode=rw',))
        # Opening a handle alone does not force SQLite to read and recover its journals.
        con.execute('BEGIN')
        con.execute('SELECT count(*) FROM main.sqlite_master').fetchone()
        con.execute('SELECT count(*) FROM admin.sqlite_master').fetchone()
        con.commit()
    except sqlite3.Error:
        if con is not None: con.rollback()
        raise sqlite3.OperationalError('Falha na recuperação dos bancos administrativos') from None
    finally:
        if con is not None: con.close()


def published_snapshot(path, baseline):
    """Read one active immutable version; never opens the administration database."""
    path = Path(path)
    if not path.exists(): raise FileNotFoundError("Catálogo publicado ausente")
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    try:
        con.execute('BEGIN')
        row = con.execute('SELECT v.products,v.id FROM active a JOIN versions v ON v.id=a.version_id WHERE a.id=1').fetchone()
        if row is None: raise ValueError('Catálogo publicado inválido')
        exists=con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_settings'").fetchone()
        setting=con.execute("SELECT value FROM public_settings WHERE key='seller_contact'").fetchone() if exists else None
        contact=_validate_contact(setting[0]) if setting else DEFAULT_SELLER_CONTACT
        products = _parse('products', row[0])
        tables = dict(baseline.tables)
        tables['products'] = tuple(baseline.tables['products']) + tuple(_freeze(p) for p in products)
        issues = baseline.issues + _scan(products, baseline.tables['categories'])
        return Snapshot(MappingProxyType(tables), issues, baseline.hashes, contact, row[1])
    finally:
        con.close()


def require_explicit_initialization(admin_path, serving_path, initialize):
    if not Path(admin_path).exists() and not Path(serving_path).exists() and not initialize:
        raise ValueError('Inicialização do catálogo exige ARTEFACT_ADMIN_INITIALIZE=1')


class CatalogAdmin:
    def __init__(self, admin_path, serving_path, baseline):
        self.admin_path = Path(admin_path).resolve()
        self.serving_path = Path(serving_path).resolve()
        self.baseline = baseline
        if self.admin_path == self.serving_path: raise ValueError('Bancos devem ser separados')
        if self.admin_path.exists() != self.serving_path.exists(): raise ValueError('Um dos bancos persistentes está ausente')
        existing = self.admin_path.exists()
        if existing:
            _recover_existing(self.admin_path, self.serving_path)
            for path, tables in ((self.admin_path, {'batches','events'}),(self.serving_path, {'versions','active'})):
                check=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
                try:
                    names={r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not tables <= names: raise ValueError('Banco persistente incompleto')
                    if path == self.serving_path and not check.execute('SELECT 1 FROM active a JOIN versions v ON v.id=a.version_id WHERE a.id=1').fetchone():
                        raise ValueError('Versão ativa ausente')
                finally: check.close()
        self.admin_path.parent.mkdir(parents=True, exist_ok=True)
        self.serving_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.admin_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS batches (id INTEGER PRIMARY KEY, csv BLOB NOT NULL, status TEXT NOT NULL, review TEXT NOT NULL DEFAULT "", created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            con.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, action TEXT NOT NULL, batch_id INTEGER, version_id INTEGER, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            columns={r[1] for r in con.execute('PRAGMA table_info(events)')}
            if 'reason' not in columns: con.execute("ALTER TABLE events ADD COLUMN reason TEXT NOT NULL DEFAULT ''")
            columns={r[1] for r in con.execute('PRAGMA table_info(batches)')}
            if 'approved_hash' not in columns: con.execute("ALTER TABLE batches ADD COLUMN approved_hash TEXT NOT NULL DEFAULT ''")
            con.execute("CREATE TABLE IF NOT EXISTS technical_reviews (id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, field TEXT NOT NULL, reason TEXT NOT NULL, catalog_version INTEGER NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            con.execute("CREATE INDEX IF NOT EXISTS technical_reviews_open_lookup ON technical_reviews(product_id,field,catalog_version) WHERE status='open'")
            con.execute('CREATE TABLE IF NOT EXISTS decisions (batch_id INTEGER NOT NULL REFERENCES batches(id), issue_id TEXT NOT NULL, evidence TEXT NOT NULL, justification TEXT NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(batch_id,issue_id))')
        with _connect(self.serving_path) as con:
            con.execute('CREATE TABLE IF NOT EXISTS public_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            con.execute('CREATE TABLE IF NOT EXISTS versions (id INTEGER PRIMARY KEY, products BLOB NOT NULL, created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
            con.execute('CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY CHECK(id=1), version_id INTEGER NOT NULL REFERENCES versions(id))')
            con.execute('INSERT OR IGNORE INTO versions(id,products) VALUES(0,?)', (_encode_products([]),))
            con.execute('INSERT OR IGNORE INTO active(id,version_id) VALUES(1,0)')

    def create_review(self, product_id, field, reason, catalog_version):
        allowed={'brand','category_id','specs.keys','specs.strings'}
        if type(product_id) is not int or product_id <= 0 or type(catalog_version) is not int or catalog_version < 0 or field not in allowed or reason != 'inconsistencia_catalogo':
            raise ValueError('Solicitação técnica inválida')
        con=self._transaction()
        try:
            row=con.execute('SELECT v.id,v.products FROM active a JOIN versions v ON v.id=a.version_id WHERE a.id=1').fetchone()
            if row is None or row[0] != catalog_version: raise ValueError('Versão do catálogo mudou')
            products=list(self.baseline.tables['products']) + _parse('products',row[1])
            if not any(p['product_id']==product_id for p in products): raise ValueError('Produto inexistente')
            if not any(i.product_id==product_id and i.field==field for i in _scan(products,self.baseline.tables['categories'])):
                raise ValueError('Campo sem conflito registrado')
            existing=con.execute("SELECT id FROM admin.technical_reviews WHERE product_id=? AND field=? AND catalog_version=? AND status='open' ORDER BY id LIMIT 1",(product_id,field,catalog_version)).fetchone()
            if existing:
                con.commit();return existing[0]
            case=con.execute("INSERT INTO admin.technical_reviews(product_id,field,reason,catalog_version,status) VALUES(?,?,?,?, 'open')",(product_id,field,reason,catalog_version)).lastrowid
            con.commit();return case
        except BaseException:
            con.rollback();raise
        finally: con.close()

    def review_cases(self):
        with _connect(self.admin_path) as con:
            keys=('id','product_id','field','reason','catalog_version','status','created','updated')
            return [dict(zip(keys,r)) for r in con.execute('SELECT id,product_id,field,reason,catalog_version,status,created,updated FROM technical_reviews ORDER BY id DESC LIMIT 100')]

    def complete_review(self, case):
        if type(case) is not int or case <= 0: raise ValueError('Caso inválido')
        with _connect(self.admin_path) as con:
            result=con.execute("UPDATE technical_reviews SET status='completed',updated=CURRENT_TIMESTAMP WHERE id=? AND status='open'",(case,))
            if result.rowcount != 1: raise ValueError('Caso inexistente ou concluído')

    def seller_contact(self):
        with _connect(self.serving_path) as con:
            row=con.execute("SELECT value FROM public_settings WHERE key='seller_contact'").fetchone()
            return _validate_contact(row[0]) if row else DEFAULT_SELLER_CONTACT

    def set_seller_contact(self, contact):
        contact=_validate_contact(contact)
        con=self._transaction()
        try:
            previous=con.execute("SELECT value FROM public_settings WHERE key='seller_contact'").fetchone()
            con.execute("INSERT INTO public_settings(key,value) VALUES('seller_contact',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(contact,))
            reason=json.dumps({'before':previous[0] if previous else DEFAULT_SELLER_CONTACT,'after':contact},ensure_ascii=False)
            con.execute("INSERT INTO admin.events(action,reason) VALUES('seller_contact',?)",(reason,))
            con.commit()
        except BaseException:
            con.rollback();raise
        finally: con.close()

    def reset_seller_contact(self):
        self.set_seller_contact(DEFAULT_SELLER_CONTACT)

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

    def _issues(self, batch, raw):
        products = _parse('products', raw)
        result=[]
        for issue in _scan(products,self.baseline.tables['categories']):
            item=asdict(issue)
            identity=json.dumps([batch,item],sort_keys=True,ensure_ascii=True,separators=(',',':'))
            item['id']=hashlib.sha256(identity.encode()).hexdigest()
            result.append(item)
        return products,result

    def preview(self, batch):
        with _connect(self.admin_path) as con:
            row = con.execute('SELECT csv,status,review FROM batches WHERE id=?',(batch,)).fetchone()
            if not row: raise ValueError('Lote inexistente')
            decisions={r[0]:r[1] for r in con.execute('SELECT issue_id,justification FROM decisions WHERE batch_id=?',(batch,))}
        products,issues=self._issues(batch,row[0])
        for issue in issues: issue['decision']=decisions.get(issue['id'])
        return {'status':row[1], 'review':row[2], 'products':products,'issues':issues}

    def decide(self, batch, issue_id, decision, justification):
        if decision != 'accept': raise ValueError('Para corrigir, envie um novo lote corrigido')
        if not isinstance(justification,str) or not justification.strip() or len(justification)>1000: raise ValueError('Justificativa necessária')
        with _connect(self.admin_path) as con:
            row=con.execute("SELECT csv FROM batches WHERE id=? AND status='staged'",(batch,)).fetchone()
            if not row: raise ValueError('Lote não está em revisão')
            issues=self._issues(batch,row[0])[1]
            issue=next((i for i in issues if i['id']==issue_id),None)
            if not issue: raise ValueError('Conflito desatualizado')
            evidence=json.dumps(issue,sort_keys=True,ensure_ascii=False)
            con.execute('INSERT OR REPLACE INTO decisions(batch_id,issue_id,evidence,justification) VALUES(?,?,?,?)',(batch,issue_id,evidence,justification.strip()))
            con.execute("INSERT INTO events(action,batch_id,reason) VALUES('accept_conflict',?,?)",(batch,json.dumps({'issue':issue,'justification':justification.strip()},ensure_ascii=False)))

    def _check_decisions(self, con, schema, batch, raw):
        issues=self._issues(batch,raw)[1]
        decisions={r[0] for r in con.execute(f'SELECT issue_id FROM {schema}.decisions WHERE batch_id=?',(batch,))}
        if {i['id'] for i in issues} != decisions: raise ValueError('Há conflitos pendentes ou decisões desatualizadas')

    def approve(self, batch, review):
        if not isinstance(review,str) or not review.strip() or len(review)>1000: raise ValueError('Informe a revisão humana')
        with _connect(self.admin_path) as con:
            row=con.execute("SELECT csv FROM batches WHERE id=? AND status='staged'",(batch,)).fetchone()
            if not row: raise ValueError('Lote não está aguardando revisão')
            self._check_decisions(con,'main',batch,row[0])
            digest=hashlib.sha256(row[0]).hexdigest()
            con.execute("UPDATE batches SET status='approved',review=?,approved_hash=? WHERE id=?",(review.strip(),digest,batch))
            con.execute("INSERT INTO events(action,batch_id,reason) VALUES('approve',?,?)",(batch,review.strip()))

    def history(self):
        with _connect(self.admin_path) as con:
            return [dict(zip(('action','batch_id','version_id','reason','created'),r)) for r in con.execute('SELECT action,batch_id,version_id,reason,created FROM events ORDER BY id')]

    def _transaction(self):
        con=_connect(self.serving_path)
        con.execute('ATTACH DATABASE ? AS admin', (str(self.admin_path),))
        con.execute('PRAGMA admin.journal_mode=DELETE')
        con.execute('PRAGMA admin.synchronous=FULL')
        con.execute('BEGIN IMMEDIATE')
        return con

    def publish(self, batch, *, _fail_for_test=False):
        con=self._transaction()
        try:
            row=con.execute("SELECT csv,approved_hash FROM admin.batches WHERE id=? AND status='approved'",(batch,)).fetchone()
            if not row: raise ValueError('Aprovação humana necessária')
            if not hmac_compare(row[1],hashlib.sha256(row[0]).hexdigest()): raise ValueError('Lote aprovado foi alterado')
            self._check_decisions(con,'admin',batch,row[0])
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
            return [{'id':r[0], 'created':r[1], 'active':bool(r[2])} for r in con.execute('SELECT v.id,v.created,v.id=a.version_id FROM versions v CROSS JOIN active a WHERE a.id=1 ORDER BY v.id DESC')]

    def rollback(self, version, reason):
        if not isinstance(reason,str) or not reason.strip() or len(reason)>1000: raise ValueError('Motivo obrigatório')
        con=self._transaction()
        try:
            if not con.execute('SELECT 1 FROM versions WHERE id=?',(version,)).fetchone(): raise ValueError('Versão inexistente')
            con.execute('UPDATE active SET version_id=? WHERE id=1',(version,))
            con.execute("INSERT INTO admin.events(action,version_id,reason) VALUES('activate',?,?)",(version,reason.strip()))
            con.commit()
        except BaseException:
            con.rollback(); raise
        finally: con.close()
