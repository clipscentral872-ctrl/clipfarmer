"""Delete campaigns from old/exploratory scans that don't represent real
Content Rewards campaigns. Safe-keep anything we discovered through the
iframe scanner (which always sets a budget_total or viability_score).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Repository


def main() -> None:
    r = Repository()
    with r.conn() as c:
        deleted = c.execute(
            "DELETE FROM campaigns "
            "WHERE budget_total IS NULL AND viability_score IS NULL"
        ).rowcount
        kept = c.execute("SELECT COUNT(*) AS n FROM campaigns").fetchone()["n"]
    print(f"deleted {deleted} junk campaigns; {kept} real campaigns remain")


if __name__ == "__main__":
    main()
