"""Atomically create shared service secrets without exposing partial writes."""
import os
from pathlib import Path
import secrets
import tempfile

def bootstrap_secret(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd, name = tempfile.mkstemp(prefix='.secret-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='ascii') as stream:
                stream.write(secrets.token_urlsafe(36))
                stream.flush()
                os.fsync(stream.fileno())
            try: os.link(name, path)
            except FileExistsError: pass
        finally:
            os.unlink(name)
    value = path.read_text(encoding='ascii').strip()
    if not value: raise ValueError('Empty service secret')
    return value
