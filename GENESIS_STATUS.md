# GENESIS-ONE — Session Status (2026-05-05)

## Bot Status
- **Mode:** DEMO (Capital.com demo account)
- **State:** Running, WebSocket connected, whale signals flowing
- **No trades executed yet** — gates working correctly, waiting for full confluence

## What's Built (complete)

### Architecture
```
Polymarket WebSocket
      ↓
WhaleMonitor (40 wallets, consensus logic)
      ↓
MarketMapper (94 curated markets → Capital.com epics)
      ↓
ConfluenceGate (7 gates)
      ↓
RiskManager (Kelly Criterion + circuit breaker)
      ↓
Capital.com order execution (DEMO)
      ↓
Telegram alerts + JSONL trade log
```

### 7-Gate Confluence Filter
1. FOMO age (< 5 min)
2. Macro trend (4H EMA 200 slope)
3. **[NEW]** Macro calendar (Forex Factory — block CPI/NFP/FOMC within 2h)
4. News logic (RSS → Gemma → Claude Haiku dual-LLM)
5. FOMO drift (< 1% price move during news evaluation)
6. Chart geometry (Value Zone: 21-EMA / Fib 0.618 / S/R)
7. Candle geometry (no upper wick rejection on BUY)

### Whale Coverage
- **40 wallets** tracked
- **94 curated Polymarket markets** mapped to Capital.com epics
- **188 WebSocket token subscriptions**
- **13 epics:** BTCUSD, ETHUSD, GOLD, SILVER, OIL_CRUDE, NATGAS, US500, US100, US30, DE30, EURUSD, USDJPY, GBPUSD

### Multi-Wallet Consensus
- Standard: 2+ wallets same direction within 10 min → signal fires
- Elite bypass: EB99999 (94% WR), ifpl (93% WR), Ume-Chang (89% WR) — fire immediately

### News Stack (three layers)
- Layer 1: RSS (Reuters, BBC, CNBC, CoinDesk, CoinTelegraph, BBC Middle East) + GDELT fallback
- Layer 2: Gemma 3:4b (local) + Claude Haiku (API cross-check) — both must agree BULLISH
- Layer 3: Forex Factory economic calendar — blocks high-impact events within 2h

### Risk
- Kelly Criterion: f* = (p*(b+1)-1)/b, hard cap 2%
- Win priors: HIGH=0.62, MEDIUM=0.55, LOW=0.50
- Daily circuit breaker: 5% loss → close all + Telegram

## Today's Signal Summary (2026-05-05)

| Time (UTC) | Epic | Whale | Question | Block Reason |
|---|---|---|---|---|
| 14:25 | ETHUSD | KrackenSruster | ETH dip $1,200 May? | Trend — 4H EMA rising, no SELL |
| 14:35 | OIL | KrackenSruster | WTI hit $150 May? | NO bet skipped |
| 14:51 | BTCUSD | KrackenSruster | BTC reach $95k May? | NO bet skipped |
| 15:19 | BTCUSD | ranger44 | BTC dip $45k May? | NO bet skipped |
| 15:24 | BTCUSD | ranger44 | BTC dip $50k May? | NO bet skipped |
| 15:30 | OIL | ranger44 | 100-124 ships Hormuz Apr27-May3? | News NOISE |
| 15:50 | BTCUSD | ranger44 | BTC above $74k May 6? | News NOISE |
| 16:00 | ETHUSD | ranger44 | ETH dip $1,400 May? | NO bet skipped |
| 16:21 | BTCUSD | ranger44 | BTC dip $55k May? | NO bet skipped |
| 16:21 | OIL | westminster | CL hit $120 June? | News NOISE |
| 16:34 | OIL | KrackenSruster | Kharg Island control May 31? | NO bet skipped |
| 16:34 | ETHUSD | KrackenSruster | ETH dip $1,200 May? | NO bet skipped |
| 16:39 | OIL | KrackenSruster | 100-124 ships Hormuz? | News NOISE |
| 16:39 | OIL | KrackenSruster | 75-99 ships Hormuz? | NO bet skipped |
| 16:43 | OIL | ranger44 | 75-99 ships Hormuz? | NO bet skipped |
| 16:43 | OIL | KrackenSruster | 100-124 ships Hormuz? | Candle wick rejection |
| 16:49 | OIL | KrackenSruster | Hormuz blockade lifted May 8? | Candle wick rejection |

**Closest to approval:** OIL_CRUDE Hormuz ship transit — passed trend + drift gates, only blocked by news NOISE then wick.

## Files Changed This Session

| File | What Changed |
|---|---|
| `whale_monitor.py` | Multi-wallet consensus, ELITE_WALLETS, consensus_count field |
| `confluence_gate.py` | 7 gates (was 6), added calendar gate (Gate 3), BLOCKED_CALENDAR verdict |
| `risk_manager.py` | Kelly Criterion sizing, daily circuit breaker, _trip_circuit() |
| `config.py` | KELLY params, DAILY_LOSS_LIMIT, ANTHROPIC_API_KEY, CLAUDE_VERIFY_MODEL, 5 new wallets |
| `market_mapper.py` | 4 new epics (SILVER, NATGAS, US30, DE30), Russia-Ukraine keywords |
| `polygon_client.py` | SILVER + US30 tickers, new epic keywords |
| `market_map.json` | 17 → 94 curated markets (WTI, CL, GC, BTC, ETH, Silver, Fed cuts, Russia-Ukraine) |
| `news_sentinel.py` | FULL REWRITE — three-layer architecture (RSS + dual-LLM + calendar) |
| `core_engine.py` | Passes gate.confidence to risk.calculate() |

## Pending — Next Session

1. **Add ANTHROPIC_API_KEY to `.env`** — Claude Haiku is disabled without it
   ```
   ANTHROPIC_API_KEY=sk-ant-...
   ```

2. **Fix News NOISE on curated markets** — Hormuz ship transit / BTC price targets have no direct RSS headlines. Consider: bypass news gate for curated + consensus (≥2 wallets) signals, since the curated map already guarantees relevance.

3. **NO bet handling** — Whales betting NO on "Will BTC dip to $45k?" = they're bullish on BTC. Bot skips these. Consider: for negatively-framed curated markets, invert direction (NO bet → BUY the underlying).

## How to Run

```bash
cd /Users/abdullahabouzaid/abouzaid/uni/Genisis

# Kill any running instance first
pkill -f "python main.py"

# Start bot
python main.py >> genesis_trade_log.jsonl 2>&1 &

# Watch logs
tail -f genesis_trade_log.jsonl
```

## Key Config Locations
- `.env` — API keys (Capital.com, Polygon, Telegram, Anthropic)
- `config.py` — all tunable parameters
- `market_map.json` — curated Polymarket → epic mappings
