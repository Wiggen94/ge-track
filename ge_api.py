from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests

WIKI_BASE = "https://prices.runescape.wiki/api/v1/osrs"
WIKI_MAPPING_URL = f"{WIKI_BASE}/mapping"
WIKI_LATEST_URL = f"{WIKI_BASE}/latest"
WIKI_ONE_HOUR_URL = f"{WIKI_BASE}/1h"
WIKI_FIVE_MIN_URL = f"{WIKI_BASE}/5m"
WIKI_TIMESERIES_URL = f"{WIKI_BASE}/timeseries"

OFFICIAL_GE_DETAIL_URL = (
    "https://services.runescape.com/m=itemdb_oldschool/api/catalogue/detail.json?item={item_id}"
)


@dataclass
class WikiItem:
    item_id: int
    name: str
    buy_limit: Optional[int]
    members: Optional[bool]


WikiItemMapping = Dict[int, WikiItem]


def _wiki_headers(user_agent: str) -> Dict[str, str]:
    return {"User-Agent": user_agent or "ge-track/0.1 (contact: none)"}


def fetch_item_mapping(user_agent: str) -> WikiItemMapping:
    response = requests.get(WIKI_MAPPING_URL, headers=_wiki_headers(user_agent), timeout=20)
    response.raise_for_status()
    data = response.json()
    mapping: WikiItemMapping = {}
    for entry in data:
        item_id = int(entry["id"]) if "id" in entry else int(entry["item"])
        mapping[item_id] = WikiItem(
            item_id=item_id,
            name=entry.get("name", str(item_id)),
            buy_limit=entry.get("limit"),
            members=entry.get("members"),
        )
    return mapping


def fetch_one_hour_prices(user_agent: str) -> Dict[int, Dict[str, Optional[int]]]:
    response = requests.get(WIKI_ONE_HOUR_URL, headers=_wiki_headers(user_agent), timeout=20)
    response.raise_for_status()
    payload = response.json()
    return {int(k): v for k, v in payload.get("data", {}).items()}


def fetch_five_min_prices(user_agent: str) -> Dict[int, Dict[str, Optional[int]]]:
    response = requests.get(WIKI_FIVE_MIN_URL, headers=_wiki_headers(user_agent), timeout=20)
    response.raise_for_status()
    payload = response.json()
    return {int(k): v for k, v in payload.get("data", {}).items()}


def fetch_latest_prices(user_agent: str) -> Dict[int, Dict[str, Optional[int]]]:
    response = requests.get(WIKI_LATEST_URL, headers=_wiki_headers(user_agent), timeout=20)
    response.raise_for_status()
    payload = response.json()
    return {int(k): v for k, v in payload.get("data", {}).items()}


def fetch_timeseries(item_id: int, *, timestep: str = "1h", user_agent: str = "") -> Dict[str, List[Dict]]:
    params = {"id": item_id, "timestep": timestep}
    response = requests.get(WIKI_TIMESERIES_URL, params=params, headers=_wiki_headers(user_agent), timeout=20)
    response.raise_for_status()
    return response.json()


def _parse_ge_price_str(value: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip().lower()
    if s == "unknown" or s == "n/a":
        return None
    try:
        # Handles formats like '12.3k', '2.1m', '123'
        multiplier = 1
        if s.endswith("b"):
            multiplier = 1_000_000_000
            s = s[:-1]
        elif s.endswith("m"):
            multiplier = 1_000_000
            s = s[:-1]
        elif s.endswith("k"):
            multiplier = 1_000
            s = s[:-1]
        return int(float(s) * multiplier)
    except Exception:
        return None


def fetch_ge_guide_price(item_id: int) -> Optional[int]:
    url = OFFICIAL_GE_DETAIL_URL.format(item_id=item_id)
    response = requests.get(url, timeout=20)
    if not response.ok:
        return None
    try:
        data = response.json()
    except Exception:
        return None
    item = data.get("item", {})
    current = item.get("current", {})
    price = current.get("price")
    return _parse_ge_price_str(price)
