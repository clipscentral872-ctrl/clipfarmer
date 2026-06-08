"""CLI entry for initializing / migrating the SQLite database.

Usage:
    python -m db.migrations init        # create tables
    python -m db.migrations status      # show table list
"""

from __future__ import annotations

import sys

from .repository import DB_PATH, get_connection, init_db


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "init"
    if cmd == "init":
        init_db()
        print(f"Database initialized at {DB_PATH}")
        return 0
    if cmd == "status":
        with get_connection() as c:
            rows = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            for r in rows:
                print(r["name"])
        return 0
    print(f"Unknown command: {cmd}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
