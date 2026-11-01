"""Print aggregate usage from an existing local state database."""
import argparse
import json
from pathlib import Path
from .state import State


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path',type=Path)
    args=parser.parse_args()
    if not args.path.is_file(): parser.error('state database does not exist')
    print(json.dumps(State(args.path).usage(),default=str))

if __name__=='__main__': main()
