"""Verify immutable input files before loading them."""

import hashlib
from pathlib import Path
from typing import Mapping


def verify_files(expected: Mapping[Path, str]) -> dict[str, str]:
    failures = {}
    for path, digest in expected.items():
        try:
            with path.open('rb') as source:
                actual = hashlib.file_digest(source, 'sha256').hexdigest()
        except FileNotFoundError:
            failures[str(path)] = 'missing'
            continue
        if actual.lower() != digest.lower():
            failures[str(path)] = 'mismatch'
    return failures
