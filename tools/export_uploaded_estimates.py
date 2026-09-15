"""Create an isolated legacy-format artifact before a controlled rollback."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from autobot.uploaded_estimates import export_legacy_snapshot

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Existing user_estimates directory')
    parser.add_argument('destination', type=Path, help='New directory outside the live source')
    args = parser.parse_args()
    count = export_legacy_snapshot(args.source, args.destination)
    print(f'Exported {count} estimates and the combined index. Originals remain in the source directory; no live files changed.')
