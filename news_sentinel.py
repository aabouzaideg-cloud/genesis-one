"""
News Sentinel — three-layer news intelligence system.

Layer 1 — Sources (headlines):
  - Polygon.io   : ticker-tagged financial news (primary, called from ConfluenceGate)
  - RSS feeds    : Reuters, BBC, CNBC, CoinDesk — real-time, no API key needed
  - GDELT        : fallback when RSS/Polygon return nothing

Layer 2 — Sentiment (dual LLM):
  - Gemma 3:4b (local/Ollama) : fast first-pass verdict
  - Claude Haiku (API)        : cross-check — both must agree to pass
  - If they disagree          : conservative default (NOISE → proceed, BEARISH → block)

Layer 3 — Macro Calendar:
  - Forex Factory calendar    : checks for high-impact events in next 2 hours
  - If CPI/NFP/FOMC imminent  : hold signal (too much uncertainty)
"""

import asyncio
import logging
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional
import aiohttp
import feedparser
from config import OLLAMA_BASE_URL, OLLAMA_MODEL, ANTHROPIC_API_KEY, CLAUDE_VERIFY_MODEL

log = logging.getLogger("NewsSentinel")

Sentiment = Literal["BULLISH", "BEARISH", "NOISE"]

_sentiment_cache: dict[str, tuple[Sentiment, str, float]] = {}
_calendar_cache: tuple[list[dict], float] = ([], 0.0)
CACHE_TTL     = 300   # 5 min
CALENDAR_TTL  = 3600  # 1 hour — economic calendar doesn't change often

# ── RSS feed sources per epic ─────────────────────────────────────────────────
_GENERIC_FEEDS = [
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
]
_CRYPTO_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
]
_GEO_FEEDS = [
    "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "https://feeds.reuters.com/Reuters/worldNews",
]

EPIC_RSS: dict[str, list[str]] = {
    "BTCUSD":    _GENERIC_FEEDS + _CRYPTO_FEEDS,
    "ETHUSD":    _GENERIC_FEEDS + _CRYPTO_FEEDS,
    "GOLD":      _GENERIC_FEEDS,
    "SILVER":    _GENERIC_FEEDS,
    "OIL_CRUDE": _GENERIC_FEEDS + _GEO_FEEDS,
    "NATGAS":    _GENERIC_FEEDS + _GEO_FEEDS,
    "US500":     _GENERIC_FEEDS,
    "US100":     _GENERIC_FEEDS,
    "US30":      _GENERIC_FEEDS,
    "DE30":      _GENERIC_FEEDS + _GEO_FEEDS,
    "EURUSD":    _GENERIC_FEEDS,
    "USDJPY":    _GENERIC_FEEDS,
    "GBPUSD":    _GENERIC_FEEDS,
}

# Keywords to filter RSS headlines by relevance per epic
EPIC_RSS_KEYWORDS: dict[str, list[str]] = {
    "BTCUSD":    ["bitcoin", "btc", "crypto", "cryptocurrency", "ethereum", "digital asset"],
    "ETHUSD":    ["ethereum", "eth", "crypto", "defi", "ether"],
    "GOLD":      ["gold", "xau", "precious metal", "safe haven", "bullion"],
    "SILVER":    ["silver", "xag", "precious metal"],
    "OIL_CRUDE": ["oil", "crude", "opec", "wti", "brent", "iran", "hormuz", "petroleum", "energy"],
    "NATGAS":    ["natural gas", "lng", "gas", "pipeline", "energy"],
    "US500":     ["s&p", "federal reserve", "fed", "fomc", "rate cut", "recession", "wall street", "stocks"],
    "US100":     ["nasdaq", "tech", "nvidia", "semiconductor", "big tech"],
    "US30":      ["dow", "djia", "industrial", "tariff", "trade war"],
    "DE30":      ["dax", "germany", "ukraine", "russia", "ceasefire", "nato", "europe"],
    "EURUSD":    ["euro", "ecb", "european", "eur/usd", "dollar"],
    "USDJPY":    ["yen", "bank of japan", "boj", "japan"],
    "GBPUSD":    ["pound", "sterling", "bank of england", "boe", "britain"],
}

# Contradiction terms — hard override before LLM
_CONTRADICTION_TERMS: dict[str, list[str]] = {
    "OIL_CRUDE": ["ceasefire", "peace deal", "diplomatic resolution", "sanctions lifted",
                  "deal reached", "agreement signed", "de-escalation", "truce", "normalization"],
    "US500":     ["recession confirmed", "market crash", "circuit breaker", "trading halt",
                  "emergency rate hike", "credit crisis", "bank run"],
    "BTCUSD":    ["bitcoin banned", "crypto ban", "sec rejects", "exchange hacked",
                  "exchange collapse", "crypto crackdown"],
}

GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


class NewsSentinel:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._claude  = None

    async def connect(self):
        self._session = aiohttp.ClientSession()
        if ANTHROPIC_API_KEY:
            try:
                import anthropic
                self._claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
                log.info("[NEWS] Claude API cross-check enabled.")
            except Exception as e:
                log.warning(f"[NEWS] Claude API unavailable: {e}")

    async def close(self):
        if self._session:
            await self._session.close()

    # ── Layer 1: RSS Feed Fetching ────────────────────────────────────────────

    async def _fetch_rss(self, epic: str) -> list[str]:
        """Fetch and filter RSS headlines for the given epic."""
        feeds    = EPIC_RSS.get(epic, _GENERIC_FEEDS)
        keywords = EPIC_RSS_KEYWORDS.get(epic, [])
        headlines: list[str] = []

        async def _fetch_one(url: str):
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status != 200:
                        return []
                    raw = await resp.text()
                    feed = feedparser.parse(raw)
                    results = []
                    for entry in feed.entries[:15]:
                        title = entry.get("title", "")
                        if not title:
                            continue
                        if not keywords or any(kw in title.lower() for kw in keywords):
                            results.append(title)
                    return results
            except Exception:
                return []

        results = await asyncio.gather(*[_fetch_one(url) for url in feeds])
        for r in results:
            headlines.extend(r)

        # Deduplicate and cap at 8
        seen, unique = set(), []
        for h in headlines:
            if h not in seen:
                seen.add(h)
                unique.append(h)

        log.info(f"[RSS] {len(unique)} headlines for {epic}")
        return unique[:8]

    # ── Layer 1: GDELT Fallback ───────────────────────────────────────────────

    async def fetch_recent_headlines(self, epic: str) -> list[str]:
        """RSS-first, GDELT fallback. Returns up to 8 headlines."""
        headlines = await self._fetch_rss(epic)
        if headlines:
            return headlines

        # GDELT fallback
        query = self._asset_to_query(epic)
        params = {"query": query, "mode": "artlist", "maxrecords": 5,
                  "timespan": "15min", "sort": "DateDesc", "format": "json"}
        try:
            async with self._session.get(
                GDELT_URL, params=params, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                gdelt = [a["title"] for a in data.get("articles", []) if a.get("title")]
                log.info(f"[GDELT] {len(gdelt)} fallback headlines for {epic}")
                return gdelt
        except Exception as exc:
            log.warning(f"GDELT fallback failed for {epic}: {exc}")
            return []

    def _asset_to_query(self, epic: str) -> str:
        mapping = {
            "BTCUSD":    '"Bitcoin" OR "BTC" OR "crypto" OR "Bitcoin ETF"',
            "ETHUSD":    '"Ethereum" OR "ETH" OR "Ethereum ETF" OR "DeFi"',
            "GOLD":      '"gold price" OR "spot gold" OR "XAU" OR "safe haven gold"',
            "SILVER":    '"silver price" OR "spot silver" OR "XAG"',
            "OIL_CRUDE": '"crude oil" OR "OPEC" OR "WTI" OR "Brent" OR "oil price"',
            "NATGAS":    '"natural gas" OR "LNG" OR "gas pipeline"',
            "US500":     '"S&P 500" OR "Federal Reserve" OR "Fed rate" OR "recession"',
            "US100":     '"Nasdaq" OR "tech stocks" OR "semiconductor" OR "Big Tech"',
            "US30":      '"Dow Jones" OR "DJIA" OR "trade war" OR "tariff"',
            "DE30":      '"DAX" OR "Germany" OR "Ukraine ceasefire" OR "Russia Ukraine"',
            "EURUSD":    '"euro" OR "ECB" OR "European Central Bank" OR "EUR/USD"',
            "USDJPY":    '"yen" OR "Bank of Japan" OR "BOJ" OR "USD/JPY"',
            "GBPUSD":    '"pound sterling" OR "Bank of England" OR "BOE" OR "GBP"',
        }
        return mapping.get(epic, f'"{epic}"')

    # ── Layer 3: Macro Calendar ───────────────────────────────────────────────

    async def check_macro_calendar(self, epic: str) -> tuple[bool, str]:
        """
        Check Forex Factory calendar for high-impact events in the next 2 hours.
        Returns (safe_to_trade, reason).
        safe_to_trade=False means a major data release is imminent — hold the signal.
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
                        log.info(f"[CALENDAR] Loaded {len(events)} events for this week")
            except Exception as e:
                log.debug(f"[CALENDAR] Fetch failed: {e}")
                return True, "Calendar unavailable — proceeding"

        # Epic → currencies that matter
        epic_currencies = {
            "US500": ["USD"], "US100": ["USD"], "US30": ["USD"],
            "GOLD":  ["USD"], "SILVER": ["USD"], "OIL_CRUDE": ["USD"],
            "NATGAS": ["USD"], "BTCUSD": ["USD"], "ETHUSD": ["USD"],
            "EURUSD": ["USD", "EUR"], "USDJPY": ["USD", "JPY"],
            "GBPUSD": ["USD", "GBP"], "DE30": ["EUR"],
        }
        relevant_currencies = epic_currencies.get(epic, ["USD"])

        now       = datetime.now(timezone.utc)
        lookahead = now + timedelta(hours=2)

        for event in events:
            try:
                impact   = event.get("impact", "").lower()
                currency = event.get("country", "").upper()
                title    = event.get("title", "")
                date_str = event.get("date", "") + " " + event.get("time", "")
                event_dt = datetime.strptime(date_str.strip(), "%Y-%m-%d %I:%M%p").replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if impact != "high":
                continue
            if currency not in relevant_currencies:
                continue
            if now <= event_dt <= lookahead:
                log.warning(f"[CALENDAR] High-impact event imminent: {title} @ {event_dt.strftime('%H:%M UTC')}")
                return False, f"High-impact event in {int((event_dt-now).seconds/60)}min: {title}"

        return True, "No high-impact events in next 2h"

    # ── Layer 2: Dual LLM Sentiment ───────────────────────────────────────────

    def _contradiction_detected(self, headlines: list[str], epic: str, direction: str) -> str | None:
        if direction != "YES":
            return None
        combined = " ".join(headlines).lower()
        for term in _CONTRADICTION_TERMS.get(epic, []):
            if term in combined:
                return term
        return None

    async def classify(self, headlines: list[str], epic: str, whale_direction: str) -> tuple[Sentiment, str]:
        """
        Dual LLM classification: Gemma (fast) + Claude (verify).
        Both must agree on BULLISH to pass. Disagreement = NOISE.
        Returns (Sentiment, reason_string).
        """
        if not headlines:
            return "NOISE", "No recent headlines — neutral."

        # Hard contradiction override
        contradiction = self._contradiction_detected(headlines, epic, whale_direction)
        if contradiction:
            reason = f"Contradiction: '{contradiction}' overrides bullish thesis."
            log.info(f"[NEWS] {epic} CONTRADICTION: {reason}")
            return "BEARISH", reason

        cache_key = f"{epic}|{'|'.join(headlines[:3])}"
        if cache_key in _sentiment_cache:
            result, reason, ts = _sentiment_cache[cache_key]
            if time.time() - ts < CACHE_TTL:
                log.info(f"[NEWS] Cache hit: {epic} → {result}")
                return result, reason

        headlines_text = "\n".join(f"- {h}" for h in headlines[:6])

        # ── Gemma (fast, local) ────────────────────────────────────────────
        gemma_verdict, gemma_reason = await self._gemma_classify(headlines_text, epic, whale_direction)
        log.info(f"[GEMMA] {epic} → {gemma_verdict} | {gemma_reason}")

        # ── Claude (cross-check) ───────────────────────────────────────────
        if self._claude:
            claude_verdict, claude_reason = await self._claude_classify(headlines_text, epic, whale_direction)
            log.info(f"[CLAUDE] {epic} → {claude_verdict} | {claude_reason}")

            # Consensus logic
            if gemma_verdict == claude_verdict:
                # Both agree
                final_verdict = gemma_verdict
                final_reason  = f"Gemma+Claude agree: {claude_reason}"
            elif gemma_verdict == "BULLISH" and claude_verdict == "BEARISH":
                # Claude overrides Gemma when bearish — more conservative
                final_verdict = "BEARISH"
                final_reason  = f"Claude override (Gemma=BULLISH, Claude=BEARISH): {claude_reason}"
            elif gemma_verdict == "BEARISH" and claude_verdict == "BULLISH":
                # Disagreement — default to NOISE (don't force a trade)
                final_verdict = "NOISE"
                final_reason  = f"LLM disagreement (Gemma=BEARISH, Claude=BULLISH) — treating as neutral"
            else:
                # One says NOISE — use the non-NOISE verdict conservatively
                if "BEARISH" in (gemma_verdict, claude_verdict):
                    final_verdict = "BEARISH"
                    final_reason  = f"One model bearish — blocking: {gemma_reason}"
                else:
                    final_verdict = "NOISE"
                    final_reason  = f"Inconclusive: Gemma={gemma_verdict}, Claude={claude_verdict}"
        else:
            # Claude unavailable — rely on Gemma alone
            final_verdict = gemma_verdict
            final_reason  = f"Gemma only (Claude unavailable): {gemma_reason}"

        _sentiment_cache[cache_key] = (final_verdict, final_reason, time.time())
        log.info(f"[NEWS] {epic} | FINAL={final_verdict} | {final_reason}")
        return final_verdict, final_reason

    async def _gemma_classify(self, headlines_text: str, epic: str, direction: str) -> tuple[Sentiment, str]:
        prompt = (
            f"You are a macro trading analyst. A whale just bet {direction} on {epic}.\n\n"
            f"Headlines:\n{headlines_text}\n\n"
            f"Reply EXACTLY:\nVERDICT: <BULLISH|BEARISH|NOISE>\nREASON: <one sentence>"
        )
        payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                   "options": {"temperature": 0.0, "num_predict": 80}}
        try:
            async with self._session.post(
                f"{OLLAMA_BASE_URL}/api/generate", json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                resp.raise_for_status()
                raw = (await resp.json()).get("response", "").strip()
                return self._parse_verdict(raw)
        except Exception as exc:
            log.warning(f"Gemma failed: {exc}")
            return "NOISE", f"Gemma unavailable: {exc}"

    async def _claude_classify(self, headlines_text: str, epic: str, direction: str) -> tuple[Sentiment, str]:
        prompt = (
            f"You are a macro trading analyst. A whale just bet {direction} on a Polymarket "
            f"market related to {epic}.\n\n"
            f"Headlines:\n{headlines_text}\n\n"
            f"Is current news BULLISH, BEARISH, or NOISE for {epic}?\n"
            f"Reply EXACTLY:\nVERDICT: <BULLISH|BEARISH|NOISE>\nREASON: <one sentence>"
        )
        try:
            msg = await self._claude.messages.create(
                model=CLAUDE_VERIFY_MODEL,
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
            return self._parse_verdict(raw)
        except Exception as exc:
            log.warning(f"Claude classify failed: {exc}")
            return "NOISE", f"Claude unavailable: {exc}"

    def _parse_verdict(self, raw: str) -> tuple[Sentiment, str]:
        sentiment: Sentiment = "NOISE"
        reason = "No parseable verdict."
        for line in raw.splitlines():
            line = line.strip()
            if line.upper().startswith("VERDICT:"):
                word = line.split(":", 1)[1].strip().upper()
                if word in ("BULLISH", "BEARISH", "NOISE"):
                    sentiment = word
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()
        if sentiment == "NOISE" and raw:
            first = raw.split()[0].upper()
            if first in ("BULLISH", "BEARISH"):
                sentiment = first
        return sentiment, reason

    # ── Public Interface ──────────────────────────────────────────────────────

    async def supports_whale(self, epic: str, whale_direction: str) -> tuple[bool, str]:
        headlines = await self.fetch_recent_headlines(epic)
        sentiment, reason = await self.classify(headlines, epic, whale_direction)
        if whale_direction == "YES" and sentiment == "BULLISH":
            return True, reason
        if whale_direction == "NO" and sentiment == "BEARISH":
            return True, reason
        return False, f"News={sentiment} does not support {whale_direction} thesis. {reason}"
