"""
scan_macro_whales.py — Find whale wallets trading the curated macro markets.

Queries Polymarket data API for BUY trades on each curated condition_id,
ranks wallets by total size traded, and prints the top N for use in config.py.

Run with Tor active:  brew services start tor
Usage:               python scan_macro_whales.py [--top 20] [--limit 500]
"""

import asyncio
import argparse
import json
import logging
from collections import defaultdict

import aiohttp
from aiohttp_socks import ProxyConnector

TOR_PROXY  = "socks5://127.0.0.1:9050"
DATA_URL   = "https://data-api.polymarket.com"
MAP_PATH   = "market_map.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
log = logging.getLogger("ScanWhales")


def load_condition_ids() -> dict[str, str]:
    """Return {condition_id: description} for all curated markets."""
    with open(MAP_PATH) as f:
        raw = json.load(f)

    result = {}
    for k, v in raw.get("curated", {}).items():
        if isinstance(v, dict) and v.get("epic"):
            result[k] = v.get("description", k[:20])
    return result


async def fetch_trades_for_market(
    session: aiohttp.ClientSession,
    condition_id: str,
    limit: int,
) -> list[dict]:
    """
    Fetch trades for a specific conditionId.
    NOTE: the data API ignores the conditionId filter, so we fetch a large
    batch and filter client-side by conditionId.
    """
    url    = f"{DATA_URL}/trades"
    # Fetch a large window and filter locally — API ignores conditionId param
    params = {"limit": min(limit, 500)}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                log.warning(f"  HTTP {resp.status} for {condition_id[:14]}...")
                return []
            data = await resp.json()
            if not isinstance(data, list):
                return []
            # Filter to only trades for this conditionId
            return [t for t in data if (t.get("conditionId") or "").lower() == condition_id.lower()]
    except Exception as exc:
        log.error(f"  Fetch error for {condition_id[:14]}...: {exc}")
        return []


async def main(top_n: int, limit: int):
    condition_ids = load_condition_ids()
    log.info(f"Scanning {len(condition_ids)} curated markets for whale activity...")

    # wallet -> {total_size, trade_count, markets_set}
    wallet_stats: dict[str, dict] = defaultdict(
        lambda: {"total_size": 0.0, "trade_count": 0, "markets": set()}
    )

    connector = ProxyConnector.from_url(TOR_PROXY)
    async with aiohttp.ClientSession(connector=connector) as session:
        for cid, desc in condition_ids.items():
            log.info(f"  Fetching trades for: {desc[:60]}")
            trades = await fetch_trades_for_market(session, cid, limit)
            log.info(f"    → {len(trades)} trades returned")

            buy_count = 0
            for t in trades:
                side = (t.get("side") or "").upper()
                if side != "BUY":
                    continue
                # Polymarket data API uses "proxyWallet" for the trader address
                wallet = (t.get("proxyWallet") or t.get("maker") or t.get("makerAddress") or "").lower()
                if not wallet or wallet == "0x0000000000000000000000000000000000000000":
                    continue

                size = float(t.get("size") or t.get("amount") or 0)
                wallet_stats[wallet]["total_size"]  += size
                wallet_stats[wallet]["trade_count"] += 1
                wallet_stats[wallet]["markets"].add(cid[:14])
                buy_count += 1

            log.info(f"    → {buy_count} BUY trades from unique makers")

    if not wallet_stats:
        log.error("No wallets found. Check that Tor is running and the condition IDs are active markets.")
        return

    # Rank by total size traded
    ranked = sorted(
        wallet_stats.items(),
        key=lambda x: x[1]["total_size"],
        reverse=True,
    )

    print("\n" + "=" * 70)
    print(f"TOP {top_n} WHALE WALLETS on curated macro markets")
    print("=" * 70)
    for i, (wallet, stats) in enumerate(ranked[:top_n], 1):
        print(
            f"  {i:2d}. {wallet}  "
            f"size={stats['total_size']:,.0f}  "
            f"trades={stats['trade_count']}  "
            f"markets={len(stats['markets'])}"
        )

    print("\n# Paste into config.py → TRACKED_WALLETS:")
    print("TRACKED_WALLETS: list[str] = [")
    for wallet, stats in ranked[:top_n]:
        print(f"    \"{wallet}\",  # size={stats['total_size']:,.0f} trades={stats['trade_count']}")
    print("]")

    # Save full results to JSON for inspection
    output_path = "macro_whale_scan.json"
    with open(output_path, "w") as f:
        json.dump(
            [
                {
                    "wallet":      w,
                    "total_size":  s["total_size"],
                    "trade_count": s["trade_count"],
                    "markets":     list(s["markets"]),
                }
                for w, s in ranked
            ],
            f,
            indent=2,
        )
    log.info(f"Full results saved to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top",   type=int, default=20,  help="Top N wallets to show (default 20)")
    parser.add_argument("--limit", type=int, default=500, help="Trades per market to fetch (default 500)")
    args = parser.parse_args()
    asyncio.run(main(args.top, args.limit))
