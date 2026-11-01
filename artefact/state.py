"""Durable conservative cost reservations and at-most-once update claims."""
from decimal import Decimal, ROUND_CEILING
from contextlib import closing
import sqlite3
import os
from pathlib import Path

class BudgetExhausted(RuntimeError):
    pass

def validate_state_path(path):
    resolved=Path(path).resolve()
    checkout=Path(__file__).resolve().parent.parent
    roots=[checkout]
    if os.environ.get('ARTEFACT_DATA_DIR'):
        roots.append(Path(os.environ['ARTEFACT_DATA_DIR']).resolve())
    if os.name=='nt':
        roots.append(Path(r'C:\App\Artefact\Desafio Técnico AI Engineer 2026\data').resolve())
    if any(resolved == root or root in resolved.parents for root in roots):
        raise ValueError('Operational database must be outside checkout and source data')
    return resolved

class State:
    def __init__(self, path):
        self.path=str(validate_state_path(path))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY CHECK(id=1), calls INTEGER NOT NULL, micros INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO usage VALUES (1,0,0)')
            db.execute('CREATE TABLE IF NOT EXISTS updates (update_id INTEGER PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS tokens (id INTEGER PRIMARY KEY CHECK(id=1), input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO tokens VALUES (1,0,0)')

    def reserve(self, estimated_usd, max_usd, max_calls):
        for amount in (estimated_usd,max_usd):
            if not isinstance(amount,Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError('invalid budget')
        if max_usd <= 0 or type(max_calls) is not int or max_calls <= 0:
            raise ValueError('positive budget and call limit required')
        micros=int((estimated_usd*1_000_000).to_integral_value(rounding=ROUND_CEILING))
        ceiling=int(max_usd*1_000_000)
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            calls,spent=db.execute('SELECT calls,micros FROM usage WHERE id=1').fetchone()
            if calls >= max_calls or spent+micros > ceiling:
                raise BudgetExhausted('API budget exhausted')
            db.execute('UPDATE usage SET calls=?,micros=? WHERE id=1',(calls+1,spent+micros))

    def usage(self):
        with closing(sqlite3.connect(self.path)) as db:
            calls,micros=db.execute('SELECT calls,micros FROM usage WHERE id=1').fetchone()
            input_tokens,output_tokens=db.execute('SELECT input_tokens,output_tokens FROM tokens WHERE id=1').fetchone()
        return {'calls':calls,'reserved_usd':Decimal(micros)/1_000_000,'input_tokens':input_tokens,'output_tokens':output_tokens}

    def record_tokens(self, input_tokens, output_tokens):
        if any(type(v) is not int or v < 0 for v in (input_tokens,output_tokens)):
            raise ValueError('invalid token counts')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE tokens SET input_tokens=input_tokens+?, output_tokens=output_tokens+? WHERE id=1',(input_tokens,output_tokens))

    def claim_update(self, update_id):
        if type(update_id) is not int or update_id < 0: raise ValueError('invalid update id')
        with closing(sqlite3.connect(self.path)) as db, db:
            result=db.execute('INSERT OR IGNORE INTO updates(update_id) VALUES (?)',(update_id,))
            return result.rowcount == 1
