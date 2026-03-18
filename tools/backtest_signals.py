#!/usr/bin/env python3
"""
tools/backtest_signals.py – Download historical Binance aggTrades and replay
the signal logic to find realistic threshold values.

Strategy: Binance Futures aggTrades are the «oracle» feed.
We have no historical WEEX data, so we simulate the WEEX latency by
artificially delaying the price seen by «WEEX» by LAG_MS milliseconds.
This approximates real latency arbitrage conditions.

Usage examples:
    # Download + analyse (default: BTCUSDT, last 2 hours)
    python tools/backtest_signals.py

    # Custom symbol, date range, lag
    python tools/backtest_signals.py --symbol ETHUSDT --hours 6 --lag 500

    # Use a cached CSV instead of downloading
    python tools/backtest_signals.py --csv data/BTCUSDT_agg.csv

    # Write a diagnostics.jsonl-compatible file for use with analyze_diag.py
    python tools/backtest_signals.py --out logs/backtest_diag.jsonl

Output:
    Same console report as analyze_diag.py, showing the real distribution
    of spread_pct / volume_ratio / tick_count that would have been seen.

Requirements:
    pip install requests  (standard library otherwise)
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter, deque
from pathlib import Path
from typing import Deque, List, Optional, Tuple

# ── Optional: requests (or urllib fallback) ───────────────────────────────────
try:
    import requests as _req
    def _get(url: str, params: dict) -> dict | list:
        r = _req.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
except ImportError:
    import urllib.request
    import urllib.parse
    def _get(url: str, params: dict) -> dict | list:  # type: ignore[misc]
        full_url = url + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(full_url, timeout=30) as resp:
            return json.loads(resp.read())

# ── Constants ─────────────────────────────────────────────────────────────────
BINANCE_AGG_URL = "https://fapi.binance.com/fapi/v1/aggTrades"
MAX_PER_REQUEST = 1000   # Binance limit

# ── Download ──────────────────────────────────────────────────────────────────

def download_agg_trades(
    symbol: str,
    start_ms: int,
    end_ms: int,
    verbose: bool = True,
) -> List[dict]:
    """
    Fetch all aggTrades for symbol between [start_ms, end_ms].
    Returns list of dicts: {ts_ms, price, qty, side}
    """
    trades = []
    cursor = start_ms

    while cursor < end_ms:
        if verbose:
            elapsed_pct = (cursor - start_ms) / max(end_ms - start_ms, 1) * 100
            print(f"\r  Downloading... {elapsed_pct:5.1f}%  ({len(trades):,} trades)", end="", flush=True)

        try:
            batch = _get(BINANCE_AGG_URL, {
                "symbol":    symbol,
                "startTime": cursor,
                "endTime":   min(cursor + 60_000, end_ms),  # 1-minute chunks
                "limit":     MAX_PER_REQUEST,
            })
        except Exception as exc:
            print(f"\n  [!] Download error: {exc}. Retrying in 2s...")
            time.sleep(2)
            continue

        if not batch:
            cursor += 60_000
            continue

        for t in batch:
            trades.append({
                "ts_ms": t["T"],
                "price": float(t["p"]),
                "qty":   float(t["q"]),
                "side":  "SELL" if t["m"] else "BUY",  # m=True means maker=sell
            })

        cursor = batch[-1]["T"] + 1

    if verbose:
        print(f"\r  Downloaded {len(trades):,} trades for {symbol}        ")
    return trades


def load_csv(path: Path) -> List[dict]:
    """Load aggTrades from a local CSV (columns: timestamp_ms, price, qty, side)."""
    trades = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append({
                "ts_ms": int(row.get("timestamp_ms") or row.get("T") or 0),
                "price": float(row.get("price") or row.get("p")),
                "qty":   float(row.get("qty") or row.get("q")),
                "side":  "SELL" if str(row.get("side") or row.get("m")).upper() in ("SELL", "TRUE") else "BUY",
            })
    return trades


# ── Replay engine ─────────────────────────────────────────────────────────────

class RollingMetrics:
    """Minimal port of SymbolMetricsV2 for offline use."""

    def __init__(self, lookback: int = 20) -> None:
        self._ticks: Deque[Tuple[float, float, str, float]] = deque(maxlen=500)
        self.lookback = lookback

    def add(self, price: float, qty: float, side: str, ts_sec: float) -> None:
        self._ticks.append((price, qty, side, ts_sec))

    @property
    def rolling_avg_volume(self) -> float:
        if not self._ticks:
            return 0.0
        recent = list(self._ticks)[-self.lookback:]
        return sum(q for _, q, _, _ in recent) / len(recent)

    def directional_volume(self, is_long: bool, last_n: int = 5) -> float:
        target = "BUY" if is_long else "SELL"
        recent = list(self._ticks)[-last_n:]
        return sum(q for _, q, s, _ in recent if s == target)

    def tick_repetition(self, window_sec: float, is_long: bool, now_sec: float) -> int:
        target = "BUY" if is_long else "SELL"
        prices = [
            round(p, 2)
            for p, _, s, ts in self._ticks
            if ts >= now_sec - window_sec and s == target
        ]
        if not prices:
            return 0
        return Counter(prices).most_common(1)[0][1]


def replay(
    trades: List[dict],
    symbol: str,
    lag_ms: int,
    c1_thr: float,
    c2_thr: float,
    c3_thr: int,
    vol_lookback: int,
    tick_window: float,
    out_path: Optional[Path],
    diag_interval_sec: float = 0.5,
) -> List[dict]:
    """
    Replay trades through the signal logic.
    Returns list of diagnostic record dicts (same schema as diagnostics.jsonl).
    """
    metrics = RollingMetrics(lookback=vol_lookback)
    records = []
    out_file = None
    last_diag: float = 0.0

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_file = out_path.open("w", encoding="utf-8")

    total = len(trades)
    for i, trade in enumerate(trades):
        if i % 50_000 == 0:
            print(f"\r  Replaying... {i/total*100:5.1f}%  ({i:,}/{total:,})", end="", flush=True)

        ts_sec = trade["ts_ms"] / 1000.0
        price  = trade["price"]
        qty    = trade["qty"]
        side   = trade["side"]

        metrics.add(price, qty, side, ts_sec)

        # Simulate WEEX as the same price but lagged
        weex_price = None
        if i >= 1:
            # Look back to find the trade that was current `lag_ms` ago
            lag_sec = lag_ms / 1000.0
            weex_ts_target = ts_sec - lag_sec
            # Binary search approximation: scan backwards
            j = i - 1
            while j >= 0 and trades[j]["ts_ms"] / 1000.0 > weex_ts_target:
                j -= 1
            if j >= 0:
                weex_price = trades[j]["price"]

        if weex_price is None:
            continue

        # Throttle diagnostic writes
        if ts_sec - last_diag < diag_interval_sec:
            continue
        last_diag = ts_sec

        # ── Condition 1 ──────────────────────────────────────────────────────
        spread_pct = (price - weex_price) / weex_price * 100.0
        abs_spread = abs(spread_pct)
        c1 = abs_spread >= c1_thr
        is_long = spread_pct >= 0

        # ── Condition 2 ──────────────────────────────────────────────────────
        avg_vol = metrics.rolling_avg_volume
        if avg_vol == 0:
            continue
        dir_vol = metrics.directional_volume(is_long, last_n=5)
        vol_ratio = dir_vol / avg_vol
        c2 = vol_ratio >= c2_thr

        # ── Condition 3 ──────────────────────────────────────────────────────
        tc = metrics.tick_repetition(tick_window, is_long, ts_sec)
        c3 = tc >= c3_thr

        all_pass = c1 and c2 and c3

        import math
        if all_pass:
            ls = min(abs_spread / (c1_thr * 3), 1.0)
            vs = min(vol_ratio  / (c2_thr  * 2), 1.0)
            ts_ = min(tc        / (c3_thr  * 2), 1.0)
            conf = ls * vs * ts_
            conf = conf ** (1/3)
        else:
            conf = None

        rec = {
            "ts":           round(ts_sec, 3),
            "symbol":       symbol,
            "direction":    "LONG" if is_long else "SHORT",
            "spread_pct":   round(spread_pct, 6),
            "c1_pass":      c1,
            "volume_ratio": round(vol_ratio, 4),
            "c2_pass":      c2,
            "tick_count":   tc,
            "c3_pass":      c3,
            "all_pass":     all_pass,
            "confidence":   round(conf, 4) if conf is not None else None,
            "binance_mid":  price,
            "weex_mid":     weex_price,
        }
        records.append(rec)

        if out_file:
            out_file.write(json.dumps(rec) + "\n")

    print(f"\r  Replayed {total:,} trades → {len(records):,} diagnostic records    ")

    if out_file:
        out_file.close()
        print(f"  Written → {out_path}")

    return records


# ── Analysis (same as analyze_diag.py but inline) ─────────────────────────────

def analyse(rows: List[dict], c1_thr: float, c2_thr: float, c3_thr: int) -> None:
    # Re-use analyze_diag logic by importing if available, else inline summary
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from analyze_diag import analyse as _analyse
        _analyse(rows, suggest=True)
        return
    except Exception:
        pass

    # Fallback inline summary
    total = len(rows)
    if not total:
        print("No records to analyse.")
        return

    def pct(lst, p):
        if not lst:
            return float("nan")
        s = sorted(lst)
        idx = (len(s) - 1) * p / 100.0
        lo, hi = int(idx), min(int(idx)+1, len(s)-1)
        return s[lo] + (s[hi]-s[lo])*(idx-lo)

    spreads = [abs(r["spread_pct"]) for r in rows]
    vols    = [r["volume_ratio"]    for r in rows]
    ticks   = [r["tick_count"]      for r in rows]
    signals = [r for r in rows if r.get("all_pass")]

    print(f"\n{'='*55}")
    print(f"  BACKTEST SUMMARY  ({total:,} records, {len(signals)} signals)")
    print(f"{'='*55}")
    print(f"  spread_pct  p50={pct(spreads,50):.4f}  p75={pct(spreads,75):.4f}  p90={pct(spreads,90):.4f}")
    print(f"  vol_ratio   p50={pct(vols,50):.3f}  p75={pct(vols,75):.3f}  p90={pct(vols,90):.3f}")
    print(f"  tick_count  p50={pct(ticks,50):.1f}  p75={pct(ticks,75):.1f}  p90={pct(ticks,90):.1f}")
    print(f"\n  Suggested thresholds (p75):")
    print(f"    LATENCY_THRESHOLD_PCT={round(pct(spreads,75),4)}")
    print(f"    VOLUME_SPIKE_MULTIPLIER={round(pct(vols,75),3)}")
    print(f"    TICK_REPETITION_MIN_COUNT={round(pct(ticks,75))}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download Binance aggTrades and backtest signal logic"
    )
    parser.add_argument("--symbol",  default="BTCUSDT",  help="Futures symbol")
    parser.add_argument("--hours",   type=float, default=2.0, help="Hours of history to fetch")
    parser.add_argument("--lag",     type=int,   default=500,  help="Simulated WEEX lag in ms")
    parser.add_argument("--csv",     default="",  help="Use local CSV instead of downloading")
    parser.add_argument("--out",     default="",  help="Write JSONL output to this file")
    parser.add_argument("--c1",      type=float, default=None, help="Override LATENCY_THRESHOLD_PCT")
    parser.add_argument("--c2",      type=float, default=None, help="Override VOLUME_SPIKE_MULTIPLIER")
    parser.add_argument("--c3",      type=int,   default=None, help="Override TICK_REPETITION_MIN_COUNT")
    args = parser.parse_args()

    # Load config thresholds
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config import CONFIG
        c1 = args.c1 or CONFIG.signal.latency_threshold_pct
        c2 = args.c2 or CONFIG.signal.volume_spike_multiplier
        c3 = args.c3 or CONFIG.signal.tick_repetition_min_count
        vol_lookback  = CONFIG.signal.volume_lookback_bars
        tick_window   = CONFIG.signal.tick_repetition_window
    except Exception:
        c1, c2, c3 = args.c1 or 0.08, args.c2 or 3.0, args.c3 or 5
        vol_lookback, tick_window = 20, 1.0

    print(f"\n  Symbol : {args.symbol}")
    print(f"  Lag    : {args.lag} ms  (simulated WEEX latency)")
    print(f"  Thresholds → C1≥{c1}%  C2≥{c2}×  C3≥{c3} ticks\n")

    # ── Load data ─────────────────────────────────────────────────────────────
    if args.csv:
        path = Path(args.csv)
        print(f"  Loading from {path} ...")
        trades = load_csv(path)
        print(f"  Loaded {len(trades):,} trades")
    else:
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - int(args.hours * 3600 * 1000)
        print(f"  Fetching last {args.hours:.1f}h of {args.symbol} aggTrades from Binance...")
        trades = download_agg_trades(args.symbol, start_ms, end_ms)

    if not trades:
        print("No trades loaded. Exiting.")
        sys.exit(1)

    # ── Replay ────────────────────────────────────────────────────────────────
    out_path = Path(args.out) if args.out else None
    records = replay(
        trades,
        symbol=args.symbol,
        lag_ms=args.lag,
        c1_thr=c1,
        c2_thr=c2,
        c3_thr=c3,
        vol_lookback=vol_lookback,
        tick_window=tick_window,
        out_path=out_path,
    )

    # ── Analyse ───────────────────────────────────────────────────────────────
    analyse(records, c1, c2, c3)


if __name__ == "__main__":
    main()
