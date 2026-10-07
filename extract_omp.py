#!/usr/bin/env python3
"""Extract OMP sessions through the shared Pi parser; history is opt-in and separate."""
import argparse
import json
from pathlib import Path
import sqlite3
from datetime import datetime
from extract_pi import run, find_installations


def extract_prompt_history(root):
    """Extract the standalone prompt history database (best effort).

    omp runs history.db in WAL mode. sqlite3.Connection.backup() copies under a
    read transaction, so the snapshot cannot mix an old main file with a rotated
    WAL. Live files are never written.
    """
    db = root / 'history.db'
    if not db.exists():
        return []
    rows = []
    src = snap = None
    try:
        src = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        snap = sqlite3.connect(':memory:')
        src.backup(snap)
        src.close()
        src = None
        snap.row_factory = sqlite3.Row
        for r in snap.execute('SELECT * FROM history'):
            rows.append(dict(r))
    except (sqlite3.Error, OSError) as e:
        print(f'   ⚠️  history.db unreadable: {e}')
    finally:
        if src is not None:
            src.close()
        if snap is not None:
            snap.close()
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--all-branches', action='store_true')
    p.add_argument('--prompt-history', action='store_true', help='also export a read-only history snapshot separately')
    args = p.parse_args()
    run('omp', 'omp', ['.omp'], 'OMP_CODING_AGENT_DIR', 'omp', args.all_branches)
    if args.prompt_history:
        rows = [r for root in find_installations(['.omp'], 'OMP_CODING_AGENT_DIR') for r in extract_prompt_history(root.parent)]
        if rows:
            out = Path('extracted_data'); out.mkdir(exist_ok=True)
            path = out / ('omp_prompt_history_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.json')
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(f'Prompt history rows: {len(rows)}; saved separately to {path}')


if __name__ == '__main__':
    main()
