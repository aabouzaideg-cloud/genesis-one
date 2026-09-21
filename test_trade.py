"""
One-shot test trade — OIL_CRUDE BUY
Thesis: Iran escalation consensus across whale wallets (9/15 YES on regime fall / Hormuz)
Runs full Capital.com flow: connect → OHLCV → risk calc → place order
"""
import asyncio
import logging
from capital_client import CapitalClient
from risk_manager import RiskManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("TestTrade")

EPIC      = "OIL_CRUDE"
DIRECTION = "BUY"

async def main():
    capital = CapitalClient()
    risk    = RiskManager(capital)

    await capital.connect()
    log.info(f"Connected. Placing test {DIRECTION} on {EPIC}...")

    df = await capital.get_ohlcv(EPIC)
    log.info(f"OHLCV fetched: {len(df)} bars, last close={df['close'].iloc[-1]:.3f}")

    params = await risk.calculate(EPIC, DIRECTION, df)
    if params is None:
        log.error("Risk manager blocked the trade. Check margin / account balance.")
        await capital.close()
        return

    log.info(
        f"Risk params: entry={params.entry:.3f}  SL={params.stop_loss:.3f}  "
        f"TP={params.take_profit:.3f}  size={params.size}"
    )

    result = await capital.place_order(
        epic=EPIC,
        direction=DIRECTION,
        size=params.size,
        stop_level=params.stop_loss,
        profit_level=params.take_profit,
    )

    deal_id = result.get("dealReference") or result.get("dealId", "unknown")
    log.info(f"Order placed. DealID={deal_id}")
    log.info(f"Full response: {result}")

    await capital.close()

if __name__ == "__main__":
    asyncio.run(main())
