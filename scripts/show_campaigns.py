"""Print captured campaigns from the DB."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Repository


def main() -> None:
    r = Repository()
    with r.conn() as c:
        rows = c.execute(
            "SELECT id, community_id, title, payout_per_1k_views, "
            "       budget_remaining_pct, viability_score, budget_total "
            "FROM campaigns WHERE community_id = ? "
            "ORDER BY viability_score DESC NULLS LAST, id DESC",
            ("clip-farm-official",),
        ).fetchall()
    print(f"\n{len(rows)} clip-farm-official campaign(s):\n")
    print(f"{'id':>4}  {'cpm':<10}  {'budget left':<13}  {'score':<7}  {'budget':<14}  title")
    print("-" * 90)
    for r2 in rows:
        cpm = f"${r2['payout_per_1k_views']:.2f}/1k" if r2["payout_per_1k_views"] else "?"
        pct = f"{r2['budget_remaining_pct']:.0f}%" if r2["budget_remaining_pct"] is not None else "?"
        score = f"{r2['viability_score']:.0f}" if r2["viability_score"] is not None else "?"
        budget = f"${r2['budget_total']:,.0f}" if r2["budget_total"] else "?"
        print(f"{r2['id']:>4}  {cpm:<10}  {pct:<13}  {score:<7}  {budget:<14}  {r2['title']}")


if __name__ == "__main__":
    main()
