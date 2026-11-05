"""Persistent provider settings, with encrypted credentials and explicit activation."""
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sqlite3
import json
import os
from urllib.parse import urlparse
from contextlib import contextmanager

@contextmanager
def connection(path, **kwargs):
    db = sqlite3.connect(path, **kwargs)
    try:
        with db:
            yield db
    finally:
        db.close()
from cryptography.fernet import Fernet
from .secrets_store import SecretStore

# Explicitly curated for tool calling; update after checking the providers' official docs.
MODELS = {'google': ('gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-2.5-flash'),
          'openai': ('gpt-4.1-mini', 'gpt-4.1-nano')}

class ProviderConfig:
    def __init__(self, db_path, master_key_path, *, catalog_path=None):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.catalog_path = Path(catalog_path or os.environ['ARTEFACT_MODEL_CATALOG_PATH']) if catalog_path or os.environ.get('ARTEFACT_MODEL_CATALOG_PATH') else None
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            from .migrations import ensure_schema
            ensure_schema(db, 'providers', 1, {
                'provider_settings': 'CREATE TABLE IF NOT EXISTS provider_settings (provider TEXT PRIMARY KEY, model TEXT NOT NULL, secret BLOB, max_calls INTEGER NOT NULL, max_usd TEXT NOT NULL, input_price TEXT NOT NULL, output_price TEXT NOT NULL)',
                'provider_verified': 'CREATE TABLE IF NOT EXISTS provider_verified (provider TEXT PRIMARY KEY, model TEXT NOT NULL, secret BLOB NOT NULL)',
                'provider_active': 'CREATE TABLE IF NOT EXISTS provider_active (id INTEGER PRIMARY KEY CHECK (id=1), provider TEXT)',
            })
            exists = db.execute('SELECT EXISTS(SELECT 1 FROM provider_settings WHERE secret IS NOT NULL)').fetchone()[0]
            self.secrets = SecretStore(master_key_path, existing_credentials=bool(exists))

    def models(self):
        if self.catalog_path is None:
            return {name: list(models) for name, models in MODELS.items()}
        try:
            raw = json.loads(self.catalog_path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or set(raw) != {'google', 'openai'}:
                raise ValueError()
            result = {}
            for provider, entries in raw.items():
                if not isinstance(entries, list) or not entries or len(entries) > 100:
                    raise ValueError()
                result[provider] = []
                for entry in entries:
                    if not isinstance(entry, dict) or set(entry) != {'id', 'function_calling', 'reference'}:
                        raise ValueError()
                    model, reference = entry['id'], entry['reference']
                    if not isinstance(model, str) or not model.strip() or len(model) > 128 or entry['function_calling'] is not True or not isinstance(reference, str):
                        raise ValueError()
                    parsed = urlparse(reference)
                    allowed = {'google': {'ai.google.dev'}, 'openai': {'platform.openai.com', 'developers.openai.com'}}
                    if parsed.scheme != 'https' or parsed.hostname not in allowed[provider] or parsed.username or parsed.password or model in result[provider]:
                        raise ValueError()
                    result[provider].append(model)
            return result
        except (OSError, ValueError, TypeError):
            raise ValueError('Invalid curated model catalog') from None

    def save(self, provider, model, api_key=None, max_calls=100, max_usd='1', input_price='1', output_price='5'):
        self._save(provider, model, api_key, dict(max_calls=max_calls, max_usd=max_usd,
                                               input_price=input_price, output_price=output_price))

    def save_partial(self, provider, model, api_key=None, **settings):
        """Save visible fields while retaining omitted limits, prices and credentials."""
        if set(settings) - {'max_calls', 'max_usd', 'input_price', 'output_price'}:
            raise ValueError('Unsupported provider setting')
        self._save(provider, model, api_key, settings, partial=True)

    def _save(self, provider, model, api_key, settings, *, partial=False):
        if model not in self.models().get(provider, ()):
            raise ValueError('Unsupported provider or model')
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip() or len(api_key)>4096):
            raise ValueError('Invalid API key')
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT secret, max_calls, max_usd, input_price, output_price, model FROM provider_settings WHERE provider=?', (provider,)).fetchone()
            merged = dict(zip(('max_calls','max_usd','input_price','output_price'),
                              existing[1:5] if partial and existing else (100, '1', '1', '5')))
            merged.update(settings)
            max_calls=merged['max_calls']
            try:
                values = [Decimal(str(merged[key])) for key in ('max_usd','input_price','output_price')]
                if any(not v.is_finite() or v <= 0 for v in values) or isinstance(max_calls, bool) or int(max_calls) != float(max_calls) or int(max_calls) < 1:
                    raise ValueError()
            except (ValueError, TypeError, InvalidOperation, OverflowError):
                raise ValueError('Positive finite limits and prices required') from None
            same_key = api_key is None or (partial and existing and existing[0] is not None
                                           and self.secrets.decrypt(existing[0]) == api_key.strip())
            # Keep ciphertext too: verification is bound to this exact encrypted value.
            secret = (existing[0] if existing else None) if same_key else self.secrets.encrypt(api_key.strip())
            if not (partial and existing and existing[5] == model and same_key):
                db.execute('DELETE FROM provider_verified WHERE provider=?', (provider,))
                db.execute('UPDATE provider_active SET provider=NULL WHERE provider=?', (provider,))
            db.execute('INSERT OR REPLACE INTO provider_settings VALUES (?,?,?,?,?,?,?)', (provider, model, secret, int(max_calls), *(str(v) for v in values)))

    def activate(self, provider):
        with connection(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT s.model, s.secret FROM provider_settings s JOIN provider_verified v ON v.provider=s.provider AND v.model=s.model AND v.secret=s.secret WHERE s.provider=?', (provider,)).fetchone()
            if not row or not row[1] or row[0] not in self.models().get(provider, ()):
                raise ValueError('Test the selected provider and model before activation')
            self.secrets.decrypt(row[1])
            db.execute('INSERT OR REPLACE INTO provider_active VALUES (1,?)', (provider,))

    def remove_key(self, provider):
        with connection(self.path) as db:
            db.execute('DELETE FROM provider_verified WHERE provider=?', (provider,))
            db.execute('UPDATE provider_settings SET secret=NULL WHERE provider=?', (provider,))
            db.execute('UPDATE provider_active SET provider=NULL WHERE provider=?', (provider,))

    def credentials(self, provider):
        with connection(self.path) as db:
            db.execute('BEGIN')
            row = db.execute('SELECT secret FROM provider_settings WHERE provider=?', (provider,)).fetchone()
            if not row or not row[0]: raise ValueError('Provider key missing')
            return self.secrets.decrypt(row[0])

    def status(self):
        with connection(self.path) as db:
            active = db.execute('SELECT provider FROM provider_active WHERE id=1').fetchone()
            rows = db.execute('SELECT s.provider, s.model, s.secret IS NOT NULL, s.max_calls, s.max_usd, s.input_price, s.output_price, EXISTS(SELECT 1 FROM provider_verified v WHERE v.provider=s.provider AND v.model=s.model AND v.secret=s.secret) FROM provider_settings s').fetchall()
        return {'active': active[0] if active else None, 'providers': {r[0]: dict(zip(('model','has_key','max_calls','max_usd','input_price','output_price','verified'), (r[1], bool(r[2]), *r[3:7], bool(r[7])))) for r in rows}}

    def test_connection(self, provider, state, *, client=None, transport=None):
        from .provider import test_connection
        with connection(self.path) as db:
            row = db.execute('SELECT model, secret FROM provider_settings WHERE provider=?', (provider,)).fetchone()
        if not row or not row[1]:
            raise ValueError('Provider key missing')
        result = test_connection(self, provider, row[0], state, explicit=True, client=client, transport=transport)
        with connection(self.path) as db:
            db.execute('INSERT OR REPLACE INTO provider_verified SELECT provider, model, secret FROM provider_settings WHERE provider=? AND model=? AND secret=?', (provider, row[0], row[1]))
        return result

    def rotate_master_key(self):
        # Commit a two-key ring before rewriting ciphertext; interruptions remain recoverable.
        with connection(self.path, timeout=30) as db:
            db.execute('BEGIN EXCLUSIVE')
            old_keys = self.secrets.path.read_bytes().splitlines()
            new_key = Fernet.generate_key()
            self.secrets.replace_keys([new_key, *old_keys])
            rows = db.execute('SELECT provider, secret FROM provider_settings WHERE secret IS NOT NULL').fetchall()
            db.execute('DELETE FROM provider_verified')
            db.execute('UPDATE provider_active SET provider=NULL')
            for provider, secret in rows:
                value = self.secrets.decrypt(secret)
                db.execute('UPDATE provider_settings SET secret=? WHERE provider=?', (self.secrets.encrypt(value), provider))
            db.commit()
            db.execute('BEGIN EXCLUSIVE')
            # A concurrent rotation may have replaced the ring after commit.
            if self.secrets.path.read_bytes().splitlines() == [new_key, *old_keys]:
                self.secrets.replace_keys([new_key])
