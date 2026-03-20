"""
polymarket/calibration.py – Prediction calibration tracking.

Records every MiroFish prediction to a SQLite database.
Enables offline Brier score analysis and calibration plots.

Schema:
  predictions(id, ts, market_id, question, our_prob, market_price,
              direction, edge, bet_placed, bet_size, outcome, brier)

  outcome = 1.0 (YES resolved), 0.0 (NO resolved), NULL (unresolved)
  brier   = (our_prob - outcome)^2, filled in when outcome is known

Usage:
    from polymarket.calibration import CalibrationLog
    cal = CalibrationLog()
    cal.log(market_id, question, our_prob, market_price, direction, edge, bet_placed, bet_size)
    cal.resolve(market_id, outcome=1.0)
    print(cal.summary())
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path("data/calibration.db")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS predictions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    market_id   TEXT    NOT NULL,
    question    TEXT    NOT NULL,
    our_prob    REAL    NOT NULL,
    market_price REAL   NOT NULL,
    direction   TEXT    NOT NULL,
    edge        REAL    NOT NULL,
    bet_placed  INTEGER NOT NULL DEFAULT 0,
    bet_size    REAL,
    outcome     REAL,
    brier       REAL
);
CREATE INDEX IF NOT EXISTS idx_market_id ON predictions(market_id);
"""


class CalibrationLog:
    """Thread-safe (single-process) SQLite-backed prediction logger."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._path = db_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self._path))

    def _init_db(self) -> None:
        try:
            conn = self._connect()
            conn.executescript(_CREATE_SQL)
            conn.commit()
            conn.close()
        except Exception as exc:
            log.warning("[Calibration] DB init failed: %s", exc)

    # ── Write ────────────────────────────────────────────────────────────────

    def log(
        self,
        market_id: str,
        question: str,
        our_prob: float,
        market_price: float,
        direction: str,
        edge: float,
        bet_placed: bool = False,
        bet_size: float = 0.0,
    ) -> int:
        """Insert a prediction record. Returns the row id, or -1 on error."""
        try:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO predictions
                   (ts, market_id, question, our_prob, market_price,
                    direction, edge, bet_placed, bet_size)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    market_id,
                    question[:250],
                    round(our_prob, 4),
                    round(market_price, 4),
                    direction,
                    round(edge, 4),
                    int(bet_placed),
                    round(bet_size, 4),
                ),
            )
            conn.commit()
            conn.close()
            return cur.lastrowid
        except Exception as exc:
            log.warning("[Calibration] log() failed: %s", exc)
            return -1

    def resolve(self, market_id: str, outcome: float) -> int:
        """
        Mark all unresolved predictions for market_id with their outcome
        and compute their Brier scores.

        outcome: 1.0 = YES resolved, 0.0 = NO resolved.
        Returns number of rows updated.
        """
        try:
            conn = self._connect()
            # Fetch unresolved predictions for this market
            rows = conn.execute(
                "SELECT id, our_prob FROM predictions "
                "WHERE market_id=? AND outcome IS NULL",
                (market_id,),
            ).fetchall()
            if not rows:
                conn.close()
                return 0
            for row_id, our_prob in rows:
                brier = (our_prob - outcome) ** 2
                conn.execute(
                    "UPDATE predictions SET outcome=?, brier=? WHERE id=?",
                    (outcome, round(brier, 6), row_id),
                )
            conn.commit()
            conn.close()
            log.info(
                "[Calibration] Resolved %d records for %s (outcome=%.0f)",
                len(rows), market_id[:12], outcome,
            )
            return len(rows)
        except Exception as exc:
            log.warning("[Calibration] resolve() failed: %s", exc)
            return 0

    # ── Read ─────────────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """
        Return calibration statistics for all resolved predictions.

        Brier score interpretation:
          0.00  → perfect calibration
          0.25  → random (coin-flip) baseline
          >0.25 → worse than random
        """
        try:
            conn = self._connect()
            rows = conn.execute(
                "SELECT our_prob, market_price, outcome, brier FROM predictions "
                "WHERE outcome IS NOT NULL",
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            conn.close()

            if not rows:
                return {
                    "total_predictions": total,
                    "resolved": 0,
                    "brier_score": None,
                    "market_brier": None,
                    "edge_vs_market": None,
                }

            n = len(rows)
            brier_ours   = sum(r[3] for r in rows) / n
            brier_market = sum((r[1] - r[2]) ** 2 for r in rows) / n
            edge_vs_mkt  = brier_market - brier_ours  # positive = we beat market

            return {
                "total_predictions": total,
                "resolved": n,
                "brier_score": round(brier_ours, 4),
                "market_brier": round(brier_market, 4),
                "edge_vs_market": round(edge_vs_mkt, 4),
                "verdict": (
                    "BETTER than market" if edge_vs_mkt > 0.005
                    else "WORSE than market" if edge_vs_mkt < -0.005
                    else "ON PAR with market"
                ),
            }
        except Exception as exc:
            log.warning("[Calibration] summary() failed: %s", exc)
            return {"error": str(exc)}
