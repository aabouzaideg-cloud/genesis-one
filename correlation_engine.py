"""
CorrelationEngine — inter-market lead/lag signal generator.

Monitors two high-confidence correlation pairs every 5 minutes:

  GOLD ↔ US500  (inverse)  — risk-off/risk-on rotation
  BTC  ↔ ETH   (positive) — crypto co-movement

Logic per pair:
  1. Measure leader's price move over the last two 15m bars
  2. If leader moved more than MIN_MOVE threshold:
     a. Determine expected follower direction
     b. Measure follower's actual move over the same window
     c. If follower is lagging by more than LAG_THRESHOLD → signal
  3. Emit ScanSignal into the shared scan queue (same as MarketScanner)
     → goes through ConfluenceGate.evaluate_scan() before any trade fires

Signals are tagged source="correlation" in ScanSignal so logs are clear.
"""

import asyncio
import logging
import time
from dataclasses import dataclass

from market_scanner import ScanSignal, SIGNAL_COOLDOWN

log = logging.getLogger("CorrelationEngine")

SCAN_INTERVAL = 300     # 5 minutes — faster than MarketScanner to catch lead-lag windows

# Correlation pair definitions
# direction: "inverse" = leader ↑ → follower ↓ (and vice versa)
#            "positive" = leader ↑ → follower ↑
PAIRS = [
    {
        "leader":        "GOLD",
        "follower":      "US500",
        "direction":     "inverse",
        "min_move":      0.003,       # leader must move ≥ 0.3% to trigger
        "lag_threshold": 0.003,       # follower must be lagging ≥ 0.3%
        "label":         "Gold/Equities inverse",
    },
    {
        "leader":        "US500",
        "follower":      "GOLD",
        "direction":     "inverse",
        "min_move":      0.003,
        "lag_threshold": 0.003,
        "label":         "Equities/Gold inverse",
    },
    {
        "leader":        "BTCUSD",
        "follower":      "ETHUSD",
        "direction":     "positive",
        "min_move":      0.005,       # crypto needs bigger move — more volatile baseline
        "lag_threshold": 0.003,
        "label":         "BTC/ETH co-movement",
    },
]


class CorrelationEngine:
    def __init__(self, capital_client, signals_queue: asyncio.Queue):
        self._capital  = capital_client
        self._signals  = signals_queue
        self._running  = False
        self._emitted: dict[str, float] = {}   # ckey → last emission time (45min refire guard)
        self._traded:  dict[str, float] = {}   # ckey → last approved trade time (4h cooldown)

    async def run(self):
        self._running = True
        log.info("[CORR] Correlation engine started — scanning pairs every 5 minutes.")
        await asyncio.sleep(120)   # let CapitalClient settle after startup
        while self._running:
            await self._scan_all_pairs()
            await asyncio.sleep(SCAN_INTERVAL)

    def stop(self):
        self._running = False

    def set_trade_cooldown(self, epic: str, direction: str):
        """Called by core_engine after a correlation trade is approved and placed."""
        ckey = f"{epic}:{direction}:corr"
        self._traded[ckey] = time.time()
        log.debug(f"[CORR] Trade cooldown set for {epic} {direction} (4h)")

    async def _scan_all_pairs(self):
        for pair in PAIRS:
            try:
                await self._scan_pair(pair)
            except Exception as exc:
                log.warning(
                    f"[CORR] Pair {pair['leader']}/{pair['follower']} error: {exc}"
                )

    async def _scan_pair(self, pair: dict):
        leader   = pair["leader"]
        follower = pair["follower"]

        # Fetch OHLCV to get the baseline (last closed bar) price
        leader_df   = await self._capital.get_ohlcv(leader)
        follower_df = await self._capital.get_ohlcv(follower)

        if leader_df is None or follower_df is None:
            return
        if len(leader_df) < 3 or len(follower_df) < 3:
            return

        # Baseline = close two bars ago; current = close of last completed bar
        leader_prev    = float(leader_df["close"].iloc[-2])
        leader_now     = float(leader_df["close"].iloc[-1])
        follower_prev  = float(follower_df["close"].iloc[-2])
        follower_now   = float(follower_df["close"].iloc[-1])

        leader_move   = (leader_now - leader_prev) / leader_prev   # signed %
        follower_move = (follower_now - follower_prev) / follower_prev

        # Leader hasn't moved enough — nothing to react to
        if abs(leader_move) < pair["min_move"]:
            log.debug(
                f"[CORR] {leader}/{follower} — leader move {leader_move*100:.3f}% "
                f"< threshold {pair['min_move']*100:.1f}%, skip"
            )
            return

        # Determine expected follower direction based on leader move + correlation type
        if pair["direction"] == "inverse":
            follower_direction = "SELL" if leader_move > 0 else "BUY"
        else:  # positive
            follower_direction = "BUY" if leader_move > 0 else "SELL"

        # Calculate how much the follower has already reacted in the expected direction
        if follower_direction == "BUY":
            already_reacted = max(follower_move, 0)   # positive move = reacted
        else:
            already_reacted = max(-follower_move, 0)  # negative move = reacted

        lag = abs(leader_move) - already_reacted

        if lag < pair["lag_threshold"]:
            log.debug(
                f"[CORR] {leader}/{follower} — follower already reacted "
                f"(lag={lag*100:.3f}% < {pair['lag_threshold']*100:.1f}%), skip"
            )
            return

        # Cooldown check — 4h after approved trade, 45min refire guard
        ckey = f"{follower}:{follower_direction}:corr"
        now  = time.time()
        if now - self._traded.get(ckey, 0) < SIGNAL_COOLDOWN:
            log.debug(f"[CORR] {follower} {follower_direction} — post-trade cooldown active")
            return
        if now - self._emitted.get(ckey, 0) < 45 * 60:
            log.debug(f"[CORR] {follower} {follower_direction} — refire cooldown active")
            return

        # Fetch ATR from follower OHLCV for sizing
        atr_col = next((c for c in follower_df.columns if "ATR" in c), None)
        ema_col = next((c for c in follower_df.columns if "EMA" in c), None)
        atr     = float(follower_df[atr_col].iloc[-1]) if atr_col else 0.0
        ema21   = float(follower_df[ema_col].iloc[-1]) if ema_col else follower_now

        leader_dir_str = "↑" if leader_move > 0 else "↓"
        reason = (
            f"[CORRELATION] {pair['label']} | "
            f"{leader} {leader_dir_str} {leader_move*100:+.3f}% → "
            f"{follower} lagging ({follower_move*100:+.3f}%, expected "
            f"{'↑' if follower_direction == 'BUY' else '↓'}) | "
            f"gap={lag*100:.3f}%"
        )

        self._emitted[ckey] = time.time()

        sig = ScanSignal(
            epic=follower,
            direction=follower_direction,
            reason=reason,
            ema21=ema21,
            atr=atr,
            current_price=follower_now,
            crossed_ema=False,
            source="correlation",
        )
        self._signals.put_nowait(sig)
        log.info(f"[CORR] ▶ {reason}")
