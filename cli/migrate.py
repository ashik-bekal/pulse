"""
Apply pending schema migrations to the PULSE database.
Run: python3 cli/migrate.py          (applies)
     python3 cli/migrate.py --check  (lists pending, applies nothing, exit 1 if any)
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from persistence.database import get_connection, get_db_path
from persistence.migrations import apply_migrations, pending_migrations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="list pending migrations without applying")
    args = parser.parse_args()

    conn = get_connection()
    if args.check:
        pending = pending_migrations(conn)
        if pending:
            print(f"Pending migrations for {get_db_path()}:")
            for f in pending:
                print(f"  {f}")
            sys.exit(1)
        print("No pending migrations.")
        return
    applied = apply_migrations(conn)
    if applied:
        print(f"Applied to {get_db_path()}:")
        for f in applied:
            print(f"  {f}")
    else:
        print("No pending migrations.")
    conn.close()


if __name__ == "__main__":
    main()
