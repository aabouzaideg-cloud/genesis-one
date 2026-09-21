"""
GENESIS-ONE — PolyPrint Trading Engine
Entry point.

Usage:
    python main.py

Set TRADING_MODE=DEMO in .env for paper trading (default).
Set TRADING_MODE=LIVE only when fully tested.
"""

import asyncio
import signal
import sys
import os
import logging
from core_engine import CoreEngine

log = logging.getLogger("GENESIS-ONE")

_PID_FILE = "/tmp/genesis_one.pid"


def _check_pid_lock():
    if os.path.exists(_PID_FILE):
        try:
            old_pid = int(open(_PID_FILE).read().strip())
            os.kill(old_pid, 0)   # signal 0 = check if process exists
            print(f"GENESIS-ONE already running (PID {old_pid}). Exiting.")
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass   # stale PID file — process is gone, safe to proceed
    open(_PID_FILE, "w").write(str(os.getpid()))


def _remove_pid_lock():
    try:
        os.remove(_PID_FILE)
    except FileNotFoundError:
        pass


async def main():
    _check_pid_lock()
    engine = CoreEngine()

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        log.info(f"Received {sig.name} — shutting down...")
        asyncio.create_task(engine.stop())

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _shutdown, s)

    def _reload_map():
        engine._whale.reload_map()
        log.info("Market map hot-reloaded via SIGUSR1.")

    loop.add_signal_handler(signal.SIGUSR1, _reload_map)

    try:
        await engine.start()
    except asyncio.CancelledError:
        pass
    finally:
        _remove_pid_lock()
        log.info("GENESIS-ONE offline.")


if __name__ == "__main__":
    asyncio.run(main())
