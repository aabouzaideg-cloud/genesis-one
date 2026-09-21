"""
CoreEngine — GENESIS-ONE main orchestration loop.

Single pipeline (Pipeline B):
  MarketScanner scans OIL_CRUDE every 15 min → ConfluenceGate.evaluate_scan()
  → RiskManager → Capital.com execution

Fundamental intelligence:
  OilSentinel — EIA weekly inventory + TankerMap Hormuz scrape + macro calendar
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from config import TRADING_MODE

THESIS_LOG_PATH = Path(__file__).parent / "genesis_trade_log.jsonl"

from capital_client import CapitalClient
from oil_sentinel import OilSentinel
from telegram_commander import TelegramCommander
from confluence_gate import ConfluenceGate, GateResult
from market_scanner import MarketScanner, ScanSignal
from risk_manager import RiskManager, TradeParams
from polygon_client import PolygonClient
import notify_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("GENESIS-ONE")


class CoreEngine:
    def __init__(self):
        self._capital      = CapitalClient()
        self._polygon      = PolygonClient()
        self._oil          = OilSentinel()
        self._gate         = ConfluenceGate(self._oil, self._capital, self._polygon)
        self._risk         = RiskManager(self._capital)
        self._trade_log: list[dict] = []
        self._start_time   = time.time()
        self._scan_signals: asyncio.Queue[ScanSignal] = asyncio.Queue()
        self._scanner      = MarketScanner(self._capital, self._scan_signals)
        self._commander    = TelegramCommander(
            capital_client=self._capital,
            risk_manager=self._risk,
            get_trade_log=lambda: self._trade_log,
            start_time=self._start_time,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        log.info(f"GENESIS-ONE starting in {TRADING_MODE} mode...")
        await self._capital.connect()
        await self._polygon.connect()
        await self._oil.connect()

        await self._risk.restore_from_store()
        log.info("All clients connected. Entering main loop...")

        await asyncio.gather(
            self._scan_processor(),
            self._scanner.run(),
            self._risk.monitor_positions(),
            self._commander.run(),
        )

    async def stop(self):
        self._scanner.stop()
        self._commander.stop()
        await self._capital.close()
        await self._polygon.close()
        await self._oil.close()
        log.info("GENESIS-ONE shut down.")

    # ── Pipeline B: Scanner Signal Processor ─────────────────────────────────

    async def _scan_processor(self):
        log.info("Scan processor running — Pipeline B active.")
        while True:
            signal: ScanSignal = await self._scan_signals.get()
            asyncio.create_task(self._process_scan_signal(signal))

    async def _process_scan_signal(self, signal: ScanSignal):
        log.info(
            f"[SCAN] Received: {signal.epic} {signal.direction} | {signal.reason[:80]}"
        )

        # Dedup: skip if position already open on this epic; cap at 1 concurrent (single market)
        try:
            open_pos   = await self._capital.get_open_positions()
            open_epics = {p["market"]["epic"] for p in open_pos}
            if signal.epic in open_epics:
                log.info(f"[SCAN] {signal.epic} — skipped, position already open")
                return
            if len(open_pos) >= 1:
                log.info(f"[SCAN] {signal.epic} — skipped, max 1 concurrent position reached")
                return
        except Exception:
            pass

        try:
            gate_result: GateResult = await self._gate.evaluate_scan(signal)

            if gate_result.verdict == "APPROVED":
                await self._execute_scan_trade(signal, gate_result)
            else:
                self._log_scan_thesis(signal, gate_result, params=None, deal_id=None)
                log.info(
                    f"[SCAN-GATE] BLOCKED ({gate_result.verdict}): {gate_result.reason}"
                )
                msg = notify_bot.format_blocked(
                    epic=gate_result.epic or "N/A",
                    verdict=gate_result.verdict,
                    reason=gate_result.reason,
                    whale=f"SCANNER({signal.epic})",
                )
                asyncio.create_task(notify_bot.send_message(msg))

        except Exception as exc:
            log.error(f"Error processing scan signal: {exc}", exc_info=True)

    async def _execute_scan_trade(self, signal: ScanSignal, gate: GateResult):
        epic      = gate.epic
        direction = gate.direction
        df        = await self._capital.get_ohlcv(epic)

        params: TradeParams | None = await self._risk.calculate(
            epic, direction, df, gate.confidence
        )
        if params is None:
            log.warning(f"[SCAN-RISK] Trade blocked by risk checks for {epic}")
            return

        log.info(
            f"[SCAN-EXEC] Placing {direction} {params.size} lots {epic} "
            f"| Entry={params.entry} SL={params.stop_loss} TP={params.take_profit} "
            f"| conf={gate.confidence} RR=5:1"
        )

        result  = await self._capital.place_order(
            epic=epic, direction=direction, size=params.size,
            stop_level=params.stop_loss, profit_level=params.take_profit,
        )

        # Resolve the real Capital.com dealId (position ID) — the order response
        # returns a dealReference (o_xxx) which differs from the position dealId
        # used by update_stop / close_position / reconciliation.
        deal_id = result.get("dealReference") or result.get("dealId", "unknown")
        try:
            await asyncio.sleep(1)   # brief wait for position to appear
            live = await self._capital.get_open_positions()
            for p in live:
                if p["market"]["epic"] == epic and p["position"]["direction"] == direction:
                    real_id = p["position"].get("dealId")
                    if real_id:
                        deal_id = real_id
                        break
        except Exception as exc:
            log.warning(f"[SCAN-EXEC] Could not resolve real dealId: {exc}")

        self._risk.register(deal_id, epic, params)
        self._scanner.set_trade_cooldown(epic, direction)
        self._log_scan_thesis(signal, gate, params, deal_id)

        msg = notify_bot.format_approved(
            verdict=gate.verdict, epic=epic, direction=direction,
            entry=params.entry, sl=params.stop_loss, tp=params.take_profit,
            size=params.size, deal_id=deal_id, confidence=gate.confidence,
            reason=gate.reason,
        )
        asyncio.create_task(notify_bot.send_message(msg))

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_scan_thesis(
        self,
        signal: ScanSignal,
        gate: GateResult,
        params: TradeParams | None,
        deal_id: str | None,
    ):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        if gate.verdict == "APPROVED" and params:
            thesis = (
                f"[GENESIS-SCAN] {ts} — {gate.direction} {gate.epic} @ {params.entry:.5f} | "
                f"{gate.reason} | SL={params.stop_loss:.5f} TP={params.take_profit:.5f} "
                f"Size={params.size} | Confidence={gate.confidence} | DealID={deal_id}"
            )
        else:
            thesis = (
                f"[GENESIS-SCAN] {ts} — BLOCKED ({gate.verdict}) | "
                f"Epic={gate.epic or 'N/A'} Direction={signal.direction} | "
                f"Reason: {gate.reason}"
            )

        log.info(thesis)

        record = {
            "timestamp":   ts,
            "source":      "scanner",
            "verdict":     gate.verdict,
            "epic":        gate.epic,
            "direction":   gate.direction,
            "reason":      gate.reason,
            "confidence":  gate.confidence,
            "deal_id":     deal_id,
            "entry":       params.entry if params else None,
            "sl":          params.stop_loss if params else None,
            "tp":          params.take_profit if params else None,
            "size":        params.size if params else None,
            "ema21":       signal.ema21,
            "atr":         signal.atr,
            "crossed_ema": signal.crossed_ema,
        }
        self._trade_log.append(record)

        try:
            with THESIS_LOG_PATH.open("a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            log.warning(f"Could not write scan thesis log: {exc}")

    def get_log(self) -> list[dict]:
        return self._trade_log
