"""Shared helpers for optional checks against the private original CSV baseline."""

import hashlib
import os
from pathlib import Path

from artefact.data import load_dataset


BASELINE_DIR_ENV = 'ARTEFACT_BASELINE_DATA_DIR'


def resolve_original_baseline_dir(test_case) -> Path:
    configured = os.environ.get(BASELINE_DIR_ENV)
    if configured is None:
        test_case.skipTest(
            f'{BASELINE_DIR_ENV} is not configured; original baseline is optional'
        )
    if not configured:
        test_case.fail(f'{BASELINE_DIR_ENV} is set but empty')

    directory = Path(configured).expanduser()
    if not directory.is_dir():
        test_case.fail(
            f'{BASELINE_DIR_ENV} is set but does not identify an available directory'
        )
    return directory


def load_original_baseline(test_case):
    directory = resolve_original_baseline_dir(test_case)
    return load_dataset(directory, verify_baseline=True)


def baseline_sha256(directory: Path) -> dict[str, str]:
    digests = {}
    for path in sorted(Path(directory).glob('*.csv'), key=lambda item: item.name):
        digest = hashlib.sha256()
        try:
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(chunk)
        except OSError:
            raise RuntimeError(
                'Unable to read the configured CSV baseline for integrity verification.'
            ) from None
        digests[path.name] = digest.hexdigest()
    return digests


def assert_baseline_unchanged(test_case, before: dict[str, str], directory: Path) -> None:
    after = baseline_sha256(directory)
    if before != after:
        test_case.fail('Original CSV baseline integrity changed during the test.')
