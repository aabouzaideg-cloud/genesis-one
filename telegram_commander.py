"""
TelegramCommander — inbound command handler for GENESIS-ONE.

Polls for Telegram updates and responds to slash commands.
Runs as a background asyncio task inside CoreEngine.

Commands:
  /status    — full snapshot: mode, signals, positions, P&L, circuit breaker
  /positions — open positions detail only
  /signals   — today's signal breakdown
  /help      — list commands
"""

import asyncio
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRADING_MODE, DAILY_LOSS_LIMIT

log = logging.getLogger("TelegramCommander")

_BASE = "https://api.telegram.org/bot{token}/{method}"


class TelegramCommander:
    def __init__(self, capital_client, risk_manager, get_trade_log, start_time: float):
        """
        capital_client  — CapitalClient instance (for balance + prices)
        risk_manager    — RiskManager instance (for positions + circuit state)
        get_trade_log   — callable returning list[dict] of all logged signals
        start_time      — time.time() when the bot started
        """
        self._capital      = capital_client
        self._risk         = risk_manager
        self._get_log      = get_trade_log
        self._start_time   = start_time
        self._session: Optional[aiohttp.ClientSession] = None
        self._offset       = 0
        self._running      = False

    async def run(self):
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.info("[COMMANDER] Telegram not configured — command listener disabled.")
            return

        self._running = True
        self._session = aiohttp.ClientSession()
        log.info("[COMMANDER] Telegram command listener active.")
        try:
            while self._running:
                await self._poll()
        finally:
            await self._session.close()

    def stop(self):
        self._running = False

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _poll(self):
        url = _BASE.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
        params = {"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]}
        try:
            async with self._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                if resp.status != 200:
                    await asyncio.sleep(5)
                    return
                data = await resp.json()
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    await self._handle(update)
        except asyncio.TimeoutError:
            pass   # long-poll timeout is normal — just loop again
        except Exception as exc:
            log.warning(f"[COMMANDER] Poll error: {exc}")
            await asyncio.sleep(5)

    async def _handle(self, update: dict):
        msg  = update.get("message", {})
        text = (msg.get("text") or "").strip()
        chat = str(msg.get("chat", {}).get("id", ""))

        # Only respond to the configured chat
        if chat != str(TELEGRAM_CHAT_ID):
            return

        if text.startswith("/status"):
            reply = await self._status()
        elif text.startswith("/positions"):
            reply = await self._positions()
        elif text.startswith("/signals"):
            reply = self._signals()
        elif text.startswith("/help"):
            reply = self._help()
        else:
            return   # ignore non-commands

        await self._send(reply)

    # ── Commands ──────────────────────────────────────────────────────────────

    async def _status(self) -> str:
        uptime   = self._uptime()
        signals  = self._signals_summary()
        pos_text = await self._positions_summary()
        cb_text  = self._circuit_text()
        pnl_text = await self._pnl_text()

        return (
            f"*GENESIS-ONE STATUS*\n"
            f"Mode: `{TRADING_MODE}` | Uptime: `{uptime}`\n"
            f"{'─' * 30}\n"
            f"{signals}\n"
            f"{'─' * 30}\n"
            f"{pos_text}\n"
            f"{'─' * 30}\n"
            f"{pnl_text}\n"
            f"{cb_text}"
        )

    async def _positions(self) -> str:
        return await self._positions_summary(detailed=True)

    def _signals(self) -> str:
        return self._signals_summary(detailed=True)

    def _help(self) -> str:
        return (
            "*GENESIS-ONE Commands*\n"
            "/status — full snapshot\n"
            "/positions — open positions\n"
            "/signals — today's signal breakdown\n"
            "/help — this message"
        )

    # ── Data Helpers ──────────────────────────────────────────────────────────

    def _uptime(self) -> str:
        secs = int(time.time() - self._start_time)
        h, m = divmod(secs // 60, 60)
        return f"{h}h {m}m" if h else f"{m}m"

    def _signals_summary(self, detailed: bool = False) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log   = [r for r in self._get_log() if r.get("timestamp", "").startswith(today)]

        total    = len(log)
        approved = sum(1 for r in log if r.get("verdict") == "APPROVED")
        blocked  = total - approved

        counts: dict[str, int] = {}
        for r in log:
            v = r.get("verdict", "UNKNOWN")
            if v != "APPROVED":
                counts[v] = counts.get(v, 0) + 1

        lines = [f"*Today's Signals:* {total}"]
        lines.append(f"  ✅ APPROVED: {approved}")
        for verdict, n in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"  ❌ {verdict}: {n}")

        if detailed and log:
            last = log[-1]
            age  = ""
            try:
                ts  = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                ago = int((datetime.now(timezone.utc) - ts).total_seconds() / 60)
                age = f" ({ago}m ago)"
            except Exception:
                pass
            lines.append(f"\n*Last:* `{last.get('verdict')}` — {last.get('epic', 'N/A')}{age}")
            lines.append(f"_{self._md_safe(last.get('reason', '')[:120])}_")

        return "\n".join(lines)

    async def _positions_summary(self, detailed: bool = False) -> str:
        # Pull live positions directly from Capital.com (includes manually opened ones)
        try:
            live = await self._capital.get_open_positions()
        except Exception:
            live = []

        if not live:
            return "*Open Positions:* None"

        lines = [f"*Open Positions:* {len(live)}"]
        for p in live:
            pos = p.get("position", {})
            mkt = p.get("market", {})
            epic      = mkt.get("epic", "?")
            direction = pos.get("direction", "?")
            entry     = pos.get("openLevel")
            size      = pos.get("size", "?")
            upl       = pos.get("upl", 0) or 0
            sl        = pos.get("stopLevel", "—")
            tp        = pos.get("limitLevel", "—")
            deal_id   = pos.get("dealId", "")

            try:
                price   = await self._capital.get_price(epic)
                mid     = price["mid"]
                if entry:
                    pnl_pct  = ((mid - entry) / entry * 100 if direction == "BUY"
                                else (entry - mid) / entry * 100)
                    pnl_sign = "+" if pnl_pct >= 0 else ""
                    pnl_str  = f"{pnl_sign}{pnl_pct:.2f}%"
                else:
                    pnl_str = "?"
                upl_sign = "+" if upl >= 0 else ""
                lines.append(
                    f"  `{epic}` {direction} x{size} | P&L: `{upl_sign}${upl:.2f}` ({pnl_str})"
                )
            except Exception:
                upl_sign = "+" if upl >= 0 else ""
                lines.append(f"  `{epic}` {direction} x{size} | P&L: `{upl_sign}${upl:.2f}`")

            if detailed:
                lines.append(f"    SL: {sl} | TP: {tp} | entry: {entry or '?'}")

        return "\n".join(lines)

    async def _pnl_text(self) -> str:
        try:
            account  = await self._capital.get_account_info()
            balance  = account["accounts"][0]["balance"]["balance"]
            start    = self._risk._day_start_balance or balance
            pnl      = balance - start
            pnl_pct  = (pnl / start * 100) if start > 0 else 0
            sign     = "+" if pnl >= 0 else ""
            return (
                f"*Balance:* `${balance:,.2f}`\n"
                f"*Daily P&L:* `{sign}${pnl:,.2f}` ({sign}{pnl_pct:.2f}%)"
            )
        except Exception:
            return "*Balance:* unavailable"

    def _circuit_text(self) -> str:
        if self._risk._circuit_open:
            return "*Circuit Breaker:* 🔴 TRIPPED — trading halted"
        limit_pct = DAILY_LOSS_LIMIT * 100
        return f"*Circuit Breaker:* ✅ OK (limit: {limit_pct:.0f}%/day)"

    # ── Send ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _md_safe(text: str) -> str:
        return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")

    async def _send(self, text: str):
        url     = _BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        try:
            async with self._session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(f"[COMMANDER] Send failed {resp.status}: {body[:100]}")
        except Exception as exc:
            log.warning(f"[COMMANDER] Send error: {exc}")
