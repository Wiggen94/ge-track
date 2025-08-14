from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

FOUR_HOURS_SEC = 4 * 60 * 60


def _read_json_or_jsonl(path: str) -> list:
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return [data]
        return []
    except Exception:
        # Try JSONL
        entries = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            entries.append(obj)
                    except Exception:
                        continue
            return entries
        except Exception:
            return []


def _normalize_ts(ts: Optional[int]) -> int:
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        t = int(ts)
        if t > 10**12:
            t //= 1000
        return t
    try:
        t = int(ts)
        if t > 10**12:
            t //= 1000
        return t
    except Exception:
        return 0


def compute_remaining_from_flipper2(dir_path: str, *, buy_limits: Dict[int, Optional[int]]) -> Dict[int, int]:
    """
    Reads Flipper2 logs in dir_path (flipper2-buys.json, flipper2-sells.json, flipper2-flips.json)
    and computes per-item remaining quantity in the last 4 hours based on buy_limits.
    Only buys are counted towards the 4-hour cap.
    Supports JSON array or JSONL formats.
    """
    if not dir_path or not os.path.isdir(dir_path):
        return {}

    buys_file = os.path.join(dir_path, "flipper2-buys.json")
    flips_file = os.path.join(dir_path, "flipper2-flips.json")
    # sells file exists but not needed for remaining buys cap

    now = int(time.time())
    cutoff = now - FOUR_HOURS_SEC
    used: Dict[int, int] = {}

    def add_entry(e: dict) -> None:
        item_id = e.get("itemId") or e.get("id") or e.get("item")
        if item_id is None:
            return
        try:
            item_id = int(item_id)
        except Exception:
            return
        ts = _normalize_ts(
            e.get("ts")
            or e.get("time")
            or e.get("timestamp")
            or e.get("createdAt")
            or e.get("createdTime")
        )
        if ts < cutoff:
            return
        qty = e.get("quantity") or e.get("qty") or e.get("amount") or e.get("tQIT") or 0
        try:
            qty = int(qty)
        except Exception:
            qty = 0
        if qty > 0:
            used[item_id] = used.get(item_id, 0) + qty

    # Parse buys
    for e in _read_json_or_jsonl(buys_file):
        if isinstance(e, dict):
            add_entry(e)

    # Some setups might only log flips; count the buy side if present
    for e in _read_json_or_jsonl(flips_file):
        if not isinstance(e, dict):
            continue
        # Look for nested buy info
        buy = e.get("buy") if isinstance(e.get("buy"), dict) else None
        if buy:
            add_entry(buy)
        else:
            side = (e.get("side") or e.get("type") or "").lower()
            if side in ("buy", "bought", "buying"):
                add_entry(e)

    remaining: Dict[int, int] = {}
    for item_id, limit in buy_limits.items():
        if limit is None:
            continue
        already = used.get(item_id, 0)
        remaining[item_id] = max(0, int(limit) - already)
    return remaining
