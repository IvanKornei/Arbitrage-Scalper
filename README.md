# Arbitrage Scalper

High-frequency crypto futures arbitrage bot that exploits price latency between **Binance** (price leader) and **WEEX** (lagging exchange).

## How it works

Binance price moves propagate to WEEX with a small delay. The bot monitors both order books simultaneously, detects the spread, and enters a position on WEEX before it catches up.

A signal fires only when **all three conditions are met at the same time**:

| # | Condition | What it checks |
|---|-----------|---------------|
| 1 | **Price Latency** | Binance–WEEX mid-price spread ≥ threshold |
| 2 | **Volume Spike** | Aggressor volume on Binance ≥ N× rolling average |
| 3 | **Tick Density** | Sustained directional aggTrades within the last T seconds |

```
DataFetcher
├── BinanceFuturesFeed  (WebSocket – price leader)
└── WeexFuturesFeed     (WebSocket – lagging exchange)
        │
        ▼
SignalGenerator  ──── 3-confluence check ────► Signal
        │
        ▼
ExecutionEngine  ◄──── RiskManager (sizing + stops)
        │
        ▼
  WEEX REST API  (market open → trailing SL → market close)
```

## Requirements

- Python 3.11+
- WEEX Futures account with API key (execution)
- Binance Futures account (optional, public WebSocket is free)

## Setup

```bash
# 1. Clone
git clone https://github.com/IvanKornei/Arbitrage-Scalper.git
cd Arbitrage-Scalper

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — fill in WEEX_API_KEY, WEEX_API_SECRET, WEEX_PASSPHRASE
```

## Running

```bash
# Paper trade (no real orders — default safe mode)
DRY_RUN=true python main.py

# Live trading
python main.py

# Live trading + web dashboard at http://localhost:8080
WEB=true python main.py

# Custom port
WEB=true WEB_PORT=9090 python main.py
```

## Configuration

All parameters are set via environment variables (`.env` file). See `.env.example` for the full list.

### Key parameters

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_PAIRS` | `BTCUSDT,ETHUSDT,SOLUSDT` | Comma-separated futures symbols |
| `ACCOUNT_BALANCE_USDT` | `1000.0` | Capital allocated to the bot |
| `RISK_PER_TRADE_PCT` | `0.5` | % of balance risked per trade |
| `MAX_OPEN_POSITIONS` | `3` | Max simultaneous open positions |
| `LEVERAGE` | `10` | Futures leverage |
| `LATENCY_THRESHOLD_PCT` | `0.08` | Min Binance–WEEX spread % to trigger |
| `VOLUME_SPIKE_MULTIPLIER` | `3.0` | Volume must be N× the rolling average |
| `TICK_REPETITION_WINDOW` | `1.0` | Seconds window for tick density check |
| `TICK_REPETITION_MIN_COUNT` | `5` | Min same-direction ticks in the window |
| `BREAKEVEN_TRIGGER_PCT` | `0.15` | Move stop to breakeven when profit ≥ X% |
| `TRAILING_STOP_PCT` | `0.12` | Trailing stop distance % |
| `MAX_TRADE_DURATION_SEC` | `30` | Hard time limit per trade |
| `DRY_RUN` | `false` | `true` = log signals only, no orders |

### Advanced (signal tuning)

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNAL_COOLDOWN_SEC` | `2.0` | Min seconds between signals per symbol |
| `BINANCE_BOOK_MAX_STALE_MS` | `500` | Reject signal if Binance book older than N ms |
| `WEEX_BOOK_MAX_STALE_MS` | `1000` | Reject signal if WEEX book older than N ms |
| `VOLUME_LOOKBACK_BARS` | `20` | Rolling window size for volume average |

## Backtesting

```bash
# Run backtest on saved diagnostic logs
python tools/backtest_signals.py

# Analyse diagnostics
python tools/analyze_diag.py
```

Diagnostics are written to `logs/diagnostics.jsonl` while the bot runs (controlled by `DIAG_ENABLED`, `DIAG_INTERVAL_SEC`).

## Project structure

```
├── main.py                  Entry point
├── config.py                All configuration (loaded from .env)
├── core/
│   ├── data_fetcher.py      WebSocket feeds aggregator
│   ├── signal_generator.py  3-confluence signal detection
│   ├── execution_engine.py  Order lifecycle on WEEX
│   └── risk_manager.py      Position sizing and stop logic
├── exchanges/
│   ├── binance_futures.py   Binance WS feed (price leader)
│   └── weex_futures.py      WEEX WS feed + REST execution
├── models/
│   ├── signal.py            Signal dataclass
│   ├── trade.py             Trade / position state
│   └── order_book.py        Order book snapshot
├── utils/
│   ├── logger.py            Structured logging
│   └── diagnostics.py       Metrics collection
├── tools/
│   ├── backtest_signals.py  Historical backtester
│   └── analyze_diag.py      Diagnostic log analyser
└── web/
    └── server.py            Optional FastAPI dashboard
```

## Risk warning

This bot trades real money with leverage. Always start with `DRY_RUN=true`, verify signal frequency and sizing, then go live with a small balance first.
