"""Validate and register component schemas inside the caller's transaction."""
import re
import sqlite3

def _canonical(sql):
    return re.sub(r'\s+', '', re.sub(r'\bIF\s+NOT\s+EXISTS\b', '', sql, flags=re.I)).casefold()

def ensure_schema(db, component, version, tables):
    if not isinstance(component, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', component):
        raise ValueError('Invalid schema component')
    if type(version) is not int or version < 1 or not tables:
        raise ValueError('Invalid schema version')
    reference = sqlite3.connect(':memory:')
    try:
        for name, ddl in tables.items():
            if not re.fullmatch(r'[a-z][a-z0-9_]*', name): raise ValueError('Invalid table name')
            reference.execute(ddl)
        expected = dict(reference.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        if set(expected) != set(tables): raise ValueError('Invalid schema definition')
    finally:
        reference.close()
    db.execute('CREATE TABLE IF NOT EXISTS artefact_schema_versions (component TEXT PRIMARY KEY, version INTEGER NOT NULL)')
    recorded = db.execute('SELECT version FROM artefact_schema_versions WHERE component=?', (component,)).fetchone()
    if recorded and recorded[0] != version:
        raise ValueError('Unsupported schema version; explicit migration required')
    existing = {name: db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() for name in tables}
    present = sum(row is not None for row in existing.values())
    if present and present != len(tables):
        raise ValueError('Partial schema; explicit recovery required')
    for name, row in existing.items():
        if row is not None and _canonical(row[0]) != _canonical(expected[name]):
            raise ValueError('Incompatible schema; explicit migration required')
    if not present:
        if recorded: raise ValueError('Missing schema; explicit recovery required')
        for ddl in tables.values(): db.execute(ddl)
    if not recorded:
        db.execute('INSERT INTO artefact_schema_versions VALUES (?,?)', (component, version))
