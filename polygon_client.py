"""
polygon_client.py — Massive.com (Polygon.io) data client.

Provides two things:
  1. OHLCV bars — replaces Capital.com for chart analysis (higher quality, longer history)
  2. News headlines — replaces GDELT (ticker-tagged, more relevant)

Free tier: 5 req/min REST, no WebSocket. We cache aggressively to stay within limits.
Capital.com is still used exclusively for order execution and real-time spread/price.

Epic → Polygon ticker mapping:
  BTCUSD    → X:BTCUSD   (crypto)
  ETHUSD    → X:ETHUSD
  GOLD      → C:XAUUSD   (forex/commodity)
  OIL_CRUDE → C:BCOUSD   (Brent proxy — free tier has no WTI futures)
  US500     → SPY        (ETF proxy, free tier)
  US100     → I:NDX
  EURUSD    → C:EURUSD
  USDJPY    → C:USDJPY
  GBPUSD    → C:GBPUSD
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
import aiohttp
import pandas as pd
from config import POLYGON_API_KEY, POLYGON_BASE_URL

log = logging.getLogger("PolygonClient")

# Epic → Polygon ticker
EPIC_TO_TICKER: dict[str, str] = {
    "BTCUSD":    "X:BTCUSD",
    "ETHUSD":    "X:ETHUSD",
    "GOLD":      "C:XAUUSD",
    "SILVER":    "C:XAGUSD",
    "OIL_CRUDE": "C:BCOUSD",
    "US500":     "SPY",
    "US100":     "I:NDX",
    "US30":      "I:DJI",
    "EURUSD":    "C:EURUSD",
    "USDJPY":    "C:USDJPY",
    "GBPUSD":    "C:GBPUSD",
    # NATGAS and DE30 not on Polygon free tier — Capital.com OHLCV used as fallback
}

# News search terms per epic (Polygon news uses ticker OR keyword search)
EPIC_TO_KEYWORDS: dict[str, str] = {
    "BTCUSD":    "bitcoin",
    "ETHUSD":    "ethereum",
    "GOLD":      "gold",
    "SILVER":    "silver",
    "OIL_CRUDE": "crude oil",
    "NATGAS":    "natural gas",
    "US500":     "S&P 500",
    "US100":     "nasdaq",
    "US30":      "dow jones",
    "DE30":      "DAX",
    "EURUSD":    "euro dollar",
    "USDJPY":    "yen",
    "GBPUSD":    "sterling",
}

# Cache TTL
_OHLCV_CACHE_TTL  = 300    # 5 min — re-fetch OHLCV after 5 min
_NEWS_CACHE_TTL   = 300    # 5 min

_ohlcv_cache: dict[str, tuple[pd.DataFrame, float]] = {}
_news_cache:  dict[str, tuple[list[str], float]]    = {}


class PolygonClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None

    async def connect(self):
        self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session:
            await self._session.close()

    # ── OHLCV ─────────────────────────────────────────────────────────────────

    async def get_ohlcv(self, epic: str, bars: int = 100,
                        timespan: str = "minute", multiplier: int = 15) -> Optional[pd.DataFrame]:
        """
        Fetch OHLCV bars from Polygon. Returns a DataFrame with columns:
        open, high, low, close, volume — indexed by UTC datetime.
        Returns None if epic not supported or API fails.
        """
        if not POLYGON_API_KEY:
            return None

        ticker = EPIC_TO_TICKER.get(epic)
        if not ticker:
            log.debug(f"No Polygon ticker for {epic}")
            return None

        # Cache key includes resolution so 15m and 4H data don't collide
        cache_key = f"{epic}:{multiplier}{timespan}"
        if cache_key in _ohlcv_cache:
            df, ts = _ohlcv_cache[cache_key]
            if time.time() - ts < _OHLCV_CACHE_TTL:
                return df

        now = datetime.now(timezone.utc)
        # For 4H bars we need ~34 days to compute EMA 200; use 60 days to be safe
        lookback_days = 60 if (timespan == "hour" and multiplier >= 4) else 5
        start = now - timedelta(days=lookback_days)
        url   = (
            f"{POLYGON_BASE_URL}/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/"
            f"{start.strftime('%Y-%m-%d')}/{now.strftime('%Y-%m-%d')}"
        )
        params = {
            "adjusted": "true",
            "sort":     "asc",
            "limit":    bars,
            "apiKey":   POLYGON_API_KEY,
        }

        try:
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    log.warning(f"Polygon OHLCV {resp.status} for {epic}")
                    return None
                data = await resp.json()

            results = data.get("results", [])
            if not results:
                log.debug(f"Polygon returned no bars for {epic}")
                return None

            df = pd.DataFrame([{
                "time":   pd.Timestamp(r["t"], unit="ms", tz="UTC"),
                "open":   r["o"],
                "high":   r["h"],
                "low":    r["l"],
                "close":  r["c"],
                "volume": r.get("v", 0),
            } for r in results])
            df.set_index("time", inplace=True)

            _ohlcv_cache[cache_key] = (df, time.time())
            log.info(f"[POLYGON] OHLCV fetched: {len(df)} bars for {epic}")
            return df

        except Exception as exc:
            log.warning(f"Polygon OHLCV failed for {epic}: {exc}")
            return None

    async def get_ohlcv_h4(self, epic: str, bars: int = 210) -> Optional[pd.DataFrame]:
        """4-hour bars for macro trend (EMA 200)."""
        return await self.get_ohlcv(epic, bars=bars, timespan="hour", multiplier=4)

    # ── News ──────────────────────────────────────────────────────────────────

    async def get_news(self, epic: str, limit: int = 5) -> list[str]:
        """
        Fetch recent news headlines from Polygon for the given epic.
        Returns a list of title strings (same interface as NewsSentinel.fetch_recent_headlines).
        """
        if not POLYGON_API_KEY:
            return []

        ticker   = EPIC_TO_TICKER.get(epic, "")
        keywords = EPIC_TO_KEYWORDS.get(epic, epic)

        # Cache check
        cache_key = epic
        if cache_key in _news_cache:
            headlines, ts = _news_cache[cache_key]
            if time.time() - ts < _NEWS_CACHE_TTL:
                return headlines

        since = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "limit":              limit,
            "published_utc.gte":  since,
            "order":              "desc",
            "sort":               "published_utc",
            "apiKey":             POLYGON_API_KEY,
        }
        # Use ticker if available, otherwise keyword search
        if ticker:
            params["ticker"] = ticker.replace("X:", "").replace("C:", "").replace("I:", "")

        url = f"{POLYGON_BASE_URL}/v2/reference/news"

        try:
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    log.debug(f"Polygon news {resp.status} for {epic}")
                    return []
                data = await resp.json()

            articles  = data.get("results", [])
            headlines = [a["title"] for a in articles if a.get("title")]
            _news_cache[cache_key] = (headlines, time.time())
            log.info(f"[POLYGON] {len(headlines)} news headlines for {epic}")
            return headlines

        except Exception as exc:
            log.warning(f"Polygon news failed for {epic}: {exc}")
            return []
