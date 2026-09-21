"""
OilSentinel — Oil-specific fundamental intelligence.

Replaces NewsSentinel. Two data sources:

  1. EIA Weekly Petroleum Status Report (free API, requires EIA_API_KEY in .env)
     - Crude inventory change vs prior week: draw → BULLISH, build → BEARISH
     - Cached 24h (EIA publishes weekly on Wednesdays)

  2. TankerMap Strait of Hormuz scrape (best-effort, no API key needed)
     - Significant drop in crude tanker transits → BULLISH (supply disruption)
     - Normal transits → NEUTRAL
     - Falls back to NEUTRAL on any parse or network error

  3. Macro Calendar (Forex Factory, ported from NewsSentinel)
     - Blocks trades within 2h of high-impact USD events
"""

import asyncio
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Literal
import aiohttp
from config import EIA_API_KEY

log = logging.getLogger("OilSentinel")

Verdict = Literal["BULLISH", "BEARISH", "NEUTRAL"]

# Module-level caches — shared across all calls
_EIA_CACHE:     tuple[str, str, float] = ("NEUTRAL", "Not fetched yet", 0.0)
_HORMUZ_CACHE:  tuple[str, str, float] = ("NEUTRAL", "Not fetched yet", 0.0)
_calendar_cache: tuple[list, float]    = ([], 0.0)

EIA_CACHE_TTL    = 86400   # 24h — EIA publishes weekly
HORMUZ_CACHE_TTL = 14400   # 4h
CALENDAR_TTL     = 3600    # 1h

EIA_URL = (
    "https://api.eia.gov/v2/petroleum/stoc/wstk/data/"
    "?api_key={key}"
    "&frequency=weekly"
    "&data[]=value"
    "&facets[product][]=EPC0"
    "&facets[duoarea][]=NUS"
    "&facets[process][]=SAX"
    "&sort[0][column]=period"
    "&sort[0][direction]=desc"
    "&length=6"
)

TANKERMAP_HORMUZ_URL = "https://tankermap.com/chokepoint/hormuz"

# Hormuz crude tanker disruption threshold — below this = supply disruption signal
HORMUZ_DISRUPTION_THRESHOLD = 5


class OilSentinel:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None

    async def connect(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "Mozilla/5.0 (compatible; research/1.0)"},
        )

    async def close(self):
        if self._session:
            await self._session.close()

    # ── Primary Signal ────────────────────────────────────────────────────────

    async def get_verdict(self) -> tuple[Verdict, str]:
        """
        Combine EIA inventory and TankerMap Hormuz signals.
        Hormuz disruption takes priority (supply shock is immediate).
        EIA drives direction otherwise.
        Returns (verdict, reason).
        """
        eia_v,    eia_r    = await self._eia_verdict()
        hormuz_v, hormuz_r = await self._hormuz_verdict()

        if hormuz_v == "BULLISH":
            return "BULLISH", f"Hormuz disruption: {hormuz_r} | EIA: {eia_r}"

        if eia_v in ("BULLISH", "BEARISH"):
            return eia_v, f"EIA: {eia_r} | Hormuz: {hormuz_r}"

        return "NEUTRAL", f"EIA: {eia_r} | Hormuz: {hormuz_r}"

    # ── EIA Weekly Inventory ──────────────────────────────────────────────────

    async def _eia_verdict(self) -> tuple[Verdict, str]:
        global _EIA_CACHE
        verdict, reason, ts = _EIA_CACHE
        if time.time() - ts < EIA_CACHE_TTL:
            return verdict, reason  # type: ignore[return-value]

        if not EIA_API_KEY:
            _EIA_CACHE = ("NEUTRAL", "EIA_API_KEY not set — add to .env", time.time())
            log.warning("[EIA] EIA_API_KEY not configured")
            return "NEUTRAL", "EIA_API_KEY not set"

        try:
            url = EIA_URL.format(key=EIA_API_KEY)
            async with self._session.get(url) as r:
                if r.status != 200:
                    msg = f"EIA API returned HTTP {r.status}"
                    _EIA_CACHE = ("NEUTRAL", msg, time.time())
                    return "NEUTRAL", msg
                data = await r.json()

            rows = data.get("response", {}).get("data", [])
            if len(rows) < 2:
                msg = "Insufficient EIA data rows"
                _EIA_CACHE = ("NEUTRAL", msg, time.time())
                return "NEUTRAL", msg

            latest  = float(rows[0]["value"])
            prev    = float(rows[1]["value"])
            change  = latest - prev        # positive = build, negative = draw
            period  = rows[0].get("period", "unknown")

            if change < 0:
                v = "BULLISH"
                r_str = (
                    f"Inventory DRAW {abs(change):.0f}kb vs prior week "
                    f"({period}) — supply tightening"
                )
            elif change > 0:
                v = "BEARISH"
                r_str = (
                    f"Inventory BUILD +{change:.0f}kb vs prior week "
                    f"({period}) — supply building"
                )
            else:
                v = "NEUTRAL"
                r_str = f"Inventory unchanged ({period})"

            _EIA_CACHE = (v, r_str, time.time())
            log.info(f"[EIA] {v}: {r_str}")
            return v, r_str

        except Exception as exc:
            msg = f"EIA fetch error: {exc}"
            _EIA_CACHE = ("NEUTRAL", msg, time.time())
            log.warning(f"[EIA] {msg}")
            return "NEUTRAL", msg

    # ── TankerMap Hormuz Scrape ───────────────────────────────────────────────

    async def _hormuz_verdict(self) -> tuple[Verdict, str]:
        global _HORMUZ_CACHE
        verdict, reason, ts = _HORMUZ_CACHE
        if time.time() - ts < HORMUZ_CACHE_TTL:
            return verdict, reason  # type: ignore[return-value]

        try:
            async with self._session.get(TANKERMAP_HORMUZ_URL) as r:
                if r.status != 200:
                    msg = f"TankerMap returned HTTP {r.status} — neutral"
                    _HORMUZ_CACHE = ("NEUTRAL", msg, time.time())
                    return "NEUTRAL", msg
                html = await r.text()

            # Try to parse crude transit count — format varies by page version
            # Pattern 1: "Crude: N" or "crude: N"
            crude_match = re.search(r'[Cc]rude[:\s]+(\d+)', html)
            # Pattern 2: "N tankers/day avg"
            avg_match   = re.search(r'(\d+)\s*tankers?/day', html, re.IGNORECASE)
            # Pattern 3: JSON-like "crude":N in any embedded data
            json_match  = re.search(r'"crude"\s*:\s*(\d+)', html)

            count = None
            if crude_match:
                count = int(crude_match.group(1))
            elif json_match:
                count = int(json_match.group(1))
            elif avg_match:
                count = int(avg_match.group(1))

            if count is not None:
                if count < HORMUZ_DISRUPTION_THRESHOLD:
                    v     = "BULLISH"
                    r_str = (
                        f"Hormuz disruption — only {count} crude tankers detected "
                        f"(threshold <{HORMUZ_DISRUPTION_THRESHOLD})"
                    )
                else:
                    v     = "NEUTRAL"
                    r_str = f"Hormuz normal — {count} crude tankers transiting"
            else:
                # Page is JS-rendered or structure changed — treat as neutral
                v     = "NEUTRAL"
                r_str = "TankerMap: unable to parse transit count (JS-rendered) — neutral"

            _HORMUZ_CACHE = (v, r_str, time.time())
            log.info(f"[HORMUZ] {v}: {r_str}")
            return v, r_str

        except Exception as exc:
            msg = f"TankerMap scrape error: {exc} — neutral"
            _HORMUZ_CACHE = ("NEUTRAL", msg, time.time())
            log.warning(f"[HORMUZ] {msg}")
            return "NEUTRAL", msg

    # ── Macro Calendar (Forex Factory) ───────────────────────────────────────

    async def check_macro_calendar(self, epic: str = "OIL_CRUDE") -> tuple[bool, str]:
        """
        Check for high-impact USD events in the next 2 hours.
        Returns (safe_to_trade, reason).
        Also specifically flags EIA Crude Inventory report (Wednesdays ~14:30 UTC).
        """
        global _calendar_cache
        events, ts = _calendar_cache

        if time.time() - ts > CALENDAR_TTL or not events:
            try:
                url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        events = await resp.json(content_type=None)
                        _calendar_cache = (events, time.time())
                        log.info(f"[CALENDAR] Loaded {len(events)} events")
            except Exception as exc:
                log.debug(f"[CALENDAR] Fetch failed: {exc}")
                return True, "Calendar unavailable — proceeding"

        now       = datetime.now(timezone.utc)
        lookahead = now + timedelta(hours=2)

        for event in events:
            try:
                impact   = event.get("impact", "").lower()
                currency = event.get("country", "").upper()
                title    = event.get("title", "")
                date_str = event.get("date", "") + " " + event.get("time", "")
                event_dt = datetime.strptime(
                    date_str.strip(), "%Y-%m-%d %I:%M%p"
                ).replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if impact != "high":
                continue
            if currency != "USD":
                continue
            if now <= event_dt <= lookahead:
                mins = int((event_dt - now).seconds / 60)
                log.warning(f"[CALENDAR] High-impact event in {mins}min: {title}")
                return False, f"High-impact event in {mins}min: {title}"

        return True, "No high-impact events in next 2h"
