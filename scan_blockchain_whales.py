"""
scan_blockchain_whales.py — Find whale wallets via Polygon blockchain data.

Queries the Polygonscan API for ERC-1155 TransferSingle mint events on the
Polymarket ConditionalTokens contract, filtered to our target market token IDs.
A "mint" (from=0x0) means someone BOUGHT that YES/NO token.

Requirements:
  pip install aiohttp aiohttp-socks

Usage:
  export POLYGONSCAN_API_KEY=<your_key>
  python scan_blockchain_whales.py [--days 60] [--top 20] [--min-size 100]

Get a free API key at: https://polygonscan.com/register
(Etherscan account also works — same company, use V2 API chainid=137)
"""

import asyncio
import argparse
import json
import logging
import os
import time
from collections import defaultdict

import aiohttp
from aiohttp_socks import ProxyConnector

TOR_PROXY = "socks5://127.0.0.1:9050"

# ── Contracts ─────────────────────────────────────────────────────────────────
# Polymarket ConditionalTokens (ERC-1155) on Polygon
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# NegRisk adapter — wraps CT for binary markets (most political/macro markets)
NEG_RISK_ADAPTER   = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ERC-1155 TransferSingle event sig
TRANSFER_SINGLE    = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
ZERO_ADDR_TOPIC    = "0x0000000000000000000000000000000000000000000000000000000000000000"

# Polygon avg block time: ~2.2s
BLOCKS_PER_DAY     = int(86400 / 2.2)
CHUNK_SIZE         = 10_000          # Etherscan getLogs max block range
RATE_LIMIT_DELAY   = 0.22            # ~5 req/s on free tier

# Etherscan V2 API (works for Polygon with chainid=137)
ETHERSCAN_V2       = "https://api.etherscan.io/v2/api"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
)
log = logging.getLogger("BlockchainScan")


def load_token_ids() -> dict[str, dict]:
    """
    Load YES/NO token IDs for all curated markets from market_map.json.
    Returns {token_id_hex: {label, epic, conditionId}}.
    """
    from market_mapper import MAP_PATH
    with open(MAP_PATH) as f:
        raw = json.load(f)

    result = {}
    for cid, v in raw.get("curated", {}).items():
        if not isinstance(v, dict) or not v.get("epic"):
            continue
        # fetch token IDs from CLOB synchronously (called once before async loop)
        # We'll populate this in main() after CLOB lookups
        result[cid] = {"epic": v["epic"], "description": v.get("description", ""), "tokens": {}}
    return result


async def fetch_token_ids(session: aiohttp.ClientSession, condition_ids: list[str]) -> dict[str, dict]:
    """Fetch YES/NO token IDs from Polymarket CLOB for each conditionId."""
    token_map = {}  # token_id_hex -> {label, conditionId}

    for cid in condition_ids:
        try:
            url = f"https://clob.polymarket.com/markets/{cid}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.warning(f"CLOB {r.status} for {cid[:14]}...")
                    continue
                data = await r.json()

            tokens = data.get("tokens", [])
            q = data.get("question", cid[:30])
            for t in tokens:
                tid_dec = t.get("token_id", "")
                outcome = t.get("outcome", "?")
                if not tid_dec:
                    continue
                # Convert decimal token ID to 32-byte hex topic
                tid_hex = "0x" + int(tid_dec).to_bytes(32, "big").hex()
                token_map[tid_hex] = {
                    "conditionId": cid,
                    "outcome":     outcome,
                    "label":       f"{q[:50]} [{outcome}]",
                    "tid_dec":     tid_dec,
                }
            log.info(f"  {cid[:14]}...  {q[:55]}  → {len(tokens)} tokens")
        except Exception as exc:
            log.warning(f"  CLOB error for {cid[:14]}...: {exc}")

    return token_map


async def get_current_block(session: aiohttp.ClientSession, api_key: str) -> int:
    params = {
        "chainid": 137, "module": "proxy", "action": "eth_blockNumber",
        "apikey": api_key,
    }
    async with session.get(ETHERSCAN_V2, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
        d = await r.json()
    return int(d["result"], 16)


async def fetch_logs_chunk(
    session: aiohttp.ClientSession,
    api_key: str,
    contract: str,
    from_block: int,
    to_block: int,
) -> list[dict]:
    """Fetch TransferSingle mint events (from=0x0) for one block range chunk."""
    params = {
        "chainid":   137,
        "module":    "logs",
        "action":    "getLogs",
        "address":   contract,
        "topic0":    TRANSFER_SINGLE,
        "topic2":    ZERO_ADDR_TOPIC,      # from = 0x0 = mint/buy
        "topic0_2_opr": "and",
        "fromBlock": from_block,
        "toBlock":   to_block,
        "page":      1,
        "offset":    1000,
        "apikey":    api_key,
    }
    try:
        async with session.get(ETHERSCAN_V2, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            d = await r.json()
        result = d.get("result", [])
        if not isinstance(result, list):
            # Rate limit or error
            msg = d.get("result", "") or d.get("message", "")
            if "rate" in str(msg).lower() or "limit" in str(msg).lower():
                await asyncio.sleep(1)
            return []
        return result
    except Exception as exc:
        log.debug(f"getLogs error {from_block}-{to_block}: {exc}")
        return []


def decode_transfer_single(log_entry: dict) -> tuple[str, str, int, int] | None:
    """
    Decode a TransferSingle log entry.
    Returns (operator, to_addr, token_id_int, value) or None on failure.
    topics = [event_sig, operator, from, to]
    data   = abi.encode(uint256 id, uint256 value) = 64 bytes
    """
    try:
        topics = log_entry.get("topics", [])
        data   = log_entry.get("data", "")

        if len(topics) < 4 or len(data) < 130:   # 0x + 128 hex chars
            return None

        to_addr   = "0x" + topics[3][-40:]       # last 20 bytes of topic3
        raw_data  = data[2:]                      # strip 0x
        token_id  = int(raw_data[:64], 16)        # first 32 bytes = id
        value     = int(raw_data[64:128], 16)     # next 32 bytes = value

        return to_addr.lower(), token_id, value
    except Exception:
        return None


async def scan_contract(
    session: aiohttp.ClientSession,
    api_key: str,
    contract: str,
    from_block: int,
    to_block: int,
    target_tokens: set[int],          # token IDs as ints
    wallet_stats: dict,
    token_map_int: dict[int, dict],   # int token_id -> metadata
) -> int:
    """Scan one contract over the given block range. Returns total logs processed."""
    total = 0
    block = from_block
    label = contract[:10] + "..."

    while block <= to_block:
        end = min(block + CHUNK_SIZE - 1, to_block)
        logs = await fetch_logs_chunk(session, api_key, contract, block, end)
        total += len(logs)

        for entry in logs:
            decoded = decode_transfer_single(entry)
            if not decoded:
                continue
            to_addr, token_id, value = decoded
            if token_id not in target_tokens:
                continue

            meta = token_map_int[token_id]
            wallet_stats[to_addr]["total_size"]  += value
            wallet_stats[to_addr]["trade_count"] += 1
            wallet_stats[to_addr]["markets"].add(meta["label"])
            wallet_stats[to_addr]["condition_ids"].add(meta["conditionId"])

        pct = (end - from_block) / max(to_block - from_block, 1) * 100
        log.debug(f"  {label} {block}-{end}  logs={len(logs)}  {pct:.0f}%")
        block = end + 1
        await asyncio.sleep(RATE_LIMIT_DELAY)

    return total


async def main(api_key: str, lookback_days: int, top_n: int, min_size: int):
    log.info(f"Blockchain whale scan: {lookback_days} days lookback, top {top_n}, min_size={min_size}")

    # CLOB (blocked in UAE) → use Tor; Etherscan → direct connection
    tor_connector = ProxyConnector.from_url(TOR_PROXY)
    async with aiohttp.ClientSession(connector=tor_connector) as tor_session:
        # ── Step 1: Resolve token IDs from CLOB via Tor ──────────────────────
        log.info("Fetching token IDs from Polymarket CLOB (via Tor)...")
        market_data = load_token_ids()
        condition_ids = list(market_data.keys())
        log.info(f"  {len(condition_ids)} curated markets")

        token_map_hex = await fetch_token_ids(tor_session, condition_ids)
        if not token_map_hex:
            log.error("No token IDs resolved. Check Tor is running: brew services start tor")
            return

        # Build int-keyed version for data decoding
        token_map_int = {int(tid_hex, 16): meta for tid_hex, meta in token_map_hex.items()}
        target_tokens = set(token_map_int.keys())
        log.info(f"  {len(target_tokens)} target token IDs to track")

    # ── Step 2 & 3: Etherscan queries via direct connection ──────────────────
    async with aiohttp.ClientSession() as session:
        log.info("Fetching current Polygon block...")
        current_block = await get_current_block(session, api_key)
        from_block    = current_block - (BLOCKS_PER_DAY * lookback_days)
        log.info(f"  Block range: {from_block:,} → {current_block:,}  ({lookback_days} days)")

        wallet_stats: dict = defaultdict(
            lambda: {"total_size": 0, "trade_count": 0, "markets": set(), "condition_ids": set()}
        )

        total_logs = 0
        for contract_name, contract_addr in [
            ("ConditionalTokens", CONDITIONAL_TOKENS),
            ("NegRiskAdapter",    NEG_RISK_ADAPTER),
        ]:
            log.info(f"Scanning {contract_name} ({contract_addr[:12]}...)...")
            n = await scan_contract(
                session, api_key, contract_addr,
                from_block, current_block,
                target_tokens, wallet_stats, token_map_int,
            )
            log.info(f"  → {n:,} total logs processed")
            total_logs += n
            await asyncio.sleep(0.5)

    # ── Step 4: Rank and output ───────────────────────────────────────────────
    if not wallet_stats:
        log.error("No whale wallets found. Try a longer lookback or check your API key.")
        return

    # Filter by min_size (tokens are in 1e6 precision on Polygon — divide by 1e6 for USDC)
    ranked = sorted(
        [
            (w, s) for w, s in wallet_stats.items()
            if s["total_size"] / 1e6 >= min_size
        ],
        key=lambda x: x[1]["total_size"],
        reverse=True,
    )

    print("\n" + "=" * 72)
    print(f"TOP {top_n} WHALE WALLETS  (lookback={lookback_days}d  min_size=${min_size:,})")
    print("=" * 72)
    for i, (wallet, stats) in enumerate(ranked[:top_n], 1):
        size_usd = stats["total_size"] / 1e6
        print(f"  {i:2d}. {wallet}  ${size_usd:>10,.0f}  trades={stats['trade_count']}")
        for m in sorted(stats["markets"])[:3]:
            print(f"      → {m}")

    print(f"\n# {total_logs:,} logs scanned across {len(wallet_stats)} unique wallets")
    print(f"\n# Paste into config.py → TRACKED_WALLETS:")
    print("TRACKED_WALLETS: list[str] = [")
    for wallet, stats in ranked[:top_n]:
        size_usd = stats["total_size"] / 1e6
        n_markets = len(stats["markets"])
        print(f'    "{wallet}",  # ${size_usd:,.0f}  {n_markets}m')
    print("]")

    # Save full results
    output = [
        {
            "wallet":       w,
            "total_usd":    s["total_size"] / 1e6,
            "trade_count":  s["trade_count"],
            "markets":      list(s["markets"]),
            "condition_ids": list(s["condition_ids"]),
        }
        for w, s in ranked
    ]
    with open("blockchain_whale_scan.json", "w") as f:
        json.dump(output, f, indent=2)
    log.info(f"Full results → blockchain_whale_scan.json  ({len(output)} wallets)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",     type=int,   default=60,  help="Lookback days (default 60)")
    parser.add_argument("--top",      type=int,   default=20,  help="Top N wallets to show (default 20)")
    parser.add_argument("--min-size", type=int,   default=100, help="Min USD size to include (default 100)")
    parser.add_argument("--api-key",  type=str,   default=None, help="Polygonscan API key (or set POLYGONSCAN_API_KEY env var)")
    args = parser.parse_args()

    api_key = args.api_key or os.getenv("POLYGONSCAN_API_KEY")
    if not api_key:
        print("ERROR: Provide API key via --api-key or POLYGONSCAN_API_KEY env var")
        print("Get a free key at: https://polygonscan.com/register")
        exit(1)

    asyncio.run(main(api_key, args.days, args.top, args.min_size))
