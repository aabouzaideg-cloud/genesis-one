# GENESIS-ONE — PolyPrint Trading Engine

**System Name:** GENESIS-ONE
**Project:** PolyPrint
**Goal:** Autonomous, cross-platform trading engine bridging Polymarket's decentralized intelligence with Capital.com's institutional-grade execution.

---

## Philosophy: Hyper-Stochastic Trading

Only trades when **three independent variables** align into a single, logical "Reason-Based" thesis:
- Whale Moves
- Global News / Macro Sentiment
- Chart Geometry

---

## 1. Data Acquisition & Integration (The Inputs)

- **Whale Monitoring:** WebSocket listener on Polymarket's CLOB API. Tracks a dynamic list of top-performing "Information Specialist" wallets.
- **Visual/Technical Analysis:** `pandas_ta` ingests live OHLCV data from Capital.com.
- **News/Macro Sentiment:** Low-latency news aggregator with an LLM-based logic gate classifying news as **Bullish**, **Bearish**, or **Noise** per asset class.

---

## 2. The Reasoning Engine — ConfluenceGate (Zero-FOMO Gatekeeper)

All **3 conditions** must pass before any trade executes:

| # | Condition | Rule |
|---|-----------|------|
| 1 | **The Signal** | A Specialist Whale takes a directional bet (Yes/No) on Polymarket |
| 2 | **The Logic** | A news event or macro shift supports the whale's move — unexplained whale activity = blocked |
| 3 | **The Chart Geometry** | Price is in a "Value Zone" (near 21-EMA, Fib 0.618, or proven S/R level) |

**FOMO Killer:** If price has moved >1% since whale entry → signal is **Expired** → trade ignored.

---

## 3. Deterministic Execution & Exit — RiskManager

- **Slippage Gate:** Only execute if slippage < 0.05%
- **Stop Loss:** 1.5x ATR from entry point
- **Take Profit:** Scale out — close 50% at 1:1.5 Risk/Reward
- **Trailing Stop:** Activate 0.3% trailing stop on remaining 50% to maximize trend capture
- **Margin Guard:** Maintain minimum 350% margin — account longevity > individual trade profit

---

## 4. Audit & Intelligence Log

Every trade attempt logs a **Decision Thesis**:

> `[GENESIS] - Long EUR/USD at 1.0850. Reason: Whale 0x... bet 'Yes' on Rate Cut; Bloomberg reports CPI cooling; 15m Chart shows Double Bottom at Support. Confidence: High.`

---

## 5. Technical Stack

| Layer | Tools |
|-------|-------|
| Language | Python 3.11+ |
| Concurrency | `asyncio` |
| Exchange API | `ccxt` |
| Technical Analysis | `pandas_ta` |
| HTTP | `requests` |
| Secrets | `.env` file |
| Mode Toggle | `TRADING_MODE = "DEMO"` or `"LIVE"` |

---

## 6. Deliverables

1. `CoreEngine` — main loop handling concurrent data streams
2. `ConfluenceGate` — class containing strict entry logic
3. `RiskManager` — module for ATR-based TP/SL and margin protection
