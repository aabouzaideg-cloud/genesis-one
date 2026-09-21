"""
Capital.com REST API client.
Handles auth (session tokens), OHLCV fetching, order placement, and account info.

Session management:
  - Tokens expire after ~10 minutes of inactivity
  - A background keep-alive task pings the session every 9 minutes
  - Any 401 triggers immediate re-authentication before retrying
"""

import asyncio
import logging
import aiohttp
import pandas as pd
import pandas_ta as ta
from typing import Optional
from config import (
    CAPITAL_BASE_URL, CAPITAL_API_KEY, CAPITAL_PASSWORD, CAPITAL_IDENTIFIER,
    OHLCV_INTERVAL, OHLCV_BARS, EMA_PERIOD, ATR_PERIOD,
)

log = logging.getLogger("CapitalClient")

SESSION_REFRESH_INTERVAL = 540   # 9 minutes (tokens last ~10min)
MAX_AUTH_RETRIES         = 3


class CapitalClient:
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cst: Optional[str] = None
        self._x_security: Optional[str] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._auth_lock = asyncio.Lock()

    async def connect(self):
        self._session = aiohttp.ClientSession()
        await self._authenticate()
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        log.info("CapitalClient connected with session keep-alive running.")

    async def close(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._session:
            await self._logout()
            await self._session.close()

    # ── Auth ──────────────────────────────────────────────────────────────────

    async def _authenticate(self):
        async with self._auth_lock:
            url = f"{CAPITAL_BASE_URL}/api/v1/session"
            payload = {
                "identifier":        CAPITAL_IDENTIFIER,
                "password":          CAPITAL_PASSWORD,
                "encryptedPassword": False,
            }
            headers = {"X-CAP-API-KEY": CAPITAL_API_KEY, "Content-Type": "application/json"}

            for attempt in range(1, MAX_AUTH_RETRIES + 1):
                try:
                    async with self._session.post(url, json=payload, headers=headers) as resp:
                        resp.raise_for_status()
                        self._cst        = resp.headers["CST"]
                        self._x_security = resp.headers["X-SECURITY-TOKEN"]
                        log.info(f"Authenticated with Capital.com (attempt {attempt})")
                        return
                except Exception as exc:
                    log.warning(f"Auth attempt {attempt}/{MAX_AUTH_RETRIES} failed: {exc}")
                    if attempt < MAX_AUTH_RETRIES:
                        await asyncio.sleep(2 ** attempt)

            raise RuntimeError("Capital.com authentication failed after max retries.")

    async def _logout(self):
        try:
            url = f"{CAPITAL_BASE_URL}/api/v1/session"
            async with self._session.delete(url, headers=self._auth_headers()) as resp:
                log.info("Session closed with Capital.com.")
        except Exception:
            pass

    async def _keepalive_loop(self):
        """Ping the session endpoint every 9 minutes to prevent token expiry."""
        while True:
            await asyncio.sleep(SESSION_REFRESH_INTERVAL)
            try:
                url = f"{CAPITAL_BASE_URL}/api/v1/ping"
                async with self._session.get(url, headers=self._auth_headers()) as resp:
                    if resp.status == 200:
                        log.debug("Session keep-alive ping OK.")
                    elif resp.status == 401:
                        log.warning("Session expired — re-authenticating...")
                        await self._authenticate()
            except Exception as exc:
                log.warning(f"Keep-alive ping failed: {exc} — re-authenticating...")
                try:
                    await self._authenticate()
                except Exception as auth_exc:
                    log.error(f"Re-authentication failed: {auth_exc}")

    def _auth_headers(self) -> dict:
        return {
            "X-CAP-API-KEY":    CAPITAL_API_KEY,
            "CST":              self._cst,
            "X-SECURITY-TOKEN": self._x_security,
            "Content-Type":     "application/json",
        }

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        """
        Wrapper for all API calls. Handles 401 by re-authenticating once and retrying.
        """
        async with self._session.request(method, url, headers=self._auth_headers(), **kwargs) as resp:
            if resp.status == 401:
                log.warning("401 received — re-authenticating and retrying...")
                await self._authenticate()
                async with self._session.request(method, url, headers=self._auth_headers(), **kwargs) as retry_resp:
                    retry_resp.raise_for_status()
                    return await retry_resp.json()
            resp.raise_for_status()
            return await resp.json()

    # ── Market Data ───────────────────────────────────────────────────────────

    async def get_ohlcv(self, epic: str) -> pd.DataFrame:
        url    = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        params = {"resolution": OHLCV_INTERVAL, "max": OHLCV_BARS}
        data   = await self._request("GET", url, params=params)

        def _mid(px): return (px["bid"] + px["ask"]) / 2

        prices = data.get("prices", [])
        df = pd.DataFrame([{
            "time":  p["snapshotTimeUTC"],
            "open":  _mid(p["openPrice"]),
            "high":  _mid(p["highPrice"]),
            "low":   _mid(p["lowPrice"]),
            "close": _mid(p["closePrice"]),
        } for p in prices])

        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        df = df.astype(float)
        df.ta.ema(length=EMA_PERIOD, append=True)
        df.ta.atr(length=ATR_PERIOD, append=True)
        return df

    async def get_ohlcv_h4(self, epic: str) -> pd.DataFrame:
        """Fetch 210 bars at 4-hour resolution for macro trend analysis (EMA 200)."""
        url    = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        params = {"resolution": "HOUR_4", "max": 210}
        data   = await self._request("GET", url, params=params)

        def _mid(px): return (px["bid"] + px["ask"]) / 2

        prices = data.get("prices", [])
        df = pd.DataFrame([{
            "time":  p["snapshotTimeUTC"],
            "open":  _mid(p["openPrice"]),
            "high":  _mid(p["highPrice"]),
            "low":   _mid(p["lowPrice"]),
            "close": _mid(p["closePrice"]),
        } for p in prices])

        df["time"] = pd.to_datetime(df["time"])
        df.set_index("time", inplace=True)
        df = df.astype(float)
        df.ta.ema(length=200, append=True)
        return df

    async def get_price(self, epic: str) -> dict:
        url    = f"{CAPITAL_BASE_URL}/api/v1/prices/{epic}"
        params = {"resolution": "MINUTE", "max": 1}
        data   = await self._request("GET", url, params=params)

        latest = data["prices"][-1]
        bid = latest["closePrice"]["bid"]
        ask = latest["closePrice"]["ask"]
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

    # ── Account ───────────────────────────────────────────────────────────────

    async def get_account_info(self) -> dict:
        url = f"{CAPITAL_BASE_URL}/api/v1/accounts"
        return await self._request("GET", url)

    async def get_margin_percent(self) -> float:
        info     = await self.get_account_info()
        accounts = info.get("accounts", [])
        if not accounts:
            return 0.0
        balance = accounts[0].get("balance", {})
        equity  = balance.get("equity", 1)
        used    = balance.get("margin", 0)
        return (equity / used * 100) if used > 0 else 9999.0

    async def get_open_positions(self) -> list[dict]:
        url  = f"{CAPITAL_BASE_URL}/api/v1/positions"
        data = await self._request("GET", url)
        return data.get("positions", [])

    # ── Order Execution ───────────────────────────────────────────────────────

    async def place_order(
        self,
        epic: str,
        direction: str,
        size: float,
        stop_level: float,
        profit_level: float,
    ) -> dict:
        url     = f"{CAPITAL_BASE_URL}/api/v1/positions"
        payload = {
            "epic":           epic,
            "direction":      direction,
            "size":           size,
            "stopLevel":      stop_level,
            "profitLevel":    profit_level,
            "guaranteedStop": False,
        }
        result = await self._request("POST", url, json=payload)
        log.info(f"Order placed: {direction} {size} {epic} | SL={stop_level} TP={profit_level}")
        return result

    async def close_position(self, deal_id: str, size: float, direction: str) -> dict:
        url     = f"{CAPITAL_BASE_URL}/api/v1/positions/{deal_id}"
        payload = {"direction": direction, "size": size}
        return await self._request("DELETE", url, json=payload)

    async def update_stop(self, deal_id: str, new_stop: float) -> dict:
        url     = f"{CAPITAL_BASE_URL}/api/v1/positions/{deal_id}"
        payload = {"stopLevel": new_stop}
        return await self._request("PUT", url, json=payload)
