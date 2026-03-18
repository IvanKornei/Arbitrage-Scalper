#!/usr/bin/env python3
"""
tools/analyze_diag.py – Offline analysis of logs/diagnostics.jsonl

Usage:
    python tools/analyze_diag.py                        # default: logs/diagnostics.jsonl
    python tools/analyze_diag.py logs/diagnostics.jsonl
    python tools/analyze_diag.py --suggest              # print suggested new thresholds

Output:
    • Sample count and time range
    • Percentile table for each raw metric (spread_pct, volume_ratio, tick_count)
    • Condition pass-rate matrix  (which combos of c1/c2/c3 appear, how often)
    • Per-symbol breakdown
    • Suggested thresholds (--suggest flag) based on p90 of each metric
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List

# ── Helpers ───────────────────────────────────────────────────────────────────

def pct(lst: List[float], p: float) -> float:
    if not lst:
        return float("nan")
    s = sorted(lst)
    idx = (len(s) - 1) * p / 100.0
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def fmt(v: float, decimals: int = 4) -> str:
    if v != v:  # nan
        return "  n/a  "
    return f"{v:.{decimals}f}"


def bar(ratio: float, width: int = 20) -> str:
    filled = round(ratio * width)
    return "█" * filled + "░" * (width - filled)


# ── Load data ─────────────────────────────────────────────────────────────────

def load(path: Path) -> List[dict]:
    rows = []
    bad = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    if bad:
        print(f"  [!] Skipped {bad} malformed lines")
    return rows


# ── Analysis ──────────────────────────────────────────────────────────────────

def analyse(rows: List[dict], suggest: bool) -> None:
    if not rows:
        print("No data found in file.")
        return

    total = len(rows)
    ts_vals = [r["ts"] for r in rows if "ts" in r]
    t_start = min(ts_vals)
    t_end = max(ts_vals)
    duration_min = (t_end - t_start) / 60.0

    print("=" * 60)
    print("  DIAGNOSTICS ANALYSIS")
    print("=" * 60)
    print(f"  Records  : {total:,}")
    print(f"  Duration : {duration_min:.1f} min  ({duration_min/60:.2f} h)")
    print(f"  Symbols  : {', '.join(sorted({r['symbol'] for r in rows}))}")
    print()

    # ── Raw metric distributions ──────────────────────────────────────────────
    spreads  = [abs(r["spread_pct"])   for r in rows if "spread_pct"   in r]
    volrats  = [r["volume_ratio"]      for r in rows if "volume_ratio"  in r]
    ticks    = [r["tick_count"]        for r in rows if "tick_count"    in r]

    PERCS = [50, 75, 90, 95, 99]

    print("─" * 60)
    print("  RAW METRIC PERCENTILES")
    print("─" * 60)
    header = f"  {'Metric':<22}" + "".join(f"  p{p:<4}" for p in PERCS)
    print(header)
    print("  " + "-" * 56)

    for label, vals, decimals in [
        ("spread_pct (%)",    spreads, 4),
        ("volume_ratio (×)",  volrats, 3),
        ("tick_count",        ticks,   1),
    ]:
        row_str = f"  {label:<22}" + "".join(f"  {fmt(pct(vals, p), decimals):<6}" for p in PERCS)
        print(row_str)
    print()

    # Current thresholds (read from config if possible, else hardcode defaults)
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from config import CONFIG
        c1_thr = CONFIG.signal.latency_threshold_pct
        c2_thr = CONFIG.signal.volume_spike_multiplier
        c3_thr = CONFIG.signal.tick_repetition_min_count
    except Exception:
        c1_thr, c2_thr, c3_thr = 0.08, 3.0, 5

    print(f"  Current thresholds → C1≥{c1_thr}%  C2≥{c2_thr}×  C3≥{c3_thr} ticks")
    print()

    # Pass rates at current thresholds
    c1_pass = sum(1 for r in rows if abs(r.get("spread_pct", 0)) >= c1_thr)
    c2_pass = sum(1 for r in rows if r.get("volume_ratio", 0)    >= c2_thr)
    c3_pass = sum(1 for r in rows if r.get("tick_count", 0)      >= c3_thr)

    print("─" * 60)
    print("  INDIVIDUAL CONDITION PASS RATES  (at current thresholds)")
    print("─" * 60)
    for label, n in [("C1 spread",  c1_pass), ("C2 volume",  c2_pass), ("C3 ticks",  c3_pass)]:
        ratio = n / total
        print(f"  {label:<12} {n:>6,} / {total:,}  ({ratio*100:5.1f}%)  {bar(ratio)}")
    print()

    # ── Condition combo matrix ────────────────────────────────────────────────
    print("─" * 60)
    print("  CONDITION COMBINATION MATRIX")
    print("─" * 60)
    combo_counts: Counter = Counter()
    for r in rows:
        key = (
            bool(abs(r.get("spread_pct", 0)) >= c1_thr),
            bool(r.get("volume_ratio", 0)    >= c2_thr),
            bool(r.get("tick_count", 0)      >= c3_thr),
        )
        combo_counts[key] += 1

    print(f"  {'C1':>5} {'C2':>5} {'C3':>5}  {'Count':>8}  {'Rate':>6}  Bar")
    print("  " + "-" * 55)
    for combo in sorted(combo_counts, key=lambda k: -combo_counts[k]):
        c1, c2, c3 = combo
        n = combo_counts[combo]
        ratio = n / total
        flags = f"  {'✓' if c1 else '✗':>5} {'✓' if c2 else '✗':>5} {'✓' if c3 else '✗':>5}"
        marker = " ← SIGNAL" if c1 and c2 and c3 else ""
        print(f"{flags}  {n:>8,}  {ratio*100:5.1f}%  {bar(ratio, 15)}{marker}")
    print()

    # ── Per-symbol breakdown ──────────────────────────────────────────────────
    symbols = sorted({r.get("symbol", "?") for r in rows})
    if len(symbols) > 1:
        print("─" * 60)
        print("  PER-SYMBOL BREAKDOWN")
        print("─" * 60)
        print(f"  {'Symbol':<12} {'Count':>7}  {'C1%':>6}  {'C2%':>6}  {'C3%':>6}  {'ALL%':>6}  {'med_spread':>10}  {'med_vol':>8}")
        print("  " + "-" * 70)
        by_sym: Dict[str, List[dict]] = defaultdict(list)
        for r in rows:
            by_sym[r.get("symbol", "?")].append(r)
        for sym in symbols:
            sr = by_sym[sym]
            n  = len(sr)
            sc1 = sum(1 for r in sr if abs(r.get("spread_pct", 0)) >= c1_thr) / n * 100
            sc2 = sum(1 for r in sr if r.get("volume_ratio", 0)    >= c2_thr) / n * 100
            sc3 = sum(1 for r in sr if r.get("tick_count", 0)      >= c3_thr) / n * 100
            sa  = sum(1 for r in sr if r.get("all_pass", False))              / n * 100
            med_sp  = pct([abs(r.get("spread_pct",  0)) for r in sr], 50)
            med_vol = pct([r.get("volume_ratio", 0) for r in sr], 50)
            print(f"  {sym:<12} {n:>7,}  {sc1:>5.1f}%  {sc2:>5.1f}%  {sc3:>5.1f}%  {sa:>5.1f}%  {med_sp:>10.4f}  {med_vol:>8.3f}")
        print()

    # ── Signal events ─────────────────────────────────────────────────────────
    signals = [r for r in rows if r.get("all_pass")]
    print("─" * 60)
    print(f"  SIGNAL EVENTS  ({len(signals)} total, {len(signals)/duration_min:.2f}/min)")
    print("─" * 60)
    if signals:
        confs = [r["confidence"] for r in signals if r.get("confidence") is not None]
        dirs  = Counter(r.get("direction") for r in signals)
        print(f"  LONG / SHORT   : {dirs.get('LONG', 0)} / {dirs.get('SHORT', 0)}")
        if confs:
            print(f"  Confidence p50 : {pct(confs, 50):.3f}")
            print(f"  Confidence p75 : {pct(confs, 75):.3f}")
            print(f"  Confidence p90 : {pct(confs, 90):.3f}")
    else:
        print("  No signals fired yet with current thresholds.")
    print()

    # ── Suggested thresholds ──────────────────────────────────────────────────
    if suggest:
        print("─" * 60)
        print("  SUGGESTED THRESHOLD ADJUSTMENTS")
        print("─" * 60)
        print("  Method: set each threshold at the p75 of observed values")
        print("  (meaning ~25% of ticks will satisfy each condition alone)")
        print()

        s_c1 = pct(spreads, 75)
        s_c2 = pct(volrats, 75)
        s_c3 = pct(ticks,   75)

        def _arrow(old: float, new: float) -> str:
            if abs(old - new) < 1e-9:
                return "  (no change)"
            arrow = "↓ looser" if new < old else "↑ tighter"
            return f"  {old} → {new:.4f}  [{arrow}]"

        print(f"  LATENCY_THRESHOLD_PCT    {_arrow(c1_thr, round(s_c1, 4))}")
        print(f"  VOLUME_SPIKE_MULTIPLIER  {_arrow(c2_thr, round(s_c2, 3))}")
        print(f"  TICK_REPETITION_MIN_COUNT{_arrow(float(c3_thr), round(s_c3))}")
        print()
        print("  Copy to your .env file:")
        print(f"    LATENCY_THRESHOLD_PCT={round(s_c1, 4)}")
        print(f"    VOLUME_SPIKE_MULTIPLIER={round(s_c2, 3)}")
        print(f"    TICK_REPETITION_MIN_COUNT={round(s_c3)}")
        print()
        print("  NOTE: p75 thresholds mean ALL-THREE will fire when each")
        print("  metric is in its top quartile simultaneously. Start here")
        print("  and tighten/loosen after reviewing backtest results.")
        print()

    print("=" * 60)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze diagnostics.jsonl")
    parser.add_argument(
        "file",
        nargs="?",
        default="logs/diagnostics.jsonl",
        help="Path to diagnostics JSONL file",
    )
    parser.add_argument(
        "--suggest",
        action="store_true",
        help="Print suggested threshold adjustments",
    )
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"File not found: {path}")
        print("Run the bot first to accumulate data, or use backtest_signals.py")
        sys.exit(1)

    rows = load(path)
    analyse(rows, suggest=args.suggest)


if __name__ == "__main__":
    main()
