import asyncio, json, aiohttp
from aiohttp_socks import ProxyConnector

TARGET_CIDS = {
    "0x9352c559e9648ab4cab236087b64ca85c5b7123a4c7d9d7d4efde4a39c18056f": "Iran-regime",
    "0x5db999fad322cea2914535aae5517060c3f80ad6d8c0231cde2124a434d16846": "US-invade-Iran",
    "0xa0f4c4924ea1a8b410b4ce821c2a9955fad21a1b19bdcfde90816732278b3dd5": "BTC-150k",
    "0xa1d97efb8a19de58d995edf58b882c4f99ef356c8a564af8daf888a7610edea1": "Kharg-May31",
    "0x6897736d782ce70f47126dfcec6669073f563d6e757e60bc61c0367370d6f73e": "Kharg-June30",
    "0xa70fc3695a65833b91b45df6db6015096f3e1471b70352ca411b4209010e7633": "US-Iran-nuke",
    "0x924a2942747dd75703321a7c8d809c68f6a514c3b0f2a2e64274e02310634669": "Hormuz",
    "0x02deb9538f5c123373adaa4ee6217b01745f1662bc902e46ac92f3fe6f8741e8": "BTC-150k-Dec",
}

async def fetch_leaderboard_wallets():
    traders = []
    async with aiohttp.ClientSession() as s:
        for page in range(1, 7):
            url = f"https://polysmartwallet.com/api/leaderboard.php?page={page}&limit=15"
            r = await s.get(url, timeout=aiohttp.ClientTimeout(total=10))
            d = await r.json(content_type=None)
            for p in d.get("data", {}).get("portfolio", []):
                traders.append({
                    "address":  p["address"],
                    "userName": p.get("userName", ""),
                    "score":    p.get("score", 0),
                    "pnl":      float(p.get("actualTotalPnl", 0)),
                    "winRate":  float(p.get("winRate", 0)),
                })
    return traders

async def check_wallet(s, wallet):
    params = {"maker_address": wallet, "limit": 500}
    try:
        async with s.get("https://data-api.polymarket.com/trades", params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
            trades = await r.json()
    except Exception:
        return None
    if not isinstance(trades, list):
        return None
    macro = [t for t in trades if (t.get("conditionId") or "") in TARGET_CIDS
             and (t.get("side") or "").upper() == "BUY"]
    if not macro:
        return None
    total = sum(float(t.get("size") or 0) for t in macro)
    markets = list({TARGET_CIDS[t["conditionId"]] for t in macro})
    return {"total_size": total, "trade_count": len(macro), "markets": markets}

async def main():
    print("Fetching top 90 traders from PolySmartWallet...")
    traders = await fetch_leaderboard_wallets()
    print(f"Got {len(traders)} traders. Scanning macro market history via Tor...")

    connector = ProxyConnector.from_url("socks5://127.0.0.1:9050")
    async with aiohttp.ClientSession(connector=connector) as s:
        found = []
        for i in range(0, len(traders), 10):
            batch = traders[i:i+10]
            results = await asyncio.gather(*[check_wallet(s, t["address"]) for t in batch])
            for trader, result in zip(batch, results):
                if result:
                    found.append({**trader, **result})
            print(f"  checked {min(i+10,len(traders))}/{len(traders)}  macro_whales={len(found)}")

    found.sort(key=lambda x: x["total_size"], reverse=True)
    print(f"\n=== {len(found)} traders with macro market activity ===")
    for f in found:
        size = f["total_size"]
        print(f"  {f['address']}  {f['userName']:20s}  score={f['score']}  wr={f['winRate']:.1f}%  size={size:.0f}  {f['markets']}")

    if found:
        print("\n# Paste into config.py:")
        print("TRACKED_WALLETS: list[str] = [")
        for f in found[:15]:
            addr = f["address"]; name = f["userName"]; sc = f["score"]; wr = f["winRate"]
        print(f'    "{addr}",  # {name} score={sc} wr={wr:.0f}%')
        print("]")

asyncio.run(main())
