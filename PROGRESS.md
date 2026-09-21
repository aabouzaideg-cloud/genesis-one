# GENESIS-ONE — Build Progress

## Status: In Progress — Backtester Debugging
**Last updated:** 2026-05-05

---

## Completed

### Core Build
- [x] `config.py` — all constants, modes, risk params
- [x] `capital_client.py` — Capital.com auth, OHLCV, orders
- [x] `whale_monitor.py` — Polymarket WebSocket listener
- [x] `news_sentinel.py` — GDELT + Ollama/Gemma sentiment gate
- [x] `confluence_gate.py` — 3-condition entry gatekeeper
- [x] `risk_manager.py` — ATR SL/TP, trailing stop, margin guard
- [x] `core_engine.py` — async orchestrator + Decision Thesis logging
- [x] `main.py` — entry point with graceful shutdown

### Gap Fixes
- [x] **#1 Session token refresh** — keep-alive loop every 9min, auto re-auth on 401
- [x] **#2 Persistent position state** — `position_store.py` writes to `positions.json`, reconciles on restart
- [x] **#3 Whale → epic mapping** — `market_mapper.py` two-level system (curated + keyword scoring), `market_map.json` with real condition IDs

---

## Completed (continued)

- [x] **#4 Polymarket WS format verified + whale_monitor rewritten**
  - Verified via official docs: 3 critical bugs found and fixed
  - Wrong WS URL → `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  - Field names fixed: `conditionId`, `outcome` string, no `taker_address`
  - Architecture redesigned: REST polling (data-api) for whale trades + WS for price feed
  - PING/PONG keep-alive added (every 10s, required by Polymarket)
  - FOMO killer now checks both signal age (>5min) AND price drift (>1%)
  - Bitcoin token IDs added to market_map.json

---

## Completed (continued)

- [x] **#5 Backtesting framework** — `backtester.py`
  - Fetches historical whale trades from Polymarket data API
  - Replays FOMO killer + chart geometry on historical Capital.com OHLCV
  - Simulates full position: partial close at TP1, trailing stop on remainder
  - Generates win rate, avg R:R, expectancy, max drawdown report
  - Saves full trade log to `backtest_results.json`
  - News gate marked as ASSUMED_PASS (can't replay LLM sentiment)

---

## All Gaps Fixed ✓

| Gap | Status |
|-----|--------|
| #1 Session token refresh | ✓ Done |
| #2 Persistent position state | ✓ Done |
| #3 Whale → epic mapping | ✓ Done |
| #4 Polymarket WS format | ✓ Done |
| #5 Backtesting framework | ✓ Done |

---

## Next Steps — Go Live Checklist

1. [ ] Add whale wallet addresses to `TRACKED_WALLETS` in `config.py`
2. [ ] Run `ollama pull gemma3:4b` + `ollama serve`
3. [ ] Run backtest: `python backtester.py --days 30`
4. [ ] Review `backtest_results.json` — expectancy should be > 0.1R
5. [ ] Run in DEMO mode for 1 week: `python main.py`
6. [ ] Monitor logs — check gate decisions make sense
7. [ ] Switch to LIVE: set `TRADING_MODE=LIVE` in `.env`

---

## Credentials & Config
- Capital.com: configured in `.env` (DEMO mode)
- Ollama: run `ollama pull gemma3:4b` + `ollama serve`
- No other API keys needed (GDELT is free/public)

---

## Real Polymarket Markets Mapped (from live API, 2026-05-04)

| Market | Epic | Condition ID | Vol 24h |
|--------|------|-------------|---------|
| Bitcoin $150k by Jun 30 2026 | BTCUSD | `0xa0f4c4...` | $5.8M |
| Hormuz traffic normal by May 15 | OIL_CRUDE | `0xffe381...` | $1.7M |
| Hormuz traffic normal by May 31 | OIL_CRUDE | `0x518a5b...` | $1.2M |
| Iran airspace closure by May 8 | OIL_CRUDE | `0x2135ff...` | $934K |
| US-Iran peace deal by May 15 | OIL_CRUDE | `0x1db02b...` | $861K |
| Iranian regime fall by May 31 | OIL_CRUDE | `0x789c94...` | $1.3M |
| QatarEnergy LNG by Apr 30 | OIL_CRUDE | `0xf22006...` | $6.5M |

---

## To Resume — Backtester Debug (paused 2026-05-05)

### Problem
Backtester runs successfully (finds 373–420 trades per wallet, Capital.com authenticates OK),
but `backtest_results.json` is empty — every `_process_trade()` call returns `None`.

### Diagnosis so far
- MarketMapper tested locally — works correctly (curated + keyword both resolve fine)
- The backtester ran BEFORE the OHLCV date-param fix; re-run needed
- Root cause unknown: need to see which filter is dropping trades
  - Candidate A: `conditionId` field not present / wrong name in data API response
  - Candidate B: `title`/`slug` fields empty → keyword scoring always 0
  - Candidate C: `outcome` values non-standard (not Yes/No/Up/Down)
  - Candidate D: OHLCV still returning empty even after fix (epic name invalid?)

### Next action
1. **Turn on VPN** (data-api.polymarket.com blocks without it)
2. Add debug counters to `_process_trade()` — log rejection reason per trade
3. Run: `python backtester.py --days 30`
4. Read logs to see breakdown: outcome_skip / mapper_skip / ohlcv_skip / chart_blocked / approved
5. Fix whichever bucket is eating all trades
