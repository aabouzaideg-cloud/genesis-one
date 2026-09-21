"""
notify_bot.py — Telegram notification for high-confidence trade events.

Setup:
  1. Create a bot via @BotFather on Telegram → get BOT_TOKEN
  2. Send any message to your bot, then:
     curl https://api.telegram.org/bot<TOKEN>/getUpdates
     → copy the chat_id from the response
  3. Add to .env:
     TELEGRAM_BOT_TOKEN=your_token
     TELEGRAM_CHAT_ID=your_chat_id
"""

import logging
import aiohttp
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger("NotifyBot")

_TELEGRAM_API  = "https://api.telegram.org/bot{token}/sendMessage"
_TOR_PROXY     = "socks5://127.0.0.1:9050"


def _md_safe(text: str) -> str:
    """Escape characters that break Telegram Markdown v1 in dynamic strings."""
    return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`").replace("[", "\\[")


async def send_message(text: str) -> bool:
    """
    Send a plain-text message to the configured Telegram chat.
    Returns True on success, False on failure (non-fatal — never raises).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured — skipping notification.")
        return False

    url     = _TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}

    # Try via Tor SOCKS5 first, fall back to direct connection
    for attempt, use_tor in enumerate([True, False]):
        try:
            if use_tor:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(_TOR_PROXY)
            else:
                connector = None
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return True
                    body = await resp.text()
                    log.warning(f"Telegram API error {resp.status}: {body[:100]}")
                    return False
        except Exception as exc:
            if attempt == 0:
                log.debug(f"Telegram via Tor failed, trying direct: {exc}")
            else:
                log.warning(f"Telegram notification failed: {exc}")
    return False


def format_approved(verdict: str, epic: str, direction: str, entry: float,
                    sl: float, tp: float, size: float, deal_id: str,
                    confidence: str, reason: str) -> str:
    rr_sl   = abs(entry - sl)
    rr_tp   = abs(entry - tp)
    rr_str  = f"{rr_tp/rr_sl:.1f}" if rr_sl > 0 else "3.0"
    return (
        f"*TRADE EXECUTED — OIL\\_CRUDE*\n"
        f"`{direction}` | Entry: `{entry:.3f}` | Conf: `{confidence}`\n"
        f"SL: `{sl:.3f}` | TP: `{tp:.3f}` | R:R `1:{rr_str}`\n"
        f"Size: `{size}` lots | Deal: `{deal_id}`\n\n"
        f"_{_md_safe(reason[:220])}_"
    )


def format_blocked(epic: str, verdict: str, reason: str, whale: str = "") -> str:
    gate = verdict.replace("BLOCKED_", "")
    return (
        f"*BLOCKED — {gate}*\n"
        f"`{epic}` | _{_md_safe(reason[:220])}_"
    )
