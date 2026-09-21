"""
fetch_trades_cache.py — Run ONCE with VPN on.

Fetches historical whale trades from Polymarket and saves them to
whale_trades_cache.json. The backtester then reads from this file
so it never needs to hit Polymarket again.

Usage:
    python fetch_trades_cache.py [--days 30]
"""

import asyncio
import argparse
import json
import logging
from datetime import datetime, timezone, timedelta
import aiohttp
from aiohttp_socks import ProxyConnector

from config import POLYMARKET_DATA_URL, TRACKED_WALLETS

TOR_PROXY = "socks5://127.0.0.1:9050"  # brew services start tor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
log = logging.getLogger("FetchCache")

CACHE_PATH = "whale_trades_cache.json"


async def fetch_wallet(session: aiohttp.ClientSession, wallet: str, lookback_days: int) -> list[dict]:
    cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
    try:
        url    = f"{POLYMARKET_DATA_URL}/trades"
        params = {"maker_address": wallet, "limit": 500}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning(f"  {wallet[:10]}... → HTTP {resp.status}")
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
        log.error(f"  {wallet[:10]}... → {exc}")
        return []


async def main(lookback_days: int):
    log.info(f"Fetching {lookback_days}-day trade history for {len(TRACKED_WALLETS)} wallets via Tor...")

    cache = {}
    connector = ProxyConnector.from_url(TOR_PROXY)
    async with aiohttp.ClientSession(connector=connector) as session:
        for wallet in TRACKED_WALLETS:
            log.info(f"  Wallet {wallet[:10]}...")
            trades = await fetch_wallet(session, wallet, lookback_days)
            cache[wallet] = trades
            log.info(f"    → {len(trades)} BUY trades")

    total = sum(len(v) for v in cache.values())
    meta  = {
        "_meta": {
            "fetched_at":    datetime.now(timezone.utc).isoformat(),
            "lookback_days": lookback_days,
            "total_trades":  total,
            "wallets":       len(TRACKED_WALLETS),
        }
    }
    output = {**meta, **cache}

    with open(CACHE_PATH, "w") as f:
        json.dump(output, f, indent=2)

    log.info(f"Saved {total} trades across {len(TRACKED_WALLETS)} wallets → {CACHE_PATH}")
    log.info("You can now run the backtester without VPN:")
    log.info("  python backtester.py --days 30")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(main(args.days))
