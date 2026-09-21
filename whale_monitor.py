"""
Polymarket Whale Monitor — hybrid REST polling + WebSocket price feed.

Architecture (based on verified Polymarket API docs):
  - Whale tracking:  REST polling via data-api.polymarket.com/trades every 30s
                     (User WS channel requires auth and only shows YOUR trades)
  - Price feed:      WebSocket market channel for real-time Capital.com-equivalent
                     price reference on our tracked markets
  - Deduplication:   by transactionHash so no signal fires twice

Direction logic (only BUY trades emitted — SELL = exit/profit-taking, ambiguous):
  side=BUY + outcome=YES  →  WhaleSignal direction="YES" (bullish)
  side=BUY + outcome=NO   →  WhaleSignal direction="NO"  (bearish)
  side=SELL               →  dropped
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import aiohttp
import websockets
from aiohttp_socks import ProxyConnector
from python_socks.async_.asyncio import Proxy as SocksProxy

TOR_PROXY = "socks5://127.0.0.1:9050"
POTENTIAL_MARKETS_LOG = Path(__file__).parent / "potential_markets.log"
from config import (
    POLYMARKET_WS_MARKET_URL, POLYMARKET_CLOB_URL, POLYMARKET_DATA_URL,
    WHALE_POLL_INTERVAL, TRACKED_WALLETS,
)
from market_mapper import MarketMapper

log = logging.getLogger("WhaleMonitor")

# Signals older than this are stale — FOMO gate will reject them anyway
MAX_SIGNAL_AGE_SECONDS = 300   # 5 minutes
WS_PING_INTERVAL       = 10    # Polymarket market WS requires PING every 10s

# ── Multi-wallet Consensus ────────────────────────────────────────────────────
CONSENSUS_WINDOW    = 600   # 10 minutes — max gap between two whales betting same market
CONSENSUS_THRESHOLD = 2     # need this many independent wallets to fire

# Elite wallets (93%+ WR) — single trade enough, no consensus required
ELITE_WALLETS: frozenset[str] = frozenset({
    "0x5d0f03cf1243a3e21262d6cf844795afd9fff0ad",   # EB99999   94% WR
    "0x9368eb0e0e2ff384095a88960c5a494fed1580a3",   # ifpl      93% WR
    "0xae89da6594bb0d421c8d807d74e213f8729e90b8",   # Ume-Chang 89% WR
})


@dataclass
class WhaleSignal:
    wallet:            str
    market_id:         str           # conditionId
    question:          str
    direction:         str           # "YES" or "NO"
    poly_price:        float         # Polymarket trade price (0–1 probability)
    epic:              Optional[str]
    confidence:        float
    map_method:        str
    trade_age_s:       float         # seconds since trade was placed
    timestamp:         float = field(default_factory=time.time)
    consensus_count:   int   = 1     # how many independent wallets agree
    consensus_wallets: list  = field(default_factory=list)  # all wallets in consensus


class WhaleMonitor:
    def __init__(self):
        self._signals: asyncio.Queue[WhaleSignal] = asyncio.Queue()
        self._mapper  = MarketMapper()
        self._running = False
        self._seen_tx: set[str] = set()                   # deduplicate by transactionHash
        self._token_ids: list[str] = []                   # for WS market subscription
        self._session: Optional[aiohttp.ClientSession] = None
        # consensus_key = f"{condition_id}:{direction}"
        # value = {"wallets": [...], "first_seen": float, "signal": WhaleSignal}
        self._consensus: dict[str, dict] = {}
        # Rolling 24h buffer of fired signals — used by MarketScanner Polymarket check
        self._signal_history: list[dict] = []

    @property
    def signals(self) -> asyncio.Queue[WhaleSignal]:
        return self._signals

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._session = aiohttp.ClientSession(connector=ProxyConnector.from_url(TOR_PROXY))
        try:
            await self._load_token_ids()
            await asyncio.gather(
                self._poll_whale_trades(),
                self._stream_market_prices(),
            )
        finally:
            await self._session.close()

    def stop(self):
        self._running = False

    # ── Token ID Resolution ───────────────────────────────────────────────────

    async def _load_token_ids(self):
        """
        Fetch YES/NO token IDs for all curated markets from the CLOB REST API.
        Token IDs are required for the market WebSocket subscription.
        """
        from market_mapper import MAP_PATH
        import json as _json
        import os

        try:
            with open(MAP_PATH) as f:
                raw = _json.load(f)
            condition_ids = [
                k for k, v in raw.get("curated", {}).items()
                if isinstance(v, dict)
            ]
        except Exception as exc:
            log.warning(f"Could not read market_map.json for token IDs: {exc}")
            return

        for cid in condition_ids:
            try:
                url = f"{POLYMARKET_CLOB_URL}/markets/{cid}"
                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status != 200:
                        continue
                    data  = await resp.json()
                    tokens = data.get("tokens", [])
                    for t in tokens:
                        tid = t.get("token_id")
                        if tid and str(tid) not in self._token_ids:
                            self._token_ids.append(str(tid))
            except Exception as exc:
                log.debug(f"Token ID fetch failed for {cid[:12]}...: {exc}")

        log.info(f"Loaded {len(self._token_ids)} token IDs for market WS subscription.")

    # ── REST Polling — Whale Trade Detection ─────────────────────────────────

    async def _poll_whale_trades(self):
        """
        Poll data-api.polymarket.com/trades for each tracked wallet every 30s.
        Emits a WhaleSignal for every new BUY trade not previously seen.
        """
        if not TRACKED_WALLETS:
            log.warning(
                "[WHALE] TRACKED_WALLETS is empty — no whale signals will fire. "
                "Add wallet addresses to config.py."
            )

        while self._running:
            for wallet in TRACKED_WALLETS:
                try:
                    await self._fetch_wallet_trades(wallet)
                except Exception as exc:
                    log.warning(f"[WHALE] Poll failed for {wallet[:10]}...: {exc}")
            await asyncio.sleep(WHALE_POLL_INTERVAL)

    async def _fetch_wallet_trades(self, wallet: str):
        url    = f"{POLYMARKET_DATA_URL}/trades"
        params = {"maker_address": wallet, "limit": 10}

        for attempt in range(2):
            try:
                async with self._session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status != 200:
                        log.debug(f"Trade API returned {resp.status} for {wallet[:10]}...")
                        return
                    trades = await resp.json()
                break
            except Exception as exc:
                if attempt == 0:
                    log.debug(f"Tor request failed ({exc}) — retrying in 1s")
                    await asyncio.sleep(1)
                else:
                    raise

        if not isinstance(trades, list):
            return

        for trade in trades:
            self._process_trade(wallet, trade)

    def _process_trade(self, wallet: str, trade: dict):
        tx_hash = trade.get("transactionHash", "")
        if not tx_hash or tx_hash in self._seen_tx:
            return
        self._seen_tx.add(tx_hash)

        # Only act on BUY trades — SELL = exit, not an entry signal
        side = (trade.get("side") or "").upper()
        if side != "BUY":
            return

        outcome      = (trade.get("outcome") or "").upper()   # "YES" or "NO"
        condition_id = trade.get("conditionId", "")
        question     = trade.get("title") or trade.get("slug") or ""
        poly_price   = float(trade.get("price", 0))
        timestamp_s = float(trade.get("timestamp", time.time()))
        trade_age   = time.time() - timestamp_s

        if outcome not in ("YES", "NO"):
            return

        if trade_age > MAX_SIGNAL_AGE_SECONDS:
            log.debug(
                f"[WHALE] Stale trade ignored ({trade_age:.0f}s old) "
                f"for {wallet[:10]}..."
            )
            return

        # Minimum conviction filter — ignore trades under $10k USD
        trade_size_usd = float(trade.get("size", 0)) * poly_price * 100
        if trade_size_usd < 10_000:
            log.debug(
                f"[WHALE] Sub-$10k trade ignored (${trade_size_usd:.0f}) "
                f"for {wallet[:10]}..."
            )
            return

        mapping = self._mapper.resolve(condition_id, question)
        if not self._mapper.is_tradeable(mapping):
            # Shadow discovery: log unmatched markets for manual review
            if mapping.method == "no_match" and "binary" not in mapping.reason.lower():
                self._log_potential_market(condition_id, question, wallet, poly_price)
            log.info(
                f"[WHALE] Dropped — {mapping.reason} "
                f"(wallet={wallet[:10]}... outcome={outcome})"
            )
            return

        # ── Consensus logic ───────────────────────────────────────────────────
        is_elite   = wallet.lower() in ELITE_WALLETS
        is_curated = mapping.method == "curated"

        if is_elite or is_curated:
            # Elite wallets and curated markets fire immediately — no consensus required.
            # Curated markets are hand-picked high-confidence mappings — one whale is enough.
            signal = WhaleSignal(
                wallet=wallet,
                market_id=condition_id,
                question=question,
                direction=outcome,
                poly_price=poly_price,
                epic=mapping.epic,
                confidence=mapping.confidence,
                map_method=mapping.method,
                trade_age_s=trade_age,
                consensus_count=1,
                consensus_wallets=[wallet],
            )
            self._signals.put_nowait(signal)
            self._record_signal_history(mapping.epic, outcome)
            tag = "ELITE" if is_elite else "CURATED"
            log.info(
                f"[WHALE] {tag} {wallet[:10]}... BUY {outcome} on '{question[:50]}' "
                f"| Epic={mapping.epic} conf={mapping.confidence:.0%} "
                f"via {mapping.method} | poly_price={poly_price:.3f} age={trade_age:.0f}s"
            )
            return

        # Standard wallet on keyword-matched market — add to consensus tracker
        ckey = f"{condition_id}:{outcome}"
        now  = time.time()

        # Prune stale consensus entries
        stale = [k for k, v in self._consensus.items()
                 if now - v["first_seen"] > CONSENSUS_WINDOW]
        for k in stale:
            del self._consensus[k]

        if ckey not in self._consensus:
            self._consensus[ckey] = {
                "wallets":    [wallet],
                "first_seen": now,
                "signal": WhaleSignal(
                    wallet=wallet,
                    market_id=condition_id,
                    question=question,
                    direction=outcome,
                    poly_price=poly_price,
                    epic=mapping.epic,
                    confidence=mapping.confidence,
                    map_method=mapping.method,
                    trade_age_s=trade_age,
                    consensus_count=1,
                    consensus_wallets=[wallet],
                ),
            }
            log.info(
                f"[CONSENSUS] First signal from {wallet[:10]}... — waiting for {CONSENSUS_THRESHOLD} "
                f"wallets on '{question[:50]}' ({outcome})"
            )
            return

        entry = self._consensus[ckey]

        # Deduplicate: same wallet betting again doesn't count
        if wallet in entry["wallets"]:
            return

        entry["wallets"].append(wallet)
        entry["signal"].consensus_count   = len(entry["wallets"])
        entry["signal"].consensus_wallets = list(entry["wallets"])
        # Update age to reflect the most recent confirming trade
        entry["signal"].trade_age_s = trade_age

        log.info(
            f"[CONSENSUS] +1 from {wallet[:10]}... — now {len(entry['wallets'])} wallets "
            f"on '{question[:50]}' ({outcome}) | threshold={CONSENSUS_THRESHOLD}"
        )

        if len(entry["wallets"]) >= CONSENSUS_THRESHOLD:
            # Threshold met — fire the signal
            signal = entry["signal"]
            del self._consensus[ckey]
            self._signals.put_nowait(signal)
            self._record_signal_history(signal.epic, signal.direction)
            log.info(
                f"[WHALE] CONSENSUS HIT ({signal.consensus_count} wallets) BUY {outcome} "
                f"on '{question[:50]}' | Epic={mapping.epic} "
                f"poly_price={poly_price:.3f} age={trade_age:.0f}s"
            )

    # ── WebSocket — Market Price Feed ─────────────────────────────────────────

    async def _stream_market_prices(self):
        """
        Connect to the Polymarket market WebSocket and maintain a real-time
        price cache. Primarily used for latency monitoring and future enhancements.
        Reconnects automatically on disconnect.
        """
        while self._running:
            try:
                await self._ws_connect()
            except Exception as exc:
                log.warning(f"[WS] Market stream error: {exc} — reconnecting in 10s")
                await asyncio.sleep(10)

    async def _ws_connect(self):
        if not self._token_ids:
            log.info("[WS] No token IDs available — skipping market price stream.")
            await asyncio.sleep(60)
            return

        # Route WebSocket through Tor SOCKS5 proxy (UAE block workaround)
        from urllib.parse import urlparse
        _parsed = urlparse(POLYMARKET_WS_MARKET_URL)
        _host   = _parsed.hostname
        _port   = _parsed.port or (443 if _parsed.scheme == "wss" else 80)
        _proxy  = SocksProxy.from_url(TOR_PROXY)
        _sock   = await _proxy.connect(dest_host=_host, dest_port=_port)

        async with websockets.connect(
            POLYMARKET_WS_MARKET_URL,
            sock=_sock,
            ping_interval=None,   # we handle PING manually per Polymarket docs
        ) as ws:
            log.info(f"[WS] Connected to Polymarket market channel.")

            # Subscribe to all tracked market token IDs
            await ws.send(json.dumps({
                "type":       "market",
                "assets_ids": self._token_ids,
            }))

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if not self._running:
                        break
                    if raw == "PONG":
                        continue
                    try:
                        self._handle_ws_message(json.loads(raw))
                    except Exception as exc:
                        log.debug(f"[WS] Message parse error: {exc}")
            finally:
                ping_task.cancel()

    async def _ping_loop(self, ws):
        """Send PING every 10s as required by Polymarket market WS docs."""
        while True:
            await asyncio.sleep(WS_PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                break

    def _handle_ws_message(self, msg: dict):
        event = msg.get("event_type", "")

        # last_trade_price — a trade just executed on this market
        if event == "last_trade_price":
            asset_id = msg.get("asset_id", "")
            price    = float(msg.get("price", 0))
            size     = float(msg.get("size", 0))
            side     = msg.get("side", "")
            log.debug(f"[WS] Trade: {side} {size} @ {price} (token={asset_id[:12]}...)")

    def _log_potential_market(self, condition_id: str, question: str, wallet: str, price: float):
        """Log unmapped BUY trades to potential_markets.log for manual review."""
        try:
            entry = (
                f"{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} | "
                f"conditionId={condition_id} | price={price:.3f} | "
                f"wallet={wallet[:10]}... | question={question[:80]}\n"
            )
            with open(POTENTIAL_MARKETS_LOG, "a") as f:
                f.write(entry)
            log.info(f"[SHADOW] Unmatched market logged → potential_markets.log: {question[:60]}")
        except Exception as exc:
            log.debug(f"Could not write potential_markets.log: {exc}")

    def reload_map(self):
        self._mapper.reload()
        log.info("Market map reloaded.")

    def _record_signal_history(self, epic: str, outcome: str):
        """Append a fired signal to the rolling 24h history buffer."""
        cutoff = time.time() - 86400
        self._signal_history = [s for s in self._signal_history if s["timestamp"] > cutoff]
        self._signal_history.append({
            "epic":      epic,
            "outcome":   outcome,   # "YES" or "NO" (raw Polymarket outcome)
            "timestamp": time.time(),
        })

    def get_recent_activity(self, epic: str, lookback_hours: float = 4.0) -> list[dict]:
        """
        Return recent whale signals for this epic within the lookback window.
        Used by MarketScanner's Polymarket confirmation check.
        """
        cutoff = time.time() - lookback_hours * 3600
        return [
            s for s in self._signal_history
            if s["epic"] == epic and s["timestamp"] > cutoff
        ]
