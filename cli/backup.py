"""
Create a consistent backup of the PULSE database (VACUUM INTO — safe while
the app is running, unlike copying ledger.db under WAL).

Run: python3 cli/backup.py
     python3 cli/backup.py --reason pre-upgrade
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.backup import create_backup, KEEP_BACKUPS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reason", default="manual", help="short label embedded in the filename")
    args = parser.parse_args()
    dest = create_backup(args.reason)
    print(f"Backup written: {dest}")
    print(f"(Keeping the {KEEP_BACKUPS} newest backups; older ones are pruned.)")


if __name__ == "__main__":
    main()
