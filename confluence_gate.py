"""
ConfluenceGate — Oil-focused gatekeeper for Pipeline B (chart scanner).

Gates (in order):
  1. Macro Calendar     — block within 2h of high-impact USD events
  2. Macro Trend        — 4H EMA 200 slope (block counter-trend)
  3. Oil Fundamental    — EIA inventory + TankerMap Hormuz
                          aligns with direction     → confidence boost
                          BUY + BEARISH fundamental → BLOCK
                          SELL + BULLISH fundamental → no boost, proceed (price action leads)
                          neutral                   → MEDIUM, proceed
  4. Value Zone         — EMA21 / Fib 0.618 / S/R (100-bar lookback)
                          S/R type returned: SUPPORT favors BUY, RESISTANCE favors SELL
                          misaligned S/R → suppress (reduce confidence)
  5. Candle Geometry    — upper wick rejection, minimum body check

Confidence tiers:
  HIGH   — fundamental aligns AND S/R aligns with direction
  MEDIUM — fundamental neutral OR S/R neutral/misaligned (but not blocked)
"""

import logging
from dataclasses import dataclass
from typing import Literal, Optional
import pandas as pd
from config import EMA_PERIOD, FOMO_PRICE_THRESHOLD, VALUE_ZONE_TOLERANCE

PIN_BAR_WICK_RATIO = 2.0
PIN_BAR_CLOSE_ZONE = 0.35
LIQUIDITY_GRAB_MIN = 0.60

log = logging.getLogger("ConfluenceGate")

Verdict = Literal["APPROVED", "BLOCKED_FUNDAMENTAL", "BLOCKED_CHART", "BLOCKED_CALENDAR"]


@dataclass
class GateResult:
    verdict:    Verdict
    direction:  Optional[str]
    epic:       Optional[str]
    confidence: Literal["HIGH", "MEDIUM", "LOW"] = "LOW"
    reason:     str = ""


class ConfluenceGate:
    def __init__(self, oil_sentinel, capital_client, polygon_client=None):
        self._oil     = oil_sentinel
        self._capital = capital_client
        self._polygon = polygon_client

    # ── Pipeline B: Scanner Signal Evaluation ────────────────────────────────

    async def evaluate_scan(self, signal) -> GateResult:
        """
        Evaluate a ScanSignal (chart-driven, OIL_CRUDE only).

        Gates: calendar → macro trend → oil fundamental → value zone + S/R → wick
        """
        epic      = signal.epic
        direction = signal.direction

        # ── Gate 1: Macro Calendar ────────────────────────────────────────────
        cal_ok, cal_reason = await self._oil.check_macro_calendar(epic)
        if not cal_ok:
            return GateResult(
                verdict="BLOCKED_CALENDAR", direction=None, epic=epic,
                reason=f"Macro calendar: {cal_reason}",
            )

        # ── Gate 2: Macro Trend — 4H EMA 200 ─────────────────────────────────
        trend_ok, trend_desc = await self._check_macro_trend(epic, direction)
        if not trend_ok:
            return GateResult(
                verdict="BLOCKED_CHART", direction=None, epic=epic,
                reason=f"Macro trend rejection: {trend_desc}",
            )

        # ── Gate 3: Oil Fundamental (EIA + TankerMap) ─────────────────────────
        fund_verdict, fund_reason = await self._oil.get_verdict()

        fund_aligns = (
            (direction == "BUY"  and fund_verdict == "BULLISH") or
            (direction == "SELL" and fund_verdict == "BEARISH")
        )
        # Only hard-block BUY when fundamentals are bearish.
        # Bullish fundamentals do NOT block SELL — price action leads direction.
        fund_contradicts = (direction == "BUY" and fund_verdict == "BEARISH")

        if fund_contradicts:
            return GateResult(
                verdict="BLOCKED_FUNDAMENTAL", direction=None, epic=epic,
                reason=f"Oil fundamental contradicts {direction}: {fund_reason}",
            )

        # Fundamental neutral or aligns — track for confidence
        fund_boost = fund_aligns   # True = earns HIGH contribution

        # ── Gate 4: Value Zone + S/R direction bias ───────────────────────────
        price_data  = await self._capital.get_price(epic)
        current_mid = price_data["mid"]

        df = None
        if self._polygon:
            df = await self._polygon.get_ohlcv(epic)
        if df is None or df.empty:
            df = await self._capital.get_ohlcv(epic)

        hint_ema = getattr(signal, "ema21", None)
        in_zone, zone_desc, sr_type = self._in_value_zone(df, current_mid, hint_ema=hint_ema)

        if not in_zone:
            return GateResult(
                verdict="BLOCKED_CHART", direction=None, epic=epic,
                reason=f"Price not in Value Zone ({current_mid:.5f}) | {zone_desc}",
            )

        # S/R direction alignment
        sr_aligns = (
            (sr_type == "SUPPORT"    and direction == "BUY") or
            (sr_type == "RESISTANCE" and direction == "SELL")
        )
        sr_opposes = (
            (sr_type == "SUPPORT"    and direction == "SELL") or
            (sr_type == "RESISTANCE" and direction == "BUY")
        )

        if sr_opposes:
            # At support trying to sell, or at resistance trying to buy — suppress
            return GateResult(
                verdict="BLOCKED_CHART", direction=None, epic=epic,
                reason=(
                    f"S/R opposition: price at {sr_type} zone but direction={direction} "
                    f"| {zone_desc}"
                ),
            )

        sr_boost = sr_aligns   # True = earns HIGH contribution

        # ── Gate 5: Candle Geometry — upper wick rejection ────────────────────
        wick_blocked, wick_desc = self._check_candle_geometry(df, direction)
        if wick_blocked:
            return GateResult(
                verdict="BLOCKED_CHART", direction=None, epic=epic,
                reason=f"Candle geometry rejection: {wick_desc}",
            )

        # ── Breakout Bias — daily + historical H/L ────────────────────────────
        breakout_boost, breakout_desc = self._check_breakout_bias(df, current_mid, direction)

        # ── Confidence: HIGH if fundamental + S/R + breakout all align ───────
        boost_count = sum([fund_boost, sr_boost, breakout_boost])
        if boost_count >= 2:
            confidence = "HIGH"
        else:
            confidence = "MEDIUM"

        pattern_desc = self._detect_pattern(df, direction)

        sr_note      = f"{sr_type} zone" if sr_type else "EMA/Fib zone"
        fund_note    = f"FUND={fund_verdict}" if fund_verdict != "NEUTRAL" else "FUND=neutral"
        breakout_note = f"BREAKOUT={breakout_desc}" if breakout_boost else ""

        thesis = (
            f"{signal.reason} | "
            f"CAL: {cal_reason} | "
            f"{fund_note}: {fund_reason} | "
            f"CHART: {zone_desc} ({sr_note})"
            + (f" + {pattern_desc}" if pattern_desc else "")
            + (f" | {breakout_note}" if breakout_note else "")
            + f" | TREND: {trend_desc} | "
            f"Epic={epic} conf={confidence} @ ~{current_mid:.5f}"
        )

        return GateResult(
            verdict="APPROVED",
            direction=direction,
            epic=epic,
            confidence=confidence,
            reason=thesis,
        )

    # ── Value Zone Detection ──────────────────────────────────────────────────

    def _in_value_zone(
        self,
        df: pd.DataFrame,
        price: float,
        hint_ema: float = None,
    ) -> tuple[bool, str, Optional[str]]:
        """
        Check if price is near:
          - 21-EMA
          - Fibonacci 0.618 retracement level
          - S/R level (swing highs/lows over last 100 bars)

        Returns (in_zone, description, sr_type).
        sr_type: "SUPPORT" | "RESISTANCE" | None
          - SUPPORT    = price is near a swing LOW (potential bounce up)
          - RESISTANCE = price is near a swing HIGH (potential rejection down)
          - None       = price is at EMA or Fib (no directional S/R bias)

        hint_ema: pre-computed EMA21 from scanner when df has too few bars.
        """
        ema_col = f"EMA_{EMA_PERIOD}"
        ema = df[ema_col].iloc[-1] if ema_col in df.columns else hint_ema

        # 21-EMA check
        if ema and self._near(price, ema):
            return True, f"21-EMA @ {ema:.5f}", None

        # Fibonacci 0.618 check (last 50 bars)
        high    = df["high"].tail(50).max()
        low     = df["low"].tail(50).min()
        fib618  = high - 0.618 * (high - low)
        if self._near(price, fib618):
            # Determine direction bias from Fib position
            # Price at 0.618 retracement from top → near support → SUPPORT
            sr_type = "SUPPORT" if price <= (high + low) / 2 else "RESISTANCE"
            return True, f"Fib 0.618 @ {fib618:.5f}", sr_type

        # S/R: swing highs and lows over last 100 bars
        lookback = df.tail(100)
        swing_highs = self._swing_levels(lookback["high"])
        swing_lows  = self._swing_levels(lookback["low"])

        for level in swing_lows:
            if self._near(price, level):
                return True, f"Support @ {level:.5f}", "SUPPORT"

        for level in swing_highs:
            if self._near(price, level):
                return True, f"Resistance @ {level:.5f}", "RESISTANCE"

        # Not in any value zone — report nearest reference
        ema_val = ema if ema else price
        nearest = min([ema_val, fib618], key=lambda x: abs(x - price))
        ema_str = f"{ema:.5f}" if ema else "N/A"
        return False, f"nearest ref={nearest:.5f} (EMA={ema_str}, Fib={fib618:.5f})", None

    def _near(self, price: float, level: float) -> bool:
        return abs(price - level) / level <= VALUE_ZONE_TOLERANCE

    def _swing_levels(self, series: pd.Series) -> list[float]:
        """Detect swing highs or lows — local extrema with strength weighting."""
        levels = []
        vals = series.values
        for i in range(1, len(vals) - 1):
            if vals[i] > vals[i - 1] and vals[i] > vals[i + 1]:
                levels.append(float(vals[i]))
            elif vals[i] < vals[i - 1] and vals[i] < vals[i + 1]:
                levels.append(float(vals[i]))
        return levels

    # ── Breakout Bias — Daily + Historical H/L ───────────────────────────────

    def _check_breakout_bias(
        self,
        df: pd.DataFrame,
        price: float,
        direction: str,
        proximity_pct: float = 0.003,   # within 0.3% of the level = "near"
    ) -> tuple[bool, str]:
        """
        Favor trades positioned to break daily or historical highs/lows.

        Logic:
          - Near today's high   + BUY  → breakout above daily high
          - Near today's low    + SELL → breakout below daily low
          - Near historical high + BUY  → continuation above all-available high
          - Near historical low  + SELL → continuation below all-available low

        Returns (boost, description).
        """
        if df is None or df.empty:
            return False, ""

        # Daily high/low — use bars tagged to today (by index date if available)
        # Fallback: first 26 bars of a 15m chart ≈ today's session
        try:
            if hasattr(df.index, 'date'):
                today = df.index[-1].date()
                today_df = df[df.index.date == today]
            else:
                today_df = df.tail(26)   # ~6.5h of 15m bars = full NYSE session
        except Exception:
            today_df = df.tail(26)

        if not today_df.empty:
            daily_high = float(today_df["high"].max())
            daily_low  = float(today_df["low"].min())
        else:
            daily_high = float(df["high"].tail(26).max())
            daily_low  = float(df["low"].tail(26).min())

        # Historical high/low — all available bars
        hist_high = float(df["high"].max())
        hist_low  = float(df["low"].min())

        def near(p: float, level: float) -> bool:
            return abs(p - level) / level <= proximity_pct

        # BUY breakout checks
        if direction == "BUY":
            if near(price, daily_high) or price > daily_high:
                return True, f"near/above daily high {daily_high:.3f} — BUY breakout"
            if near(price, hist_high) or price > hist_high:
                return True, f"near/above historical high {hist_high:.3f} — continuation BUY"

        # SELL breakout checks
        if direction == "SELL":
            if near(price, daily_low) or price < daily_low:
                return True, f"near/below daily low {daily_low:.3f} — SELL breakdown"
            if near(price, hist_low) or price < hist_low:
                return True, f"near/below historical low {hist_low:.3f} — continuation SELL"

        return False, ""

    # ── Candle Geometry ───────────────────────────────────────────────────────

    def _check_candle_geometry(self, df: pd.DataFrame, direction: str) -> tuple[bool, str]:
        """
        For BUY: reject if upper wick > 50% of body (resistance rejection).
        Minimum body = 30% of ATR (ignore doji/spinning top candles).
        """
        if direction != "BUY" or len(df) < 1:
            return False, ""

        c = df.iloc[-1]
        o, h, close = float(c["open"]), float(c["high"]), float(c["close"])
        body        = abs(close - o)
        upper_wick  = h - max(o, close)

        atr_col = next((col for col in df.columns if "ATR" in col), None)
        atr     = float(df[atr_col].iloc[-1]) if atr_col else 0

        if atr > 0 and body < 0.3 * atr:
            return False, ""

        if body > 0 and upper_wick > 0.5 * body:
            return True, (
                f"Upper wick ({upper_wick:.5f}) > 50% body ({body:.5f}) "
                f"— resistance rejection, aborting BUY"
            )
        return False, ""

    # ── Macro Trend — 4H EMA 200 ──────────────────────────────────────────────

    async def _check_macro_trend(self, epic: str, direction: str) -> tuple[bool, str]:
        """
        Block BUY when 4H EMA 200 is declining (bearish macro).
        Block SELL when 4H EMA 200 is rising (bullish macro).
        Blocks if data is unavailable — no bypass.
        """
        try:
            df_h4 = None
            if self._polygon:
                df_h4 = await self._polygon.get_ohlcv_h4(epic)
            if df_h4 is None or df_h4.empty or len(df_h4) < 200:
                df_h4 = await self._capital.get_ohlcv_h4(epic)

            ema_col = "EMA_200"
            if ema_col not in df_h4.columns:
                df_h4.ta.ema(length=200, append=True)
            if ema_col not in df_h4.columns or df_h4[ema_col].dropna().shape[0] < 5:
                return False, "4H EMA 200 unavailable — blocking trade"

            ema   = df_h4[ema_col].dropna()
            slope = ema.iloc[-1] - ema.iloc[-4]
            ema_val = round(float(ema.iloc[-1]), 5)

            if direction == "BUY" and slope < 0:
                return False, f"4H EMA 200 declining ({ema_val}) — macro bearish, no BUY"
            if direction == "SELL" and slope > 0:
                return False, f"4H EMA 200 rising ({ema_val}) — macro bullish, no SELL"

            label = "rising" if slope > 0 else "flat/declining"
            return True, f"4H EMA 200 {label} @ {ema_val} — aligned with {direction}"

        except Exception as exc:
            log.debug(f"4H trend check failed for {epic}: {exc}")
            return False, "4H trend check failed — blocking trade"

    # ── Candlestick Pattern Detection ─────────────────────────────────────────

    def _detect_pattern(self, df: pd.DataFrame, direction: str) -> str:
        if len(df) < 2:
            return ""

        cur  = df.iloc[-1]
        prev = df.iloc[-2]

        o, h, l, c = float(cur["open"]), float(cur["high"]), float(cur["low"]), float(cur["close"])
        candle_range = h - l
        if candle_range == 0:
            return ""

        body       = abs(c - o)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # Pin Bar
        if body > 0:
            if (direction == "BUY"
                    and lower_wick >= PIN_BAR_WICK_RATIO * body
                    and (c - l) / candle_range >= (1 - PIN_BAR_CLOSE_ZONE)):
                return f"Bullish Pin Bar (lower wick={lower_wick:.5f})"

            if (direction == "SELL"
                    and upper_wick >= PIN_BAR_WICK_RATIO * body
                    and (h - c) / candle_range >= (1 - PIN_BAR_CLOSE_ZONE)):
                return f"Bearish Pin Bar (upper wick={upper_wick:.5f})"

        # Engulfing
        prev_body_top = max(float(prev["open"]), float(prev["close"]))
        prev_body_bot = min(float(prev["open"]), float(prev["close"]))

        if (direction == "BUY"
                and c > o
                and float(prev["close"]) < float(prev["open"])
                and o <= prev_body_bot
                and c >= prev_body_top):
            return f"Bullish Engulfing (body={body:.5f})"

        if (direction == "SELL"
                and c < o
                and float(prev["close"]) > float(prev["open"])
                and o >= prev_body_top
                and c <= prev_body_bot):
            return f"Bearish Engulfing (body={body:.5f})"

        # Liquidity Grab
        swing_highs = self._swing_levels(df.tail(20)["high"])
        swing_lows  = self._swing_levels(df.tail(20)["low"])

        if direction == "BUY" and lower_wick / candle_range >= LIQUIDITY_GRAB_MIN:
            for level in swing_lows:
                if l <= level <= (l + lower_wick):
                    return f"Liquidity Grab below S/R @ {level:.5f}"

        if direction == "SELL" and upper_wick / candle_range >= LIQUIDITY_GRAB_MIN:
            for level in swing_highs:
                if (h - upper_wick) <= level <= h:
                    return f"Liquidity Grab above S/R @ {level:.5f}"

        return ""
