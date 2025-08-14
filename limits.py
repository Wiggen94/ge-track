from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

FOUR_HOURS_SEC = 4 * 60 * 60


@dataclass
class LimitEvent:
    ts: int
    item_id: int
    type: str  # 'buy' | 'sell'
    qty: int


@dataclass
class LimitState:
    events: List[LimitEvent]
    version: int = 1


def load_state(path: str) -> LimitState:
    if not path or not os.path.exists(path):
        return LimitState(events=[])
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    events_raw = data.get("events", [])
    events: List[LimitEvent] = []
    for e in events_raw:
        try:
            events.append(
                LimitEvent(
                    ts=int(e.get("ts", 0)),
                    item_id=int(e.get("item_id")),
                    type=str(e.get("type")),
                    qty=int(e.get("qty", 0)),
                )
            )
        except Exception:
            continue
    # prune very old events (older than 8h for compactness)
    now = int(time.time())
    fresh_cutoff = now - (2 * FOUR_HOURS_SEC)
    events = [e for e in events if e.ts >= fresh_cutoff]
    return LimitState(events=events, version=int(data.get("version", 1)))


def save_state(path: str, state: LimitState) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": state.version,
                "events": [e.__dict__ for e in state.events],
            },
            f,
            indent=2,
        )


def append_event(path: str, *, item_id: int, qty: int, type: str = "buy", ts: Optional[int] = None) -> None:
    if type not in ("buy", "sell"):
        raise ValueError("type must be 'buy' or 'sell'")
    state = load_state(path)
    state.events.append(
        LimitEvent(
            ts=int(ts if ts is not None else time.time()),
            item_id=int(item_id),
            type=type,
            qty=int(qty),
        )
    )
    save_state(path, state)


def compute_remaining_limits(path: str, *, buy_limits: Dict[int, Optional[int]]) -> Dict[int, int]:
    """
    Returns per-item remaining quantity within a rolling 4-hour window based on local recorded buys.
    Items without a known buy limit are omitted.
    """
    state = load_state(path)
    now = int(time.time())
    cutoff = now - FOUR_HOURS_SEC
    # Sum buys in window
    used: Dict[int, int] = {}
    for e in state.events:
        if e.type != "buy":
            continue
        if e.ts < cutoff:
            continue
        used[e.item_id] = used.get(e.item_id, 0) + max(0, e.qty)
    remaining: Dict[int, int] = {}
    for item_id, limit in buy_limits.items():
        if limit is None:
            continue
        already = used.get(item_id, 0)
        remaining[item_id] = max(0, int(limit) - already)
    return remaining
