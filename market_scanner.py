"""
MarketScanner — Pipeline B: proactive chart-driven signal generator.

Scans tradeable epics every 15 minutes. For each epic:
  1. Fetches 15m OHLCV
  2. Detects EMA21 setup (crossover or trend consensus)
  3. Emits ScanSignal if setup is clear and cooldown has elapsed

Direction logic:
  - 2+ of last 3 closes above EMA21 → BUY candidate
  - 2+ of last 3 closes below EMA21 → SELL candidate
  - Crossover on the last bar (strongest setup) → flagged separately

Confidence is resolved in ConfluenceGate.evaluate_scan():
  - Whale recently active on this epic → HIGH
  - No whale activity → MEDIUM
  - Whale opposing direction → BLOCKED
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("MarketScanner")

SCAN_EPICS      = ["OIL_CRUDE"]
SCAN_INTERVAL   = 900         # 15 minutes — aligns with OHLCV_INTERVAL
SIGNAL_COOLDOWN    = 4 * 3600   # 4 hours cooldown after an APPROVED trade
REFIRE_COOLDOWN    = 45 * 60    # 45 minutes between signal emissions

# NYSE session — Eastern Time, weekdays only
_ET = ZoneInfo("America/New_York")
NYSE_OPEN  = (9, 30)    # 9:30 AM ET
NYSE_CLOSE = (16, 0)    # 4:00 PM ET


def _is_nyse_session() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:   # Saturday/Sunday
        return False
    t = (now.hour, now.minute)
    return NYSE_OPEN <= t < NYSE_CLOSE


@dataclass
class ScanSignal:
    epic:          str
    direction:     str      # "BUY" or "SELL"
    reason:        str      # human-readable chart description
    ema21:         float
    atr:           float
    current_price: float
    crossed_ema:   bool  = False   # True if this bar just crossed EMA21
    timestamp:     float = field(default_factory=time.time)
    source:        str   = "scanner"


class MarketScanner:
    def __init__(self, capital_client, signals_queue: asyncio.Queue):
        self._capital = capital_client
        self._signals = signals_queue
        self._running = False
        self._emitted: dict[str, float] = {}   # ckey → last emission time (45min refire guard)
        self._traded:  dict[str, float] = {}   # ckey → last approved trade time (4h cooldown)

    async def run(self):
        self._running = True
        log.info("[SCANNER] Pipeline B started — scanning every 15 minutes.")
        # Stagger startup so CapitalClient session is fully ready
        await asyncio.sleep(90)
        while self._running:
            await self._scan_all()
            await asyncio.sleep(SCAN_INTERVAL)

    def stop(self):
        self._running = False

    def set_trade_cooldown(self, epic: str, direction: str):
        """Called by core_engine after a scan trade is approved and placed."""
        ckey = f"{epic}:{direction}"
        self._traded[ckey] = time.time()
        log.debug(f"[SCANNER] Trade cooldown set for {epic} {direction} (4h)")

    async def _scan_all(self):
        if not _is_nyse_session():
            log.info("[SCANNER] Outside NYSE session (9:30 AM – 4:00 PM ET) — skipping scan")
            return
        log.info(f"[SCANNER] Scanning {len(SCAN_EPICS)} epics...")
        for epic in SCAN_EPICS:
            try:
                await self._scan_epic(epic)
            except Exception as exc:
                log.warning(f"[SCANNER] {epic} error: {exc}")

    async def _scan_epic(self, epic: str):
        df = await self._capital.get_ohlcv(epic)
        if df is None or len(df) < 5:
            return

        ema_col = next((c for c in df.columns if "EMA" in c), None)
        atr_col = next((c for c in df.columns if "ATR" in c), None)
        if not ema_col or not atr_col:
            return

        closes = df["close"].iloc[-3:].tolist()
        emas   = df[ema_col].iloc[-3:].tolist()
        atr    = float(df[atr_col].iloc[-1])
        price  = closes[-1]
        ema21  = emas[-1]

        above = sum(1 for c, e in zip(closes, emas) if c > e)
        below = 3 - above

        # EMA crossover: last bar just crossed, previous bar was on the other side
        crossed = (
            (closes[-1] > emas[-1] and closes[-2] <= emas[-2]) or
            (closes[-1] < emas[-1] and closes[-2] >= emas[-2])
        )

        if above >= 2:
            direction = "BUY"
            label = "EMA crossover" if crossed else "Trending above EMA21"
            desc  = f"{label} ({above}/3 bars) | price={price:.2f} ema21={ema21:.2f}"
        elif below >= 2:
            direction = "SELL"
            label = "EMA crossover" if crossed else "Trending below EMA21"
            desc  = f"{label} ({below}/3 bars) | price={price:.2f} ema21={ema21:.2f}"
        else:
            log.debug(f"[SCANNER] {epic} — no clear EMA consensus, skipping")
            return

        ckey = f"{epic}:{direction}"
        now  = time.time()
        if now - self._traded.get(ckey, 0) < SIGNAL_COOLDOWN:
            log.debug(f"[SCANNER] {epic} {direction} — post-trade cooldown active, skipping")
            return
        if now - self._emitted.get(ckey, 0) < REFIRE_COOLDOWN:
            log.debug(f"[SCANNER] {epic} {direction} — refire cooldown active, skipping")
            return

        self._emitted[ckey] = now
        sig = ScanSignal(
            epic=epic,
            direction=direction,
            reason=f"[SCANNER] {desc} | ATR={atr:.4f}",
            ema21=ema21,
            atr=atr,
            current_price=price,
            crossed_ema=crossed,
        )
        self._signals.put_nowait(sig)
        log.info(f"[SCANNER] ▶ {epic} {direction} | {desc}")
