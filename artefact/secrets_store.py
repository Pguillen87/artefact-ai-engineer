"""Fernet keyring stored separately from the SQLite credential database."""
import os
from pathlib import Path
import tempfile
from cryptography.fernet import Fernet, MultiFernet

class SecretStore:
    def __init__(self, path, *, existing_credentials=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            if existing_credentials:
                raise ValueError('Master key missing: restore it from backup')
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(Fernet.generate_key() + b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
        self._fernet()

    def _fernet(self):
        try:
            keys = self.path.read_bytes().splitlines()
            if not keys: raise ValueError()
            return MultiFernet([Fernet(key) for key in keys])
        except (OSError, ValueError):
            raise ValueError('Master key unavailable or invalid') from None

    def encrypt(self, value):
        return self._fernet().encrypt(value.encode('utf-8'))

    def decrypt(self, value):
        from cryptography.fernet import InvalidToken
        try:
            return self._fernet().decrypt(value).decode('utf-8')
        except (InvalidToken, UnicodeDecodeError):
            raise ValueError('Credential cannot be decrypted; restore the matching master key') from None

    def replace_keys(self, keys):
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix='.master-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(b'\n'.join(keys) + b'\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name): os.unlink(name)
