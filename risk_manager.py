"""
RiskManager — ATR-based TP/SL, trailing stop, margin guard, and position sizing.

Rules:
  - SL  : 1.5x ATR from entry
  - TP  : 50% at 1:1.5 RR, then 0.3% trailing stop on remainder
  - Size: risk no more than MAX_RISK_PER_TRADE % of account balance
  - Margin Guard: block any trade if margin < 350%
  - Slippage: block if spread > 0.05%

Positions are persisted to positions.json via PositionStore so that a restart
never leaves open trades unmanaged.
"""

import asyncio
import logging
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone
import pandas as pd
from config import (
    ATR_SL_MULTIPLIER, PARTIAL_CLOSE_RR, TRAILING_STOP_PCT,
    MIN_MARGIN_PERCENT, MAX_RISK_PER_TRADE, MAX_SLIPPAGE, ATR_PERIOD,
    BREAKEVEN_BUFFER, BREAKEVEN_PROFIT_PCT, ATR_REFRESH_INTERVAL,
    KELLY_MAX_FRACTION, KELLY_WIN_PROB, DAILY_LOSS_LIMIT,
)
from position_store import PositionStore, StoredPosition
import notify_bot

log = logging.getLogger("RiskManager")


@dataclass
class TradeParams:
    entry:       float
    stop_loss:   float
    take_profit: float
    size:        float
    direction:   str
    atr:         float
    risk_amount: float


# In-memory view of an active position (mirrors StoredPosition but used at runtime)
@dataclass
class ActivePosition:
    deal_id:        str
    epic:           str
    direction:      str
    entry:          float
    stop_loss:      float
    take_profit:    float
    size:           float
    atr:            float
    partial_done:   bool  = False
    trailing_stop:  float = 0.0
    breakeven_done: bool  = False
    last_atr_refresh: float = 0.0
    registered_at:    float = 0.0


class RiskManager:
    def __init__(self, capital_client):
        self._capital   = capital_client
        self._store     = PositionStore()
        self._positions: dict[str, ActivePosition] = {}
        # ── Circuit breaker state ──────────────────────────────────────────
        self._circuit_open:       bool  = False   # True = trading halted for the day
        self._day_start_balance:  float = 0.0
        self._day_label:          str   = ""      # "YYYY-MM-DD" of current trading day
        self._realized_loss:      float = 0.0     # cumulative realized loss today (AED)

    # ── Startup Reconciliation ────────────────────────────────────────────────

    async def restore_from_store(self):
        """
        Called once at startup. Fetches live positions from Capital.com,
        reconciles against stored state, and resumes managing survivors.
        """
        live_positions = await self._capital.get_open_positions()
        live_ids = {p.get("position", {}).get("dealId", "") for p in live_positions}
        live_ids.discard("")

        resumed, dropped = self._store.reconcile(live_ids)

        for sp in resumed:
            self._positions[sp.deal_id] = ActivePosition(
                deal_id=sp.deal_id,
                epic=sp.epic,
                direction=sp.direction,
                entry=sp.entry,
                stop_loss=sp.stop_loss,
                take_profit=sp.take_profit,
                size=sp.size,
                atr=sp.atr,
                partial_done=sp.partial_done,
                trailing_stop=sp.trailing_stop,
                registered_at=_time.time(),
            )

        log.info(
            f"[RISK] Restored {len(resumed)} position(s), "
            f"dropped {len(dropped)} stale record(s)."
        )

    # ── Pre-Trade Checks ──────────────────────────────────────────────────────

    async def margin_ok(self) -> bool:
        margin = await self._capital.get_margin_percent()
        if margin < MIN_MARGIN_PERCENT:
            log.warning(f"Margin too low: {margin:.1f}% < {MIN_MARGIN_PERCENT}% — blocking trade")
            return False
        return True

    async def slippage_ok(self, epic: str) -> tuple[bool, float]:
        price  = await self._capital.get_price(epic)
        spread = (price["ask"] - price["bid"]) / price["mid"]
        ok     = spread <= MAX_SLIPPAGE
        if not ok:
            log.warning(f"Slippage too high: {spread*100:.4f}% > {MAX_SLIPPAGE*100:.3f}%")
        return ok, spread

    # ── Position Sizing & TP/SL Calculation ──────────────────────────────────

    async def calculate(
        self,
        epic: str,
        direction: str,
        df: pd.DataFrame,
        confidence: str = "LOW",   # "HIGH" | "MEDIUM" | "LOW" from GateResult
    ) -> TradeParams | None:
        # ── Day reset — must run before circuit breaker check ──────────────
        account = await self._capital.get_account_info()
        balance = account["accounts"][0]["balance"]["balance"]
        today   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._day_label != today:
            self._day_label         = today
            self._day_start_balance = balance
            self._circuit_open      = False
            log.info(f"[CIRCUIT] New trading day — baseline balance ${balance:.2f}")

        # ── Circuit breaker check ──────────────────────────────────────────
        if self._circuit_open:
            log.warning("[CIRCUIT] Trading halted for the day — circuit breaker is open.")
            return None

        if not await self.margin_ok():
            return None
        slip_ok, _ = await self.slippage_ok(epic)
        if not slip_ok:
            return None

        price_data = await self._capital.get_price(epic)
        entry = price_data["ask"] if direction == "BUY" else price_data["bid"]

        atr_col = f"ATRr_{ATR_PERIOD}"
        if atr_col not in df.columns:
            candidates = [c for c in df.columns if "ATR" in c.upper()]
            atr_col    = candidates[0] if candidates else None

        atr         = float(df[atr_col].iloc[-1]) if atr_col else entry * 0.001
        sl_distance = ATR_SL_MULTIPLIER * atr
        tp_distance = sl_distance * PARTIAL_CLOSE_RR

        if direction == "BUY":
            stop_loss   = entry - sl_distance
            take_profit = entry + tp_distance
        else:
            stop_loss   = entry + sl_distance
            take_profit = entry - tp_distance

        # ── Kelly Criterion sizing ─────────────────────────────────────────
        # f* = (p*(b+1) - 1) / b  where b = RR ratio, p = win probability prior
        rr_ratio  = tp_distance / sl_distance if sl_distance > 0 else PARTIAL_CLOSE_RR
        p         = KELLY_WIN_PROB.get(confidence, 0.50)
        kelly_f   = (p * (rr_ratio + 1) - 1) / rr_ratio if rr_ratio > 0 else 0
        kelly_f   = max(kelly_f, 0.0)                    # floor at 0 (never short Kelly)
        kelly_f   = min(kelly_f, KELLY_MAX_FRACTION)     # hard cap (2%)
        risk_amount = balance * kelly_f

        log.info(
            f"[KELLY] conf={confidence} p={p:.2f} RR={rr_ratio:.2f} "
            f"→ f*={kelly_f*100:.2f}% risk_amount=${risk_amount:.2f}"
        )

        size = max(round(risk_amount / sl_distance, 2), 0.01)

        return TradeParams(
            entry=entry,
            stop_loss=round(stop_loss, 5),
            take_profit=round(take_profit, 5),
            size=size,
            direction=direction,
            atr=atr,
            risk_amount=risk_amount,
        )

    # ── Position Registration ─────────────────────────────────────────────────

    def register(self, deal_id: str, epic: str, params: TradeParams):
        pos = ActivePosition(
            deal_id=deal_id,
            epic=epic,
            direction=params.direction,
            entry=params.entry,
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            size=params.size,
            atr=params.atr,
            trailing_stop=params.stop_loss,
            registered_at=_time.time(),
        )
        self._positions[deal_id] = pos

        # Persist to disk immediately
        self._store.save(StoredPosition(
            deal_id=deal_id,
            epic=epic,
            direction=params.direction,
            entry=params.entry,
            stop_loss=params.stop_loss,
            take_profit=params.take_profit,
            size=params.size,
            atr=params.atr,
            partial_done=False,
            trailing_stop=params.stop_loss,
            opened_at=datetime.now(timezone.utc).isoformat(),
        ))
        log.info(f"[RISK] Registered & persisted {deal_id}: {params.direction} {epic} @ {params.entry}")

    def remove(self, deal_id: str):
        self._positions.pop(deal_id, None)
        self._store.remove(deal_id)

    # ── Position Monitoring ───────────────────────────────────────────────────

    async def monitor_positions(self):
        """Continuously monitor open positions for partial TP, trailing stop, and circuit breaker."""
        cycle = 0
        while True:
            await self._check_circuit_breaker()

            # Every 4 cycles (~60s) reconcile against live Capital.com positions
            # to auto-remove any positions closed by TP/SL outside our tracking.
            # Match on epic+direction since order references differ from position dealIds.
            if cycle % 4 == 0 and self._positions:
                try:
                    live = await self._capital.get_open_positions()
                    live_keys = {
                        (p["market"]["epic"], p["position"]["direction"])
                        for p in live
                    }
                    for deal_id, pos in list(self._positions.items()):
                        # Grace period: skip positions registered < 2 min ago
                        # (Capital.com API may not reflect new orders yet)
                        if _time.time() - pos.registered_at < 120:
                            continue
                        if (pos.epic, pos.direction) not in live_keys:
                            log.info(
                                f"[RISK] {pos.epic} {pos.direction} no longer live "
                                f"— deregistering {deal_id}"
                            )
                            self.remove(deal_id)
                except Exception as exc:
                    log.debug(f"[RISK] Reconciliation check failed: {exc}")

            for deal_id, pos in list(self._positions.items()):
                try:
                    await self._check_position(deal_id, pos)
                except Exception as exc:
                    # Do NOT deregister on 400 — a failed update_stop or price call
                    # does not mean the position is closed. Let the reconciliation
                    # check (every 4 cycles above) determine if it's truly gone.
                    log.warning(f"[RISK] _check_position error for {deal_id}: {exc}")

            cycle += 1
            await asyncio.sleep(15)

    async def _check_circuit_breaker(self):
        """Trip if today's loss exceeds DAILY_LOSS_LIMIT of start-of-day balance.
        Checks realized loss when flat, and total account value (balance + unrealized P&L)
        when positions are open — so SL bleed can't silently breach the daily limit."""
        if self._circuit_open or self._day_start_balance == 0:
            return
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._day_label != today:
                self._realized_loss = 0.0
                return

            account = await self._capital.get_account_info()
            bal     = account["accounts"][0]["balance"]
            balance = bal["balance"]

            if self._positions:
                # Include unrealized P&L so open positions bleeding to SL can trip the breaker
                unrealized = bal.get("profit", 0.0)
                total_value = balance + unrealized
            else:
                total_value = balance

            loss_pct = (self._day_start_balance - total_value) / self._day_start_balance
            if loss_pct >= DAILY_LOSS_LIMIT:
                await self._trip_circuit(loss_pct, total_value)
        except Exception as exc:
            log.debug(f"[CIRCUIT] Balance check failed: {exc}")

    async def _trip_circuit(self, loss_pct: float, current_balance: float):
        """Close all positions and halt trading for the rest of the day."""
        self._circuit_open = True
        log.critical(
            f"[CIRCUIT] BREAKER TRIPPED — daily loss {loss_pct*100:.1f}% "
            f"(${self._day_start_balance - current_balance:.2f} lost). "
            f"Closing all positions and halting for the day."
        )

        # Close every open position
        for deal_id, pos in list(self._positions.items()):
            try:
                close_dir = "SELL" if pos.direction == "BUY" else "BUY"
                await self._capital.close_position(deal_id, pos.size, close_dir)
                self.remove(deal_id)
                log.info(f"[CIRCUIT] Force-closed {deal_id} ({pos.epic})")
            except Exception as exc:
                log.error(f"[CIRCUIT] Failed to close {deal_id}: {exc}")

        # Emergency Telegram alert
        msg = (
            f"🚨 *CIRCUIT BREAKER TRIPPED*\n"
            f"Daily loss: *{loss_pct*100:.1f}%* (${self._day_start_balance - current_balance:.2f})\n"
            f"Threshold: {DAILY_LOSS_LIMIT*100:.0f}%\n"
            f"All positions closed. Trading halted until midnight UTC.\n"
            f"Balance: ${current_balance:.2f}"
        )
        asyncio.create_task(notify_bot.send_message(msg))

    async def _check_position(self, deal_id: str, pos: ActivePosition):
        import time as _time
        price_data = await self._capital.get_price(pos.epic)
        current    = price_data["mid"]

        # ── Hourly ATR refresh ─────────────────────────────────────────────
        if _time.time() - pos.last_atr_refresh > ATR_REFRESH_INTERVAL:
            try:
                df_fresh = await self._capital.get_ohlcv(pos.epic)
                atr_col  = f"ATRr_{ATR_PERIOD}"
                if atr_col not in df_fresh.columns:
                    candidates = [c for c in df_fresh.columns if "ATR" in c.upper()]
                    atr_col = candidates[0] if candidates else None
                if atr_col:
                    pos.atr = float(df_fresh[atr_col].iloc[-1])
                    pos.last_atr_refresh = _time.time()
                    log.info(f"[RISK] ATR refreshed for {deal_id}: {pos.atr:.5f}")
            except Exception as exc:
                log.debug(f"ATR refresh failed for {deal_id}: {exc}")

        # ── Break-even at 1% unrealized profit ────────────────────────────
        if not pos.breakeven_done:
            unrealized_pct = (
                (current - pos.entry) / pos.entry if pos.direction == "BUY"
                else (pos.entry - current) / pos.entry
            )
            if unrealized_pct >= BREAKEVEN_PROFIT_PCT:
                if pos.direction == "BUY":
                    be_stop = round(pos.entry * (1 + BREAKEVEN_BUFFER), 5)
                else:
                    be_stop = round(pos.entry * (1 - BREAKEVEN_BUFFER), 5)
                pos.breakeven_done = True
                pos.trailing_stop  = be_stop
                await self._capital.update_stop(deal_id, be_stop)
                self._store.update(deal_id, trailing_stop=be_stop)
                log.info(
                    f"[RISK] Break-even triggered at +{unrealized_pct*100:.2f}% "
                    f"→ SL moved to {be_stop} for {deal_id}"
                )

        # ── Partial close at 1:1.5 RR ─────────────────────────────────────
        if not pos.partial_done:
            tp_hit = (
                (pos.direction == "BUY"  and current >= pos.take_profit) or
                (pos.direction == "SELL" and current <= pos.take_profit)
            )
            if tp_hit:
                close_size = round(pos.size * 0.5, 2)
                close_dir  = "SELL" if pos.direction == "BUY" else "BUY"
                await self._capital.close_position(deal_id, close_size, close_dir)

                # Move stop to break-even + 0.1% buffer
                if pos.direction == "BUY":
                    breakeven_stop = round(pos.entry * (1 + BREAKEVEN_BUFFER), 5)
                else:
                    breakeven_stop = round(pos.entry * (1 - BREAKEVEN_BUFFER), 5)

                pos.partial_done  = True
                pos.trailing_stop = breakeven_stop
                await self._capital.update_stop(deal_id, breakeven_stop)
                self._store.update(deal_id, partial_done=True, trailing_stop=breakeven_stop)
                log.info(
                    f"[RISK] Partial close {close_size} lots of {deal_id} at {current} "
                    f"| Stop moved to breakeven+buf @ {breakeven_stop}"
                )

        # ── Trailing stop on remaining 50% ────────────────────────────────
        if pos.partial_done:
            if pos.direction == "BUY":
                new_trail = current * (1 - TRAILING_STOP_PCT)
                if new_trail > pos.trailing_stop:
                    pos.trailing_stop = new_trail
                    await self._capital.update_stop(deal_id, new_trail)
                    self._store.update(deal_id, trailing_stop=new_trail)
                    log.info(f"[RISK] Trailing stop → {new_trail:.5f} for {deal_id}")
            else:
                new_trail = current * (1 + TRAILING_STOP_PCT)
                if new_trail < pos.trailing_stop or pos.trailing_stop == pos.stop_loss:
                    pos.trailing_stop = new_trail
                    await self._capital.update_stop(deal_id, new_trail)
                    self._store.update(deal_id, trailing_stop=new_trail)
                    log.info(f"[RISK] Trailing stop → {new_trail:.5f} for {deal_id}")
