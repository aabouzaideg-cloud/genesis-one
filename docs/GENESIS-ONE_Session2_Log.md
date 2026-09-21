# GENESIS-ONE — Session 2 Log
**Date:** 2026-05-05
**Mode:** DEMO
**Bot started:** 14:47 UAE time

---

## System Status

| Component | Status |
|---|---|
| Capital.com | Connected (DEMO) |
| Polymarket WS | Connected via Tor (32 token IDs) |
| Whale wallets tracked | 25 |
| Poll interval | 10s |
| Curated markets | 16 |

---

## Bugs Found & Fixed This Session

### 1. Capital.com OHLCV — `mid` key missing
- **File:** `capital_client.py`
- **Problem:** `get_ohlcv()` accessed `p["openPrice"]["mid"]` but Capital.com only returns `bid` and `ask`
- **Fix:** Compute `mid = (bid + ask) / 2` inline

### 2. WebSocket not routed through Tor
- **File:** `whale_monitor.py`
- **Problem:** `websockets.connect()` bypassed Tor — got "Connection reset by peer" in UAE
- **Fix:** Used `python_socks` to create a SOCKS5 socket, passed to `websockets.connect(sock=...)`

### 3. Timestamp in seconds, not milliseconds
- **File:** `whale_monitor.py`
- **Problem:** Polymarket data API returns timestamps in **seconds**. Code divided by 1000 treating them as milliseconds → computed age of ~56 years → every trade silently dropped by the 5-min FOMO filter
- **Fix:** `trade_age = time.time() - timestamp_s` (no division)

### 4. NO bets mapped to SELL — direction ambiguous
- **File:** `confluence_gate.py`
- **Problem:** `direction = "BUY" if YES else "SELL"` — but NO bets on negatively-framed questions ("Will BTC dip to $45k?") are actually bullish signals, not SELL signals
- **Fix:** Skip NO bets entirely — only YES bets are unambiguous directional signals

### 5. Multiple bot instances accumulating
- Killed stale processes and restarted clean single instance

---

## Signals Log — All 10 Entries

| # | Time | Verdict | Epic | Whale | Market | Poly Price | Reason |
|---|---|---|---|---|---|---|---|
| 1 | 14:25 | BLOCKED_CHART | ETHUSD | KrackenSruster | Will Ethereum dip to $1,200 in May? | 0.99 | NO bet — direction ambiguous |
| 2 | 14:35 | BLOCKED_CHART | OIL_CRUDE | KrackenSruster | Will WTI hit $150 in May? | 0.94 | NO bet — direction ambiguous |
| 3 | 14:51 | BLOCKED_CHART | BTCUSD | KrackenSruster | Will Bitcoin reach $95,000 in May? | 0.91 | NO bet — direction ambiguous |
| 4 | 15:19 | BLOCKED_CHART | BTCUSD | ranger44 | Will Bitcoin dip to $45,000 in May? | 0.99 | NO bet — direction ambiguous |
| 5 | 15:24 | BLOCKED_CHART | BTCUSD | ranger44 | Will Bitcoin dip to $50,000 in May? | 0.99 | NO bet — direction ambiguous |
| 6 | 15:30 | BLOCKED_NEWS | OIL_CRUDE | ranger44 | Will 100-124 ships transit Hormuz Apr27–May3? | 0.79 | GDELT returned no headlines |
| 7 | 15:50 | BLOCKED_NEWS | BTCUSD | ranger44 | Will Bitcoin be above $74,000 on May 6? | 1.00 | GDELT returned no headlines |
| 8 | 16:00 | BLOCKED_CHART | ETHUSD | ranger44 | Will Ethereum dip to $1,400 in May? | 0.99 | NO bet — direction ambiguous |
| 9 | 16:21 | BLOCKED_CHART | BTCUSD | ranger44 | Will Bitcoin dip to $55,000 in May? | 0.98 | NO bet — direction ambiguous |
| 10 | 16:21 | BLOCKED_NEWS | OIL_CRUDE | westminster | Will Crude Oil hit $120 by end of June? | **0.53** | GDELT returned no headlines |

---

## Signal Analysis

### Wallets Active Today
| Wallet | Alias | PnL | Signals | Type |
|---|---|---|---|---|
| 0xd44e974a | KrackenSruster | $265k | 3 | NO bets on extreme targets |
| 0x33559240 | ranger44 | $185k | 6 | Mix of NO bets + YES Hormuz/BTC |
| 0xbc43a2f0 | westminster | $176k | 1 | YES — Oil to $120 by June (best signal) |

### Most Interesting Signal
**#10 — westminster YES on OIL_CRUDE @ 0.53**
- "Will Crude Oil hit $120 by end of June?"
- 0.53 = genuine conviction bet (not a sure-thing)
- Passed: FOMO gate, YES filter, 4H trend gate
- Blocked only by: GDELT returning no oil headlines
- This is exactly the signal the bot is built for

---

## Why No Trade Has Been Approved Yet

### Gate Pass Rates (estimated)
| Gate | Pass Rate | Reason |
|---|---|---|
| YES bets only | ~40% | Most whale activity is NO bets selling overpriced probability |
| 4H EMA 200 trend | ~50% | Depends on market direction |
| News gate (GDELT) | ~30% | Off-peak hours, GDELT 15-min window often empty |
| Gemma BULLISH | ~50% | Of signals with headlines |
| Drift < 0.05% | ~80% | Usually fine |
| Value zone | ~40% | Price must be near 21-EMA or Fib 0.618 |

**Combined probability of APPROVED: ~1.2%**

---

## Test Trade (Manual)
Placed manually to test infrastructure end-to-end:

| Field | Value |
|---|---|
| Epic | OIL_CRUDE |
| Direction | BUY |
| Entry | 102.115 |
| Stop Loss | 101.343 |
| Take Profit | 103.274 |
| Size | 52.33 lots |
| RR | 1:1.5 |
| DealID | o_7b81be31-3e98-4c3c-bd5e-75bd44b35cfb |
| Outcome | **Hit SL** — OPEC+ output increase announcement hammered oil to 100.57 |

Thesis was valid (Iran escalation) but timing was wrong — OPEC+ supply increase overrode the geopolitical premium.

---

## Proposed Change — Relax News Gate

**Current behaviour:** No GDELT headlines → NOISE → BLOCKED
**Proposed:** No GDELT headlines → neutral → proceed to drift + value zone

**Effect:** Signals with strong whale conviction + aligned trend can pass even during quiet news windows.
**Risk:** Trade on whale + trend alone, without news confirmation. Still 3 gates enforced.

**Decision pending.**

---

## Running Instructions

```bash
# Start bot
cd /Users/abdullahabouzaid/abouzaid/uni/Genisis
python main.py

# Watch signals live
tail -f genesis_trade_log.jsonl

# Kill bot
pkill -f "python main.py"
```
