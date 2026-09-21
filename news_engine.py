"""
NewsEngine — proactive news-driven trade signal generator.

Every NEWS_POLL_INTERVAL seconds, sweeps all EPIC_RSS feeds for new headlines.
On startup, performs a silent warmup pass to baseline seen headlines — so only
genuinely new stories (appearing after boot) fire signals.

When NEWS_MIN_HEADLINES or more unseen headlines appear for an epic within a
single sweep, a NewsSignal is emitted for the ConfluenceGate to evaluate.
Per-epic cooldown prevents duplicate signals within NEWS_COOLDOWN seconds.

Signal flow:
  NewsEngine → NewsSignal → ConfluenceGate.evaluate_news()
             → LLM verdict (BULLISH=BUY, BEARISH=SELL, NOISE=drop)
             → chart gates → RiskManager → Capital.com order
"""

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
import feedparser

from news_sentinel import EPIC_RSS, EPIC_RSS_KEYWORDS

log = logging.getLogger("NewsEngine")

NEWS_POLL_INTERVAL  = 60     # seconds between sweeps
NEWS_COOLDOWN       = 1800   # 30min per-epic cooldown after firing
NEWS_MIN_HEADLINES  = 3      # minimum new headlines needed to fire


@dataclass
class NewsSignal:
    epic:          str
    headline:      str        # primary (first) headline
    all_headlines: list[str]
    source_count:  int
    timestamp:     float = field(default_factory=time.time)


class NewsEngine:
    def __init__(self):
        self._signals:  asyncio.Queue[NewsSignal] = asyncio.Queue()
        self._seen:     set[str] = set()          # headline hashes seen this session
        self._cooldown: dict[str, float] = {}     # epic → last fire timestamp
        self._session:  Optional[aiohttp.ClientSession] = None
        self._running   = False

    @property
    def signals(self) -> asyncio.Queue[NewsSignal]:
        return self._signals

    async def run(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        try:
            # Warmup: populate _seen without firing — avoids flooding on startup
            log.info("[NEWS-ENGINE] Warming up — baselining current headlines...")
            await self._sweep(emit=False)
            log.info("[NEWS-ENGINE] Warmup complete. Watching for new stories...")

            while self._running:
                await asyncio.sleep(NEWS_POLL_INTERVAL)
                await self._sweep(emit=True)
        finally:
            await self._session.close()

    def stop(self):
        self._running = False

    # ── Feed Sweep ────────────────────────────────────────────────────────────

    async def _sweep(self, emit: bool):
        """Poll all EPIC_RSS feeds. If emit=True, fire signals for fresh stories."""
        fetch_tasks = [self._fetch_new_headlines(epic) for epic in EPIC_RSS]
        results     = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        if not emit:
            return  # warmup pass — _seen already populated by _fetch_new_headlines

        now = time.time()
        for epic, result in zip(EPIC_RSS.keys(), results):
            if isinstance(result, Exception) or not result:
                continue

            last_fired = self._cooldown.get(epic, 0)
            if now - last_fired < NEWS_COOLDOWN:
                log.debug(
                    f"[NEWS-ENGINE] {epic} on cooldown "
                    f"({(now - last_fired) / 60:.0f}min since last signal)"
                )
                continue

            if len(result) >= NEWS_MIN_HEADLINES:
                signal = NewsSignal(
                    epic=epic,
                    headline=result[0],
                    all_headlines=result,
                    source_count=len(result),
                )
                self._cooldown[epic] = now
                self._signals.put_nowait(signal)
                log.info(
                    f"[NEWS-ENGINE] Signal fired: {epic} — {len(result)} new headlines | "
                    f"'{result[0][:70]}'"
                )

    # ── Per-Epic Headline Fetch ───────────────────────────────────────────────

    async def _fetch_new_headlines(self, epic: str) -> list[str]:
        """
        Fetch all feeds for this epic, return headlines not previously seen.
        Side-effect: adds seen hashes to self._seen.
        """
        feeds    = EPIC_RSS.get(epic, [])
        keywords = EPIC_RSS_KEYWORDS.get(epic, [])
        new: list[str] = []

        async def _one(url: str) -> list[str]:
            try:
                async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=6)
                ) as resp:
                    if resp.status != 200:
                        return []
                    raw  = await resp.text()
                    feed = feedparser.parse(raw)
                    out  = []
                    for entry in feed.entries[:15]:
                        title = entry.get("title", "")
                        if not title:
                            continue
                        if keywords and not any(kw in title.lower() for kw in keywords):
                            continue
                        h = hashlib.md5(title.encode()).hexdigest()
                        if h not in self._seen:
                            self._seen.add(h)
                            out.append(title)
                    return out
            except Exception:
                return []

        results = await asyncio.gather(*[_one(url) for url in feeds])
        for r in results:
            new.extend(r)
        return new
