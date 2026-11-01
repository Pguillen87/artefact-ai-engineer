"""Print an internal, data-minimized quality report; never print customer rows."""
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from .data import StructuralError, load_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--baseline', action='store_true', help='Require the six approved original hashes')
    args = parser.parse_args()
    try:
        snapshot = load_dataset(args.directory, verify_baseline=args.baseline)
    except StructuralError as exc:
        parser.exit(2, f'Cannot load dataset: {exc}\n')
    report = {'counts': {table: len(rows) for table, rows in snapshot.tables.items()},
              'hashes': dict(snapshot.hashes), 'issues': [asdict(i) for i in snapshot.issues]}
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
