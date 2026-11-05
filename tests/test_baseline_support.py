import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from test_data import write_dataset
from artefact.data import StructuralError
from baseline_support import (
    BASELINE_DIR_ENV,
    assert_baseline_unchanged,
    baseline_sha256,
    load_original_baseline,
    resolve_original_baseline_dir,
)


class BaselineSupportTests(unittest.TestCase):
    def test_missing_baseline_configuration_is_an_explicit_skip(self):
        with patch.dict(os.environ):
            os.environ.pop(BASELINE_DIR_ENV, None)
            with self.assertRaises(unittest.SkipTest) as raised:
                resolve_original_baseline_dir(self)

        self.assertIn(BASELINE_DIR_ENV, str(raised.exception))

    def test_invalid_baseline_directory_fails_without_echoing_its_path(self):
        with TemporaryDirectory() as directory:
            configured = str(Path(directory) / 'private-do-not-disclose')
            with patch.dict(os.environ, {BASELINE_DIR_ENV: configured}):
                with self.assertRaises(AssertionError) as raised:
                    resolve_original_baseline_dir(self)

        self.assertIn(BASELINE_DIR_ENV, str(raised.exception))
        self.assertNotIn(configured, str(raised.exception))

    def test_invalid_dataset_fails_validation_instead_of_skipping(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_dataset(root)
            for source in root.glob('*.csv'):
                source.rename(root / f'desafio_tecnico_ai_eng - {source.name}')
            with patch.dict(os.environ, {BASELINE_DIR_ENV: str(root)}):
                with self.assertRaises(StructuralError) as raised:
                    load_original_baseline(self)

        self.assertNotIn(str(root), str(raised.exception))
        self.assertNotIn('Synthetic Customer', str(raised.exception))

    def test_changed_csv_reports_generic_integrity_failure_only(self):
        marker = 'Synthetic Private Customer Marker'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            csv_path = root / 'customers.csv'
            csv_path.write_text(marker, encoding='utf-8')
            before = baseline_sha256(root)
            csv_path.write_text(marker + ' changed', encoding='utf-8')

            with self.assertRaises(AssertionError) as raised:
                assert_baseline_unchanged(self, before, root)

        message = str(raised.exception)
        self.assertEqual(message, 'Original CSV baseline integrity changed during the test.')
        self.assertNotIn(marker, message)
        self.assertTrue(all(digest not in message for digest in before.values()))
