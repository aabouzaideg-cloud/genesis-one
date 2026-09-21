"""
PositionStore — persistent position state via local JSON.

Writes to positions.json on every state change so that a crash or restart
never leaves open trades unmanaged. On startup, CoreEngine calls reconcile()
to reload state and drop any positions that are no longer live on Capital.com.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Optional

log = logging.getLogger("PositionStore")

STORE_PATH = os.path.join(os.path.dirname(__file__), "positions.json")


@dataclass
class StoredPosition:
    deal_id:       str
    epic:          str
    direction:     str
    entry:         float
    stop_loss:     float
    take_profit:   float
    size:          float
    atr:           float
    partial_done:  bool
    trailing_stop: float
    opened_at:     str   # ISO timestamp


class PositionStore:
    def __init__(self):
        self._positions: dict[str, StoredPosition] = {}
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(STORE_PATH):
            log.info("No positions.json found — starting fresh.")
            return
        try:
            with open(STORE_PATH, "r") as f:
                raw = json.load(f)
            self._positions = {k: StoredPosition(**v) for k, v in raw.items()}
            log.info(f"Loaded {len(self._positions)} position(s) from store.")
        except Exception as exc:
            log.error(f"Failed to load positions.json: {exc} — starting fresh.")
            self._positions = {}

    def _save(self):
        try:
            with open(STORE_PATH, "w") as f:
                json.dump({k: asdict(v) for k, v in self._positions.items()}, f, indent=2)
        except Exception as exc:
            log.error(f"Failed to save positions.json: {exc}")

    # ── CRUD ─────────────────────────────────────────────────────────────────

    def save(self, pos: StoredPosition):
        self._positions[pos.deal_id] = pos
        self._save()
        log.debug(f"[STORE] Saved position {pos.deal_id}")

    def update(self, deal_id: str, **fields):
        """Update specific fields of a stored position and persist."""
        pos = self._positions.get(deal_id)
        if not pos:
            return
        for key, val in fields.items():
            if hasattr(pos, key):
                setattr(pos, key, val)
        self._save()
        log.debug(f"[STORE] Updated {deal_id}: {fields}")

    def remove(self, deal_id: str):
        if deal_id in self._positions:
            del self._positions[deal_id]
            self._save()
            log.info(f"[STORE] Removed position {deal_id}")

    def all(self) -> list[StoredPosition]:
        return list(self._positions.values())

    def get(self, deal_id: str) -> Optional[StoredPosition]:
        return self._positions.get(deal_id)

    def count(self) -> int:
        return len(self._positions)

    # ── Reconciliation ────────────────────────────────────────────────────────

    def reconcile(self, live_deal_ids: set[str]) -> tuple[list[StoredPosition], list[str]]:
        """
        Compare stored positions against live positions on Capital.com.

        Returns:
          - resumed: positions still live → hand back to RiskManager
          - dropped: deal_ids no longer on broker → remove from store
        """
        resumed = []
        dropped = []

        for deal_id, pos in list(self._positions.items()):
            if deal_id in live_deal_ids:
                resumed.append(pos)
                log.info(f"[STORE] Resuming management of {deal_id} ({pos.epic} {pos.direction})")
            else:
                dropped.append(deal_id)
                del self._positions[deal_id]
                log.info(f"[STORE] Position {deal_id} no longer live — dropping from store")

        if dropped:
            self._save()

        return resumed, dropped
