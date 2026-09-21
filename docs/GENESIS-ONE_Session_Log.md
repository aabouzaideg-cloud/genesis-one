# GENESIS-ONE — Session Log
*Last updated: 2026-05-05*

---

## What Is GENESIS-ONE

Autonomous trading bot that watches Polymarket whale wallets and executes CFD trades on Capital.com when those whales take positions on curated macro markets (Iran, Hormuz, BTC $150k, etc.).

**Stack:** Python · Polymarket Data API · Capital.com REST API · Ollama/Gemma (news gate) · Tor (UAE bypass)

---

## Architecture Overview

```
Polymarket Whale Wallets
        ↓  (poll every 30s via Tor)
whale_monitor.py   →   core_engine.py
                              ↓
                    market_mapper.py       (conditionId → Capital.com epic)
                              ↓
                    confluence_gate.py     (EMA, Fib 0.618, ATR, news LLM)
                              ↓
                    capital_client.py      (place/manage CFD trade)
                              ↓
                    position_store.py      (track open positions)
```

**Entry point:** `python main.py` (DEMO mode by default)

---

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point |
| `core_engine.py` | Main loop, orchestration |
| `whale_monitor.py` | Polls Polymarket data API per wallet |
| `market_mapper.py` | Maps conditionId → Capital.com epic |
| `confluence_gate.py` | Trade filter: EMA, Fib, ATR, news |
| `capital_client.py` | Capital.com REST + session keep-alive |
| `risk_manager.py` | Position sizing, SL/TP |
| `news_sentinel.py` | Ollama/Gemma news gate |
| `position_store.py` | Tracks open positions |
| `config.py` | All constants, API keys, wallet list |
| `market_map.json` | Curated conditionId → epic mappings |
| `backtester.py` | Offline strategy evaluator |
| `fetch_trades_cache.py` | Pre-fetches whale trade history |
| `scan_psw.py` | Scans PolySmartWallet leaderboard for whale wallets |
| `scan_blockchain_whales.py` | Scans Polygon blockchain via Etherscan API |
| `scan_macro_whales.py` | Scans Polymarket data API for macro trades |

---

## Config

### Tracked Wallets (`config.py → TRACKED_WALLETS`)

15 wallets sourced from PolySmartWallet Politics leaderboard — all verified to hold positions on curated macro markets:

| Wallet | Username | PNL | Markets |
|---|---|---|---|
| `0xbaa2bcb5...` | denizz | $1.77M | Iran-regime, US-invade, Kharg |
| `0x5739ddf8...` | hopedieslast | $514k | Hormuz, US-Iran-nuke, peace |
| `0xd44e974a...` | KrackenSruster | $265k | Kharg-May31/June30, US-invade |
| `0x335592400...` | ranger44 | $185k | Iran-nuke, Hormuz |
| `0xbc43a2f0...` | westminster | $176k | Iran-regime-fall |
| `0x9e7e57fb...` | mcorleone | $92k | Hormuz, US-invade |
| `0xb8b44e3c...` | MonacoLover | $42k | US-Iran-nuke, Hormuz |
| `0x5652090d...` | AlphaBetaGamma | $35k | BTC-150k-June30/Dec31 |
| `0x6f62410c...` | Adrimarket | $38k | Iran-diplomatic |
| `0xae89da65...` | Ume-Chang | $27k wr=89% | US-Iran-peace |
| `0x654ab106...` | Sakura-Kinomoto | $7k | Hormuz, US-Iran |
| `0x49ebd7ba...` | Madnox | $11k | Iran-regime, Kharg |
| `0x6d16c33b...` | slippy0 | $11k | US-invade, Kharg-June30, Hormuz |
| `0x1b824fc8...` | zoomer1 | $15k | US-Iran-nuke, regime-fall |
| `0xeafc14c8...` | CitriniResearch | $34k | Iran-airspace |

### Curated Markets (`market_map.json → curated`)

| conditionId (prefix) | Description | Epic | Volume |
|---|---|---|---|
| `0x9352c559...` | Will Iranian regime fall by June 30? | OIL_CRUDE | $36.3M |
| `0x5db999fa...` | Will US invade Iran before 2027? | OIL_CRUDE | $21.4M |
| `0x924a2942...` | Strait of Hormuz traffic returns to normal by end of April? | OIL_CRUDE | $36.4M |
| `0xa0f4c492...` | Bitcoin price hits $150k before July 1? | BTCUSD | $15.7M |
| `0xa1d97efb...` | Kharg Island no longer under Iranian control by May 31? | OIL_CRUDE | $4.8M |
| `0x6897736d...` | Kharg Island no longer under Iranian control by June 30? | OIL_CRUDE | $2.1M |
| `0xa70fc369...` | US-Iran nuclear deal by June 30? | OIL_CRUDE | $1.5M |
| `0x02deb953...` | Bitcoin price hits $150k by Dec 31? | BTCUSD | — |

### Risk Parameters

| Parameter | Value |
|---|---|
| Max risk per trade | 1% of account |
| Min margin | 350% |
| ATR SL multiplier | 1.5× |
| Partial close at | 1:1.5 RR (close 50%) |
| Trailing stop | 0.3% |
| FOMO threshold | 1% price move = signal expired |
| Signal max age | 300 seconds |

---

## Session Work Log

### Problem 1: All Polymarket conditionIds were stale
All 7 original conditionIds in `market_map.json` returned wrong or non-existent markets.

**Fix:** Paginated the Gamma API events endpoint (`gamma-api.polymarket.com/events`), scanned 2,100 events, located live high-volume macro markets by slug/description. Replaced the entire curated section with 8 verified conditionIds.

---

### Problem 2: Polymarket APIs blocked in UAE
`clob.polymarket.com` and other subdomains return "Connection reset by peer" without a proxy.

**Fix:** Route all Polymarket API calls through Tor (`socks5://127.0.0.1:9050`) using `aiohttp_socks.ProxyConnector`. Etherscan/Polygonscan calls go direct (not blocked).

Start Tor: `brew services start tor`

---

### Problem 3: Whale wallet discovery
Original wallets were placeholders — needed real wallets actively trading the curated macro markets.

**Approaches tried:**

**A) Polymarket data API scan (`scan_macro_whales.py`)**
- `GET data-api.polymarket.com/trades?maker_address=X`
- API field for wallet is `proxyWallet` (not `maker` or `makerAddress`)
- `conditionId` param is ignored server-side — must filter client-side
- Rolling ~3,500 record window — macro trades extremely sparse
- Result: very few hits

**B) Blockchain scan (`scan_blockchain_whales.py`)**
- Etherscan V2 API (`api.etherscan.io/v2/api`, chainid=137 for Polygon)
- Scanned ERC-1155 `TransferSingle` events on ConditionalTokens + NegRiskAdapter contracts
- Result: 0 logs — Polymarket uses CLOB order-hash events, not standard ERC-1155 mints
- Approach abandoned

**C) PolySmartWallet leaderboard (`scan_psw.py`)** ← Used
- `polysmartwallet.com/api/leaderboard.php?page=N&limit=15&category=Politics`
- `polysmartwallet.com/api/profile.php?address=0x...`
- Found 46 traders with Iran/Kharg/Hormuz keyword matches in profile positions
- Top 15 by PNL selected → now in `config.py`

---

### Problem 4: Backtester crash
```
TypeError: object of type 'NoneType' has no len()
File "backtester.py", line 245, in _process_trade
    if len(df_full) < 40:
```

**Root cause:** `_fetch_historical_ohlcv` returned `(None, [])` on empty data. Caller unpacked it as `df_full, all_raw = (None, [])`, then crashed on `len(None)`.

**Fix:** Changed both `return None, []` instances in `_fetch_historical_ohlcv` to `return None`. Caller already checked `if ohlcv_result is None` correctly.

---

### Problem 5: Backtester using single OHLCV snapshot for all trades
Every trade evaluated against today's Capital.com DEMO price ($79,714 → $80,199). All outcomes identical: 100% losses.

**Root cause:** `_ohlcv_cache` keyed by `epic` only — first fetch cached current prices, all subsequent calls returned same data. Capital.com DEMO API doesn't support fetching bars at a past timestamp.

**Fix:** Replaced `_fetch_historical_ohlcv` to use **yfinance** for historical data:
- `BTC-USD` for BTCUSD
- `CL=F` for OIL_CRUDE
- Downloads full 60-day / 15-min history once per epic
- Slices 200 bars centered on `at_time` using `searchsorted`
- Converts yfinance rows to Capital.com-style dicts so `_simulate_outcome` works unchanged

Install: `pip install yfinance`

---

### Problem 6: Mapper generating false signals from daily binary markets
3,147 of 6,300 cached trades were "Bitcoin Up or Down - May 5, 5:50AM-5:55AM ET" type markets — short-term 5-minute binary markets. The keyword-matching layer in `market_mapper.py` was mapping these to BTCUSD, flooding the backtest (and the live bot) with irrelevant signals.

**Fix:** Disabled keyword-level matching in `market_mapper.resolve()`. Only exact curated conditionId matches generate signals now. One-line change:

```python
# Before (market_mapper.py):
result = self._level2_keywords(question)
if result:
    return result

# After: removed — curated-only
```

---

### Backtest Result After All Fixes

With curated-only mapper:
- 6,300 cached trades → 5,352 filtered (wrong market) → **3 approved** (all Hormuz)
- 3 trades is statistically meaningless — backtest cannot evaluate the strategy

**Why the backtest can't work:**
The Polymarket data API returns only the 500 most recent trades per wallet. These wallets trade hundreds of binary markets daily. Their actual macro market positions (Iran, Hormuz, BTC $150k) happened weeks ago and are no longer in the API window. Only 3 of 6,300 cached trades hit a curated macro market.

**Conclusion:** The live bot is architecturally correct. It polls in real-time and will catch macro trades as they happen. A meaningful backtest would require a different data source (CLOB historical fills, blockchain event logs with decoded wallet addresses).

---

## Running the Bot

### Prerequisites
```bash
# Tor must be running (UAE bypass)
brew services start tor

# .env file in project root
TRADING_MODE=DEMO
CAPITAL_API_KEY=...
CAPITAL_PASSWORD=...
CAPITAL_IDENTIFIER=...
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_MODEL=gemma3:4b
```

### Start
```bash
cd ~/abouzaid/uni/Genisis
python main.py
```

### Backtest (limited utility — see above)
```bash
python backtester.py --days 60
```

### Refresh whale trade cache
```bash
python fetch_trades_cache.py
```

### Find new whale wallets
```bash
python scan_psw.py          # PolySmartWallet leaderboard
```

---

## Key API Endpoints

| Service | Endpoint | Notes |
|---|---|---|
| Polymarket trades | `data-api.polymarket.com/trades?maker_address=0x...` | 500 most recent, wallet field = `proxyWallet` |
| Polymarket CLOB | `clob.polymarket.com/markets/{conditionId}` | Requires Tor from UAE |
| Polymarket Gamma | `gamma-api.polymarket.com/events?limit=100&offset=N` | Public, no auth |
| PolySmartWallet | `polysmartwallet.com/api/leaderboard.php?page=N&limit=15&category=Politics` | Public |
| PSW Profile | `polysmartwallet.com/api/profile.php?address=0x...` | Returns positions + activity |
| Capital.com DEMO | `demo-api-capital.backend-capital.com` | Auth via session token |
| Etherscan V2 | `api.etherscan.io/v2/api?chainid=137` | Free key, Polygon |

---

## Known Limitations

1. **Backtest data depth** — Polymarket data API only returns 500 most recent trades per wallet. Active traders exhaust this in 1-3 days. No way to get 60-day macro trade history via this API.

2. **Capital.com DEMO OHLCV** — DEMO API returns current prices only, not true historical data. Backtester now bypasses this with yfinance.

3. **Hormuz market expired** — The Hormuz "by end of April" market may already be resolved. May need to find its successor or add the next Hormuz market conditionId to `market_map.json`.

4. **Keyword mapper disabled** — Level 2 keyword matching is off. If a high-confidence macro market appears that isn't in the curated list yet, the bot won't trade it until its conditionId is manually added to `market_map.json`.

---

## Next Steps

- [ ] Run live bot: `python main.py`
- [ ] Monitor logs for first real macro whale signal
- [ ] Check if Hormuz market is still active — update conditionId if resolved
- [ ] Deploy to VPS for 24/7 operation
- [ ] After 30+ days of live data, run a meaningful backtest
