import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from artefact.integrity import verify_files


class IntegrityTests(unittest.TestCase):
    def test_valid_and_tampered_files(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'catalog.csv'
            path.write_bytes(b'synthetic data')
            expected = hashlib.sha256(b'synthetic data').hexdigest()
            self.assertEqual(verify_files({path: expected}), {})
            path.write_bytes(b'tampered')
            self.assertEqual(verify_files({path: expected}), {str(path): 'mismatch'})

    def test_missing_file_is_reported(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.csv'
            self.assertEqual(verify_files({path: '0' * 64}), {str(path): 'missing'})
