# Trading Bots Monorepo

Два независимых торговых бота в одном репозитории.

---

## Продукты

| Бот | Рынок | Запуск |
|-----|-------|--------|
| [Arbitrage Scalper](#arbitrage-scalper) | Crypto Futures (Binance → WEEX) | `python main.py` |
| [Polymarket AI Agent](#polymarket-ai-agent) | Prediction Markets (Polymarket) | `python polymarket_main.py` |

---

## Arbitrage Scalper

High-frequency crypto futures арбитраж между **Binance** (price leader) и **WEEX** (lagging exchange).

### Как работает

Цена на Binance движется с небольшой задержкой на WEEX. Бот мониторит оба ордербука одновременно, детектирует спред и входит в позицию на WEEX до того, как она выравнивается.

Сигнал срабатывает только когда **три условия выполнены одновременно**:

| # | Условие | Что проверяет |
|---|---------|--------------|
| 1 | **Price Latency** | Binance–WEEX спред ≥ порог |
| 2 | **Volume Spike** | Объём на Binance ≥ N× скользящего среднего |
| 3 | **Tick Density** | Устойчивые направленные aggTrades за последние T секунд |

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

### Требования

- Python 3.11+
- WEEX Futures аккаунт с API ключом (исполнение)
- Binance Futures аккаунт (опционально, публичный WebSocket бесплатный)

### Запуск

```bash
pip install -r requirements.txt
cp .env.example .env
# Заполни WEEX_API_KEY, WEEX_API_SECRET, WEEX_PASSPHRASE

# Paper trade (без реальных ордеров)
DRY_RUN=true python main.py

# Live торговля
python main.py

# Live + веб-дашборд на http://localhost:8080
WEB=true python main.py
```

### Конфигурация

| Переменная | По умолчанию | Описание |
|-----------|-------------|---------|
| `TRADING_PAIRS` | `BTCUSDT,ETHUSDT,SOLUSDT` | Торговые пары |
| `ACCOUNT_BALANCE_USDT` | `1000.0` | Капитал бота |
| `RISK_PER_TRADE_PCT` | `0.5` | % баланса на сделку |
| `LEVERAGE` | `10` | Плечо |
| `LATENCY_THRESHOLD_PCT` | `0.08` | Мин спред % для сигнала |
| `VOLUME_SPIKE_MULTIPLIER` | `3.0` | Объём должен быть N× среднего |
| `TRAILING_STOP_PCT` | `0.12` | Трейлинг стоп % |
| `MAX_TRADE_DURATION_SEC` | `30` | Максимальная длина сделки |
| `DRY_RUN` | `false` | `true` = только логи |

### Бэктест

```bash
python tools/backtest_signals.py   # Бэктест на сохранённых логах
python tools/analyze_diag.py       # Анализ диагностики
```

### Структура файлов

```
├── main.py                  Точка входа
├── config.py                Конфигурация (из .env)
├── core/
│   ├── data_fetcher.py      Агрегатор WebSocket фидов
│   ├── signal_generator.py  3-confluence детектор сигналов
│   ├── execution_engine.py  Жизненный цикл ордеров на WEEX
│   └── risk_manager.py      Сайзинг позиции и стопы
├── exchanges/
│   ├── binance_futures.py   Binance WS фид (price leader)
│   └── weex_futures.py      WEEX WS фид + REST исполнение
├── models/
│   ├── signal.py            Датакласс Signal
│   ├── trade.py             Состояние сделки / позиции
│   └── order_book.py        Снимок ордербука
└── tools/
    ├── backtest_signals.py  Исторический бэктестер
    └── analyze_diag.py      Анализатор диагностических логов
```

---

## Polymarket AI Agent

Алгоритмический агент для [Polymarket](https://polymarket.com). Сканирует открытые рынки, использует **MiroFish** (локальный AI-сервис) для оценки вероятностей, и ставит когда математическое преимущество превышает порог.

### Как работает

```
Market Scanner  ──── фильтр по объёму / ликвидности ────► Список рынков
      │
      ▼  (для каждого рынка не в портфеле)
Обновить цену  (Polymarket CLOB API)
      │
      ▼
MiroFish AI  ──── вопрос + описание ────► Вероятность YES
      │
      ▼
Edge = наша_вероятность − рыночная_цена
      │
      ├── edge < порог  →  пропустить
      │
      └── edge ≥ порог  →  Kelly sizing  →  Поставить (CLOB API)
```

Один цикл сканирования запускается каждые 30 минут. Рынки оцениваются последовательно (MiroFish анализ занимает 2–5 минут на рынок).

### Требования

- Python 3.11+
- **MiroFish** запущен локально (Docker)
- Polymarket аккаунт с CLOB API credentials
- LLM API ключ для MiroFish (Gemini, Qwen, Groq — есть бесплатные тиры)
- Zep Cloud аккаунт для памяти MiroFish (бесплатный тир)

### Быстрый старт

#### 1. Запустить MiroFish

```bash
git clone https://github.com/IvanKornei/MiroFish.git
cd MiroFish
cp .env.example .env
# Заполни: LLM_API_KEY, LLM_BASE_URL, LLM_MODEL_NAME, ZEP_API_KEY
docker compose up -d

# Проверить что работает
curl http://localhost:5001/health  # → {"status": "ok"}
```

**Бесплатные LLM для MiroFish:**

| Провайдер | Модель | Регистрация |
|-----------|--------|------------|
| Google Gemini | `gemini-2.0-flash` | [aistudio.google.com](https://aistudio.google.com) |
| Alibaba Qwen | `qwen-plus` | [bailian.aliyun.com](https://bailian.aliyun.com) |
| Groq | `llama-3.3-70b-versatile` | [console.groq.com](https://console.groq.com) |

**Zep Cloud:** бесплатный аккаунт на [getzep.com](https://getzep.com)

#### 2. Получить Polymarket credentials

```bash
pip install -r requirements.txt

# Сгенерировать CLOB API ключи из приватного ключа кошелька
python tools/generate_poly_creds.py
# Выведет POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
```

#### 3. Настроить и запустить

```bash
cp .env.example .env
# Заполни: POLY_PRIVATE_KEY, POLY_API_*, MIROFISH_URL, LLM_*, ZEP_API_KEY

# Тест (без реальных ставок)
DRY_RUN=true python polymarket_main.py

# Live торговля
DRY_RUN=false python polymarket_main.py
```

### Конфигурация

| Переменная | По умолчанию | Описание |
|-----------|-------------|---------|
| `DRY_RUN` | `true` | `true` = только логи, без реальных ставок |
| `POLY_PRIVATE_KEY` | — | Приватный ключ Polygon кошелька (`0x...`) |
| `POLY_API_KEY` | — | CLOB API key |
| `POLY_API_SECRET` | — | CLOB API secret |
| `POLY_API_PASSPHRASE` | — | CLOB API passphrase |
| `MIROFISH_URL` | `http://localhost:5001` | URL MiroFish сервиса |
| `POLY_MIN_EDGE_PCT` | `5.0` | Мин edge % для ставки |
| `POLY_MAX_POSITIONS` | `10` | Макс одновременных позиций |
| `POLY_SCAN_INTERVAL` | `1800` | Секунд между сканированиями |
| `POLY_SIM_ROUNDS` | `10` | Раундов MiroFish на рынок |
| `POLY_MIN_VOLUME` | `500` | Мин 24h объём USD |
| `POLY_MIN_LIQUIDITY` | `200` | Мин ликвидность USD |
| `POLY_MIN_BET_USDC` | `1.0` | Минимальный размер ставки |
| `POLY_MAX_BET_FRACTION` | `0.05` | Макс доля банкролла на ставку |
| `POLY_KELLY_FRACTION` | `0.25` | Kelly множитель (0.25 = quarter-Kelly) |
| `POLY_MAX_SCAN_MARKETS` | `20` | Макс рынков на цикл |

### Структура файлов

```
├── polymarket_main.py          Точка входа
├── agents/
│   └── polymarket_agent.py     Главный цикл: сканирование → прогноз → ставка
├── polymarket/
│   ├── client.py               Polymarket CLOB API клиент (EIP-712 авторизация)
│   ├── market_scanner.py       Поиск и фильтрация рынков
│   ├── order_manager.py        Отслеживание позиций
│   └── kelly.py                Kelly criterion для сайзинга ставок
├── mirofish/
│   ├── client.py               HTTP клиент MiroFish сервиса
│   └── predictor.py            Извлечение вероятности из AI отчёта
├── tests/
│   ├── test_kelly.py
│   ├── test_market_scanner.py
│   ├── test_mirofish_client.py
│   ├── test_predictor.py
│   └── test_agent_integration.py
└── tools/
    └── generate_poly_creds.py  Генерация CLOB API credentials
```

### Тесты

```bash
pip install -r requirements.txt pytest pytest-asyncio
pytest tests/
```

---

## Общие файлы

```
├── utils/
│   ├── logger.py        Структурированное логирование (shared)
│   └── diagnostics.py   Сбор метрик (shared)
├── web/
│   └── server.py        Веб-дашборд для Arbitrage Scalper
├── .env.example         Все переменные окружения (оба бота)
└── requirements.txt     Зависимости (оба бота)
```

---

## Предупреждение о рисках

Оба бота работают с реальными деньгами. Всегда запускай с `DRY_RUN=true` сначала, чтобы убедиться что логика работает корректно, и только потом переходи в live с небольшим балансом.
