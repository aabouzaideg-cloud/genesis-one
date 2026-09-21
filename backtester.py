"""
GENESIS-ONE Backtester

Replays historical Polymarket whale trades against historical Capital.com OHLCV
to measure whether the ConfluenceGate logic has positive expectancy.

What is tested:
  ✓  FOMO killer (signal age + price drift)
  ✓  Chart geometry (21-EMA, Fib 0.618, S/R value zones)
  ✓  ATR-based SL/TP calculation
  ✓  Partial close at 1:1.5 RR + trailing stop on remainder
  ✗  News gate — skipped (cannot replay historical LLM sentiment)
                 All trades marked as news=ASSUMED_PASS in results

Usage:
    python backtester.py [--days 30] [--wallet 0xABC...]

Output:
    backtest_results.json  — full trade-by-trade log
    Console summary        — win rate, avg R:R, P&L, max drawdown
"""

import asyncio
import argparse
import json
import logging
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict
from typing import Optional
import aiohttp
import pandas as pd
from aiohttp_socks import ProxyConnector

TOR_PROXY = "socks5://127.0.0.1:9050"

from config import (
    POLYMARKET_DATA_URL, TRACKED_WALLETS,
    ATR_SL_MULTIPLIER, PARTIAL_CLOSE_RR, TRAILING_STOP_PCT,
    EMA_PERIOD, ATR_PERIOD, VALUE_ZONE_TOLERANCE, FOMO_PRICE_THRESHOLD,
)
from market_mapper import MarketMapper
from capital_client import CapitalClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
log = logging.getLogger("Backtester")

FOMO_MAX_AGE_SECONDS = 300
RESULTS_PATH         = "backtest_results.json"
CACHE_PATH           = "whale_trades_cache.json"


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class BacktestTrade:
    wallet:        str
    condition_id:  str
    question:      str
    direction:     str          # "YES" or "NO"
    epic:          str
    map_method:    str
    confidence:    float
    trade_time:    str          # ISO timestamp of whale trade
    gate_verdict:  str          # APPROVED / BLOCKED_FOMO / BLOCKED_CHART / BLOCKED_NEWS_SKIPPED
    gate_reason:   str
    # Filled only for APPROVED trades:
    entry:         Optional[float] = None
    stop_loss:     Optional[float] = None
    take_profit:   Optional[float] = None
    size_r:        float = 1.0   # always 1R for backtest normalisation
    outcome:       Optional[str] = None   # WIN_FULL / WIN_PARTIAL / LOSS / OPEN
    pnl_r:         Optional[float] = None # P&L in R multiples
    exit_price:    Optional[float] = None
    exit_reason:   Optional[str] = None   # TP1 / TRAILING_STOP / STOP_LOSS / OPEN


# ── Backtester ────────────────────────────────────────────────────────────────

class Backtester:
    def __init__(self):
        self._capital    = CapitalClient()
        self._mapper     = MarketMapper()
        self._session: Optional[aiohttp.ClientSession] = None
        self._results:  list[BacktestTrade] = []
        self._ohlcv_cache: dict = {}   # epic → (df, raw_prices)

    async def run(self, lookback_days: int = 30, wallets: list[str] = None):
        wallets = wallets or TRACKED_WALLETS
        if not wallets:
            log.error("No wallets to backtest. Add addresses to TRACKED_WALLETS in config.py.")
            return

        log.info(f"Starting backtest: {len(wallets)} wallet(s), {lookback_days} day lookback")

        self._lookback_days = lookback_days
        self._cache   = self._load_cache()
        connector     = ProxyConnector.from_url(TOR_PROXY) if not self._cache else None
        self._session = aiohttp.ClientSession(connector=connector)
        await self._capital.connect()

        # Diagnostic counters
        self._skip = {"outcome": 0, "mapper": 0, "ohlcv": 0, "chart": 0, "fomo": 0, "approved": 0}

        try:
            for wallet in wallets:
                log.info(f"Processing wallet {wallet[:10]}...")
                trades = await self._fetch_trades(wallet, lookback_days)
                log.info(f"  Found {len(trades)} historical trades")
                # Log first trade's raw fields to verify API schema
                if trades:
                    log.info(f"  Sample trade keys: {list(trades[0].keys())}")
                    log.info(f"  Sample trade: conditionId={trades[0].get('conditionId','MISSING')!r}  "
                             f"outcome={trades[0].get('outcome','MISSING')!r}  "
                             f"title={str(trades[0].get('title','MISSING'))[:60]!r}")
                for trade in trades:
                    result = await self._process_trade(wallet, trade)
                    if result:
                        self._results.append(result)
        finally:
            await self._capital.close()
            await self._session.close()

        log.info("── Drop breakdown ──────────────────────────────────────")
        for reason, count in self._skip.items():
            log.info(f"  {reason:<12}: {count}")
        log.info("────────────────────────────────────────────────────────")

        self._save_results()
        self._print_summary()

    # ── Trade Fetching ────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        """Load whale_trades_cache.json if it exists."""
        import os
        if not os.path.exists(CACHE_PATH):
            return {}
        try:
            with open(CACHE_PATH) as f:
                data = json.load(f)
            meta = data.get("_meta", {})
            log.info(f"Cache loaded: {meta.get('total_trades')} trades, "
                     f"fetched {meta.get('fetched_at', '?')[:10]}")
            return {k: v for k, v in data.items() if k != "_meta"}
        except Exception as exc:
            log.warning(f"Could not load cache: {exc}")
            return {}

    async def _fetch_trades(self, wallet: str, lookback_days: int) -> list[dict]:
        # Use cache if available (avoids needing VPN)
        if self._cache and wallet in self._cache:
            trades = self._cache[wallet]
            cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
            return [t for t in trades
                    if (lambda ts: ts/1000 if ts > 1e10 else ts)(float(t.get("timestamp", 0))) >= cutoff]

        # Live fetch fallback (requires VPN / unrestricted network)
        cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
        try:
            url    = f"{POLYMARKET_DATA_URL}/trades"
            params = {"maker_address": wallet, "limit": 500}
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Trade fetch returned {resp.status} for {wallet[:10]}...")
                    return []
                trades = await resp.json()

            if not isinstance(trades, list):
                return []

            kept = []
            for t in trades:
                ts     = float(t.get("timestamp", 0))
                ts_sec = ts / 1000 if ts > 1e10 else ts
                if ts_sec >= cutoff_ts and (t.get("side") or "").upper() == "BUY":
                    kept.append(t)
            kept.sort(key=lambda t: float(t.get("timestamp", 0)))
            return kept

        except Exception as exc:
            log.error(f"Failed to fetch trades for {wallet[:10]}...: {exc}")
            return []

    # ── Gate Simulation ───────────────────────────────────────────────────────

    async def _process_trade(self, wallet: str, trade: dict) -> Optional[BacktestTrade]:
        condition_id = trade.get("conditionId", "")
        question     = trade.get("title") or trade.get("slug") or ""
        raw_outcome  = (trade.get("outcome") or "").strip()
        ts           = float(trade.get("timestamp", 0))
        ts_sec       = ts / 1000 if ts > 1e10 else ts
        trade_time   = datetime.fromtimestamp(ts_sec, tz=timezone.utc)

        # Normalise outcome to YES/NO — covers Up/Down/Over/Under variants
        outcome = self._normalise_outcome(raw_outcome, question)
        if outcome not in ("YES", "NO"):
            log.debug(f"  SKIP outcome: raw={raw_outcome!r} → {outcome!r}  q={question[:50]!r}")
            self._skip["outcome"] += 1
            return None

        mapping = self._mapper.resolve(condition_id, question)
        if not self._mapper.is_tradeable(mapping):
            log.debug(f"  SKIP mapper: method={mapping.method} conf={mapping.confidence:.2f}  "
                      f"cid={condition_id[:12]!r}  q={question[:50]!r}")
            self._skip["mapper"] += 1
            return None   # below confidence threshold — skip silently

        epic      = mapping.epic
        direction = "BUY" if outcome == "YES" else "SELL"

        result = BacktestTrade(
            wallet=wallet,
            condition_id=condition_id,
            question=question[:80],
            direction=outcome,
            epic=epic,
            map_method=mapping.method,
            confidence=mapping.confidence,
            trade_time=trade_time.isoformat(),
            gate_verdict="",
            gate_reason="",
        )

        # ── FOMO: signal age ───────────────────────────────────────────────
        # In backtest, the signal is "detected" at polling time (~30s after trade)
        # We simulate a 30s lag
        signal_lag = 30
        if signal_lag > FOMO_MAX_AGE_SECONDS:
            self._skip["fomo"] += 1
            result.gate_verdict = "BLOCKED_FOMO"
            result.gate_reason  = "Signal too old"
            return result

        # ── Fetch OHLCV (cached per epic — all trades share same recent bars)
        ohlcv_result = await self._fetch_historical_ohlcv(epic, trade_time)
        if ohlcv_result is None:
            log.warning(f"  SKIP ohlcv: no data for epic={epic!r}")
            self._skip["ohlcv"] += 1
            return None
        df_full, all_raw = ohlcv_result
        if len(df_full) < 40:
            log.warning(f"  SKIP ohlcv: too few bars for epic={epic!r}")
            self._skip["ohlcv"] += 1
            return None

        # Split: use first half for chart setup, second half for outcome sim
        mid     = len(df_full) // 2
        df      = df_full.iloc[:mid]   # chart context
        fwd_raw = all_raw[mid:]        # forward candles (raw price dicts)

        entry_price = float(df["close"].iloc[-1])

        # ── FOMO: price drift ──────────────────────────────────────────────
        if len(df) >= 2:
            prev_close = float(df["close"].iloc[-2])
            drift = abs(entry_price - prev_close) / prev_close if prev_close > 0 else 0
            if drift > FOMO_PRICE_THRESHOLD:
                self._skip["fomo"] += 1
                result.gate_verdict = "BLOCKED_FOMO"
                result.gate_reason  = f"Price drifted {drift*100:.2f}% (threshold {FOMO_PRICE_THRESHOLD*100:.1f}%)"
                return result

        # ── Chart geometry ─────────────────────────────────────────────────
        in_zone, zone_desc = self._in_value_zone(df, entry_price)
        if not in_zone:
            self._skip["chart"] += 1
            result.gate_verdict = "BLOCKED_CHART"
            result.gate_reason  = f"Not in value zone — {zone_desc}"
            return result

        # ── News gate ─────────────────────────────────────────────────────
        self._skip["approved"] += 1
        result.gate_verdict = "APPROVED"
        result.gate_reason  = f"Chart: {zone_desc} | News: ASSUMED_PASS (not replayed)"

        # ── Simulate position ──────────────────────────────────────────────
        atr_col = [c for c in df.columns if "ATR" in c.upper()]
        atr     = float(df[atr_col[0]].iloc[-1]) if atr_col else entry_price * 0.001

        sl_dist = ATR_SL_MULTIPLIER * atr
        tp_dist = sl_dist * PARTIAL_CLOSE_RR

        if direction == "BUY":
            sl = entry_price - sl_dist
            tp = entry_price + tp_dist
        else:
            sl = entry_price + sl_dist
            tp = entry_price - tp_dist

        result.entry       = round(entry_price, 5)
        result.stop_loss   = round(sl, 5)
        result.take_profit = round(tp, 5)

        # Simulate outcome using forward candles already in memory
        outcome_data = self._simulate_outcome(fwd_raw, direction, entry_price, sl, tp)
        result.outcome     = outcome_data["outcome"]
        result.pnl_r       = outcome_data["pnl_r"]
        result.exit_price  = outcome_data["exit_price"]
        result.exit_reason = outcome_data["exit_reason"]

        log.info(
            f"  [{result.outcome}] {direction} {epic} @ {entry_price:.5f} "
            f"| P&L={result.pnl_r:+.2f}R | exit={result.exit_reason}"
        )
        return result

    # ── OHLCV Fetching ────────────────────────────────────────────────────────

    # Maps Capital.com epic → yfinance ticker
    _YF_TICKER = {
        "BTCUSD":    "BTC-USD",
        "OIL_CRUDE": "CL=F",
    }

    def _ensure_yf_history(self, epic: str, lookback_days: int = 60) -> Optional[pd.DataFrame]:
        """Download full yfinance history for an epic (once per session)."""
        if not hasattr(self, "_yf_cache"):
            self._yf_cache: dict = {}
        if epic in self._yf_cache:
            return self._yf_cache[epic]

        ticker = self._YF_TICKER.get(epic)
        if not ticker:
            log.warning(f"No yfinance ticker for epic {epic!r}")
            self._yf_cache[epic] = None
            return None

        try:
            import yfinance as yf
            # 15-min data available up to 60 days back on yfinance
            df = yf.download(ticker, period=f"{lookback_days}d", interval="15m",
                             auto_adjust=True, progress=False)
            if df.empty:
                log.warning(f"yfinance returned empty data for {ticker}")
                self._yf_cache[epic] = None
                return None

            df.index = pd.to_datetime(df.index, utc=True)
            df.columns = [c[0].lower() if isinstance(c, tuple) else c.lower()
                          for c in df.columns]
            log.info(f"  yfinance {ticker}: {len(df)} bars downloaded")
            self._yf_cache[epic] = df
            return df
        except Exception as exc:
            log.warning(f"yfinance download failed for {ticker}: {exc}")
            self._yf_cache[epic] = None
            return None

    async def _fetch_historical_ohlcv(
        self, epic: str, at_time: datetime, bars: int = 200
    ) -> Optional[tuple]:
        """
        Fetch 200 bars (100 before + 100 after at_time) from yfinance history.
        Returns (numeric_df, raw_prices_list) matching the original interface,
        where raw_prices_list contains Capital.com-style dicts for _simulate_outcome.
        """
        half = bars // 2
        df_full = self._ensure_yf_history(epic, getattr(self, "_lookback_days", 60))
        if df_full is None:
            return None

        # Ensure at_time is tz-aware UTC
        if at_time.tzinfo is None:
            at_time = at_time.replace(tzinfo=timezone.utc)

        try:
            # Find the index position closest to at_time
            pos = df_full.index.searchsorted(at_time)
            start = max(0, pos - half)
            end   = min(len(df_full), pos + half)
            window = df_full.iloc[start:end]
            if len(window) < 40:
                return None

            numeric = window[["open", "high", "low", "close"]].copy().astype(float)
            import pandas_ta as ta
            numeric.ta.ema(length=EMA_PERIOD, append=True)
            numeric.ta.atr(length=ATR_PERIOD, append=True)

            # Build Capital.com-style raw price dicts for _simulate_outcome
            def _make_raw(row) -> dict:
                o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
                mk = lambda v: {"bid": v, "ask": v}
                return {
                    "snapshotTimeUTC": str(row.name),
                    "openPrice":  mk(o),
                    "highPrice":  mk(h),
                    "lowPrice":   mk(l),
                    "closePrice": mk(c),
                }

            raw_prices = [_make_raw(window.iloc[i]) for i in range(len(window))]
            return (numeric, raw_prices)

        except Exception as exc:
            log.debug(f"OHLCV slice failed for {epic} at {at_time}: {exc}")
            return None

    def _simulate_outcome(
        self,
        prices:    list,
        direction: str,
        entry:     float,
        sl:        float,
        tp:        float,
    ) -> dict:
        """
        Walk forward candles to determine trade outcome.
        Uses pre-fetched second-half bars — no extra API call needed.
        """
        if not prices:
            return {"outcome": "OPEN", "pnl_r": 0.0, "exit_price": None, "exit_reason": "NO_FWD_DATA"}

        sl_dist      = abs(entry - sl) or entry * 0.001
        partial_done = False
        trail_stop   = sl

        for p in prices:
            high  = (p["highPrice"]["bid"]  + p["highPrice"]["ask"])  / 2
            low   = (p["lowPrice"]["bid"]   + p["lowPrice"]["ask"])   / 2
            close = (p["closePrice"]["bid"] + p["closePrice"]["ask"]) / 2

            if direction == "BUY":
                # Check stop loss
                if low <= sl if not partial_done else low <= trail_stop:
                    stop_hit = sl if not partial_done else trail_stop
                    loss_r   = -1.0 if not partial_done else -0.5 + (abs(entry - stop_hit) / sl_dist * -0.5)
                    return {
                        "outcome":     "LOSS" if not partial_done else "WIN_PARTIAL",
                        "pnl_r":       round(loss_r, 3),
                        "exit_price":  round(stop_hit, 5),
                        "exit_reason": "STOP_LOSS" if not partial_done else "TRAILING_STOP",
                    }
                # Check TP1
                if not partial_done and high >= tp:
                    partial_done = True
                    trail_stop   = entry   # move stop to breakeven after partial close
                    log.debug(f"  TP1 hit at {tp:.5f}")
                # Update trailing stop
                if partial_done:
                    new_trail = close * (1 - TRAILING_STOP_PCT)
                    if new_trail > trail_stop:
                        trail_stop = new_trail

            else:  # SELL
                if high >= sl if not partial_done else high >= trail_stop:
                    stop_hit = sl if not partial_done else trail_stop
                    loss_r   = -1.0 if not partial_done else -0.5 + (abs(entry - stop_hit) / sl_dist * -0.5)
                    return {
                        "outcome":     "LOSS" if not partial_done else "WIN_PARTIAL",
                        "pnl_r":       round(loss_r, 3),
                        "exit_price":  round(stop_hit, 5),
                        "exit_reason": "STOP_LOSS" if not partial_done else "TRAILING_STOP",
                    }
                if not partial_done and low <= tp:
                    partial_done = True
                    trail_stop   = entry
                if partial_done:
                    new_trail = close * (1 + TRAILING_STOP_PCT)
                    if new_trail < trail_stop or trail_stop == sl:
                        trail_stop = new_trail

        # Position still open after all candles
        sl_dist    = abs(entry - sl)
        last_close = (prices[-1]["closePrice"]["bid"] + prices[-1]["closePrice"]["ask"]) / 2
        if partial_done:
            open_pnl = abs(last_close - entry) / sl_dist * 0.5 if sl_dist else 0
            return {
                "outcome":     "OPEN",
                "pnl_r":       round(0.75 + open_pnl, 3),
                "exit_price":  round(last_close, 5),
                "exit_reason": "STILL_OPEN_AFTER_TP1",
            }
        return {
            "outcome":     "OPEN",
            "pnl_r":       0.0,
            "exit_price":  round(last_close, 5),
            "exit_reason": "STILL_OPEN",
        }

    # ── Value Zone (mirrors ConfluenceGate logic) ─────────────────────────────

    def _in_value_zone(self, df: pd.DataFrame, price: float) -> tuple[bool, str]:
        ema_col = f"EMA_{EMA_PERIOD}"
        ema     = float(df[ema_col].iloc[-1]) if ema_col in df.columns else None

        if ema and self._near(price, ema):
            return True, f"21-EMA @ {ema:.5f}"

        high   = df["high"].tail(50).max()
        low    = df["low"].tail(50).min()
        fib618 = high - 0.618 * (high - low)
        if self._near(price, fib618):
            return True, f"Fib 0.618 @ {fib618:.5f}"

        recent = df.tail(20)
        for series in [recent["high"], recent["low"]]:
            vals = series.values
            for i in range(1, len(vals) - 1):
                is_swing = (vals[i] > vals[i-1] and vals[i] > vals[i+1]) or \
                           (vals[i] < vals[i-1] and vals[i] < vals[i+1])
                if is_swing and self._near(price, vals[i]):
                    return True, f"S/R @ {vals[i]:.5f}"

        nearest = min(filter(None, [ema, fib618]), key=lambda x: abs(x - price), default=fib618)
        return False, f"nearest={nearest:.5f}"

    def _normalise_outcome(self, outcome: str, question: str) -> str:
        """
        Map Polymarket outcome strings to YES/NO.
        Handles: Yes/No, Up/Down, Over/Under, Above/Below, closes above/below.
        """
        o = outcome.lower()
        q = question.lower()
        # Direct matches
        if o in ("yes", "up", "over", "above", "higher", "long"):
            return "YES"
        if o in ("no", "down", "under", "below", "lower", "short"):
            return "NO"
        # Question context: "closes above" / "reaches" → YES is bullish
        if "closes above" in q or "reaches" in q or "hits" in q:
            if o == "yes":
                return "YES"
            if o == "no":
                return "NO"
        return outcome.upper()

    def _near(self, price: float, level: float) -> bool:
        return abs(price - level) / level <= VALUE_ZONE_TOLERANCE

    # ── Reporting ─────────────────────────────────────────────────────────────

    def _save_results(self):
        with open(RESULTS_PATH, "w") as f:
            json.dump([asdict(r) for r in self._results], f, indent=2)
        log.info(f"Results saved to {RESULTS_PATH}")

    def _print_summary(self):
        if not self._results:
            print("\n[BACKTEST] No results to summarise.")
            return

        approved = [r for r in self._results if r.gate_verdict == "APPROVED"]
        blocked  = [r for r in self._results if r.gate_verdict != "APPROVED"]

        wins     = [r for r in approved if r.pnl_r and r.pnl_r > 0]
        losses   = [r for r in approved if r.pnl_r and r.pnl_r <= 0]
        open_pos = [r for r in approved if r.outcome == "OPEN"]

        total_pnl    = sum(r.pnl_r for r in approved if r.pnl_r)
        win_rate     = len(wins) / len(approved) * 100 if approved else 0
        avg_win      = sum(r.pnl_r for r in wins) / len(wins) if wins else 0
        avg_loss     = sum(r.pnl_r for r in losses) / len(losses) if losses else 0
        expectancy   = (win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss)

        # Max drawdown (running total)
        running = 0.0
        peak    = 0.0
        max_dd  = 0.0
        for r in approved:
            if r.pnl_r:
                running += r.pnl_r
                peak     = max(peak, running)
                max_dd   = min(max_dd, running - peak)

        # Block breakdown
        block_counts = {}
        for r in blocked:
            block_counts[r.gate_verdict] = block_counts.get(r.gate_verdict, 0) + 1

        print("\n" + "═" * 60)
        print("  GENESIS-ONE BACKTEST RESULTS")
        print("═" * 60)
        print(f"  Total signals evaluated : {len(self._results)}")
        print(f"  Approved (took trade)   : {len(approved)}")
        print(f"  Blocked by gate         : {len(blocked)}")
        for verdict, count in block_counts.items():
            print(f"    └─ {verdict:<25} {count}")
        print(f"\n  ── Performance (approved trades) ──")
        print(f"  Wins                    : {len(wins)}")
        print(f"  Losses                  : {len(losses)}")
        print(f"  Still open              : {len(open_pos)}")
        print(f"  Win rate                : {win_rate:.1f}%")
        print(f"  Avg win                 : +{avg_win:.2f}R")
        print(f"  Avg loss                : {avg_loss:.2f}R")
        print(f"  Expectancy per trade    : {expectancy:+.3f}R")
        print(f"  Total P&L               : {total_pnl:+.2f}R")
        print(f"  Max drawdown            : {max_dd:.2f}R")
        print(f"\n  NOTE: News gate was ASSUMED_PASS for all trades.")
        print(f"        Live results will be more selective (fewer trades).")
        print("═" * 60 + "\n")


# ── Entry Point ───────────────────────────────────────────────────────────────

async def _main():
    parser = argparse.ArgumentParser(description="GENESIS-ONE Backtester")
    parser.add_argument("--days",   type=int,  default=30,  help="Lookback days (default: 30)")
    parser.add_argument("--wallet", type=str,  default=None, help="Single wallet to test (overrides config)")
    args = parser.parse_args()

    wallets = [args.wallet] if args.wallet else TRACKED_WALLETS
    bt = Backtester()
    await bt.run(lookback_days=args.days, wallets=wallets)


if __name__ == "__main__":
    asyncio.run(_main())
