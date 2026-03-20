#!/usr/bin/env python3
"""
tools/calibration_report.py – Print a human-readable calibration report.

Usage:
    python tools/calibration_report.py [--db data/calibration.db] [--top 20]

Shows:
  - Overall Brier score vs market baseline
  - Breakdown by probability bucket (calibration curve)
  - Recent predictions (last N rows)
  - Markets awaiting resolution
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

_DEFAULT_DB = Path("data/calibration.db")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _overall(conn: sqlite3.Connection) -> None:
    total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    resolved = conn.execute(
        "SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL"
    ).fetchone()[0]
    pending = total - resolved

    print("=" * 65)
    print("  CALIBRATION REPORT")
    print("=" * 65)
    print(f"  Total predictions logged : {total}")
    print(f"  Resolved                 : {resolved}")
    print(f"  Awaiting resolution      : {pending}")

    if resolved == 0:
        print("\n  No resolved markets yet — Brier score unavailable.")
        return

    rows = conn.execute(
        "SELECT our_prob, market_price, outcome, brier FROM predictions "
        "WHERE outcome IS NOT NULL"
    ).fetchall()

    n = len(rows)
    brier_ours   = sum(r["brier"] for r in rows) / n
    brier_market = sum((r["market_price"] - r["outcome"]) ** 2 for r in rows) / n
    edge         = brier_market - brier_ours

    verdict = (
        "BETTER than market ✓" if edge > 0.005
        else "WORSE than market ✗" if edge < -0.005
        else "ON PAR with market ~"
    )

    print(f"\n  Our Brier score          : {brier_ours:.4f}  (lower = better)")
    print(f"  Market Brier score       : {brier_market:.4f}")
    print(f"  Edge vs market           : {edge:+.4f}")
    print(f"  Verdict                  : {verdict}")

    # Accuracy
    correct = sum(
        1 for r in rows
        if (r["our_prob"] >= 0.5 and r["outcome"] == 1.0) or
           (r["our_prob"] <  0.5 and r["outcome"] == 0.0)
    )
    print(f"  Directional accuracy     : {correct}/{n} = {correct/n*100:.1f}%")


def _calibration_curve(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT our_prob, outcome FROM predictions WHERE outcome IS NOT NULL"
    ).fetchall()
    if not rows:
        return

    # 10-bucket calibration curve
    buckets: dict[int, list] = {i: [] for i in range(10)}
    for r in rows:
        bucket = min(int(r["our_prob"] * 10), 9)
        buckets[bucket].append(r["outcome"])

    print("\n  CALIBRATION CURVE  (predicted → actual)")
    print("  " + "-" * 50)
    for i in range(10):
        lo = i * 10
        hi = lo + 10
        outcomes = buckets[i]
        if not outcomes:
            print(f"  {lo:2d}–{hi:2d}%   no data")
            continue
        actual = sum(outcomes) / len(outcomes) * 100
        bar_len = int(actual / 2)
        bar = "█" * bar_len + "░" * (50 - bar_len)
        print(f"  {lo:2d}–{hi:2d}%  {actual:5.1f}% actual  n={len(outcomes):3d}  |{bar[:25]}|")


def _recent(conn: sqlite3.Connection, n: int) -> None:
    rows = conn.execute(
        "SELECT ts, question, our_prob, market_price, direction, edge, "
        "       bet_placed, bet_size, outcome, brier "
        "FROM predictions ORDER BY id DESC LIMIT ?",
        (n,),
    ).fetchall()

    if not rows:
        return

    print(f"\n  RECENT {n} PREDICTIONS")
    print("  " + "-" * 100)
    header = f"  {'Date':10s}  {'Question':42s}  {'Ours':5s}  {'Mkt':5s}  {'Dir':3s}  {'Edge':6s}  {'Bet?':4s}  {'$':5s}  {'Out':3s}  {'Brier':6s}"
    print(header)
    print("  " + "-" * 100)

    for r in rows:
        ts    = r["ts"][:10]
        q     = r["question"][:42]
        ours  = f"{r['our_prob']*100:.0f}%"
        mkt   = f"{r['market_price']*100:.0f}%"
        edge  = f"{r['edge']*100:+.1f}%"
        bet   = "YES" if r["bet_placed"] else " no"
        size  = f"${r['bet_size']:.2f}" if r["bet_size"] else "    -"
        out   = f"{int(r['outcome'])}" if r["outcome"] is not None else " ?"
        brier = f"{r['brier']:.4f}" if r["brier"] is not None else "  n/a"
        print(f"  {ts}  {q:<42s}  {ours:>5s}  {mkt:>5s}  {r['direction']:>3s}  {edge:>6s}  {bet:>4s}  {size:>5s}  {out:>3s}  {brier:>6s}")


def _pending(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT DISTINCT market_id, question, COUNT(*) as cnt "
        "FROM predictions WHERE outcome IS NULL "
        "GROUP BY market_id ORDER BY cnt DESC LIMIT 30"
    ).fetchall()

    if not rows:
        return

    print(f"\n  MARKETS AWAITING RESOLUTION ({len(rows)} unique)")
    print("  " + "-" * 70)
    for r in rows:
        print(f"  {r['market_id'][:16]}  {r['question'][:50]}  (n={r['cnt']})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Print calibration report")
    ap.add_argument("--db",  default=str(_DEFAULT_DB), help="Path to calibration.db")
    ap.add_argument("--top", type=int, default=20, help="Recent predictions to show")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}")
        print("The agent must run at least one cycle before data is available.")
        sys.exit(1)

    conn = _connect(db_path)
    _overall(conn)
    _calibration_curve(conn)
    _recent(conn, args.top)
    _pending(conn)
    print("\n" + "=" * 65)
    conn.close()


if __name__ == "__main__":
    main()
