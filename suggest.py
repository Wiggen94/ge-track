from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Iterable

from ge_api import WikiItemMapping

# OSRS GE tax: 2% of sale value, capped at 5,000,000 gp per item
GE_TAX_RATE = 0.02
GE_TAX_CAP = 5_000_000


@dataclass
class Suggestion:
    item_id: int
    item_name: str
    buy_limit: Optional[int]
    buy_price_gp: int
    sell_price_gp: int
    unit_profit_gp: int
    unit_roi: float
    quantity: int
    total_profit_gp: int
    hourly_volume: int
    expected_fill_hours: float
    profit_per_hour_gp: float
    official_ge_price: Optional[int] = None
    # Transparency fields
    buy_hourly_volume: int = 0
    sell_hourly_volume: int = 0
    buy_fill_hours: float = 0.0
    sell_fill_hours: float = 0.0
    remaining_limit: Optional[int] = None


def _adjust_prices_for_aggressiveness(base_buy: int, base_sell: int, aggressiveness: float) -> Tuple[int, int]:
    if base_buy <= 0 or base_sell <= 0 or base_sell <= base_buy:
        return base_buy, base_sell
    spread = base_sell - base_buy
    # Move buy up and sell down by up to 25% of spread based on aggressiveness
    buy_adjust = int(spread * 0.25 * max(0.0, min(1.0, aggressiveness)))
    sell_adjust = int(spread * 0.25 * max(0.0, min(1.0, aggressiveness)))
    adj_buy = base_buy + buy_adjust
    adj_sell = base_sell - sell_adjust
    # Ensure still profitable
    if adj_sell <= adj_buy:
        adj_buy = base_buy
        adj_sell = base_sell
    return adj_buy, adj_sell


def _compute_tax_per_unit(unit_sell_price: int) -> int:
    return min(int(unit_sell_price * GE_TAX_RATE), GE_TAX_CAP)


def _choose_prices_for_item(
    item_id: int,
    *,
    latest: Dict[int, Dict[str, Optional[int]]],
    one_hour: Dict[int, Dict[str, Optional[int]]],
    five_min: Optional[Dict[int, Dict[str, Optional[int]]]] = None,
    price_source: str,
    latest_max_age_min: float,
) -> Optional[Tuple[int, int, int, int]]:
    # Returns (buy_price, sell_price, low_vol, high_vol)
    lh = one_hour.get(item_id)
    lt = latest.get(item_id)
    fm = five_min.get(item_id) if five_min else None

    if price_source == "1h":
        if not lh:
            return None
        return int(lh.get("avgLowPrice") or 0), int(lh.get("avgHighPrice") or 0), int(lh.get("lowPriceVolume") or 0), int(lh.get("highPriceVolume") or 0)
    if price_source == "latest":
        if not lt:
            return None
        return int(lt.get("low") or 0), int(lt.get("high") or 0), int(lh.get("lowPriceVolume") or 0) if lh else 0, int(lh.get("highPriceVolume") or 0) if lh else 0

    # hybrid: prefer latest if fresh; else prefer 5m if available; else 1h
    if lt and isinstance(lt.get("highTime"), int) and isinstance(lt.get("lowTime"), int):
        import time
        now = int(time.time())
        high_age_min = (now - lt["highTime"]) / 60.0
        low_age_min = (now - lt["lowTime"]) / 60.0
        if high_age_min <= latest_max_age_min and low_age_min <= latest_max_age_min:
            buy = int(lt.get("low") or 0)
            sell = int(lt.get("high") or 0)
            lv = int(lh.get("lowPriceVolume") or 0) if lh else 0
            hv = int(lh.get("highPriceVolume") or 0) if lh else 0
            return buy, sell, lv, hv
    if fm:
        return int(fm.get("avgLowPrice") or 0), int(fm.get("avgHighPrice") or 0), int(lh.get("lowPriceVolume") or 0) if lh else 0, int(lh.get("highPriceVolume") or 0) if lh else 0
    if lh:
        return int(lh.get("avgLowPrice") or 0), int(lh.get("avgHighPrice") or 0), int(lh.get("lowPriceVolume") or 0), int(lh.get("highPriceVolume") or 0)
    return None


def _is_fresh_enough(lt_entry: Optional[Dict[str, int]], fresh_minutes: float, policy: str) -> bool:
    if not fresh_minutes or fresh_minutes <= 0:
        return True
    if not lt_entry:
        return False
    high_time = lt_entry.get("highTime")
    low_time = lt_entry.get("lowTime")
    if not isinstance(high_time, int) and not isinstance(low_time, int):
        return False
    import time
    now = int(time.time())
    high_ok = isinstance(high_time, int) and (now - high_time) <= fresh_minutes * 60
    low_ok = isinstance(low_time, int) and (now - low_time) <= fresh_minutes * 60
    return (high_ok or low_ok) if policy == "any" else (high_ok and low_ok)


def build_suggestions(
    *,
    budget_gp: int,
    mapping: WikiItemMapping,
    latest: Dict[int, Dict[str, Optional[int]]],
    one_hour: Dict[int, Dict[str, Optional[int]]],
    min_unit_roi: float,
    min_unit_profit_gp: int,
    aggressiveness: float,
    liquidity_fraction: float,
    min_hourly_volume: int,
    max_fill_hours: float,
    price_source: str,
    latest_max_age_min: float,
    fresh_minutes: float,
    fresh_policy: str,
    remaining_limits: Optional[Dict[int, int]] = None,
    top_n: int = 10,
    include_item_ids: Optional[Iterable[int]] = None,
) -> List[Suggestion]:
    results: List[Suggestion] = []

    # Attempt to fetch 5m if present in inputs (call site uses only latest/1h; 5m is optional)
    five_min = globals().get("__five_min_data__")  # not used by default

    universe = one_hour.keys() | latest.keys()
    if include_item_ids is not None:
        include_set = set(include_item_ids)
        universe = [iid for iid in universe if iid in include_set]

    for item_id in universe:
        # Freshness filter using latest timestamps
        if not _is_fresh_enough(latest.get(item_id), fresh_minutes, fresh_policy):
            continue

        chosen = _choose_prices_for_item(
            item_id,
            latest=latest,
            one_hour=one_hour,
            five_min=five_min,
            price_source=price_source,
            latest_max_age_min=latest_max_age_min,
        )
        if not chosen:
            continue
        base_buy, base_sell, low_vol, high_vol = chosen

        if not base_buy or not base_sell or base_buy <= 0 or base_sell <= 0:
            continue
        if base_sell <= base_buy:
            continue

        # Require both sides to have decent activity
        if min(low_vol, high_vol) < max(0, min_hourly_volume):
            continue

        buy_price, sell_price = _adjust_prices_for_aggressiveness(base_buy, base_sell, aggressiveness)

        # Capacities per hour on each side (scaled by liquidity_fraction)
        buy_capacity_per_hour = max(1, int(low_vol * max(0.0, min(1.0, liquidity_fraction))))
        sell_capacity_per_hour = max(1, int(high_vol * max(0.0, min(1.0, liquidity_fraction))))

        # Buy limit constraint
        item = mapping.get(item_id)
        buy_limit = item.buy_limit if item else None

        # Budget constraint
        max_by_budget = budget_gp // buy_price
        if max_by_budget <= 0:
            continue

        quantity = max_by_budget
        if buy_limit is not None:
            quantity = min(quantity, buy_limit)

        # Remaining buy-limit constraint (from local/FU tracking)
        rem = None
        if remaining_limits and item_id in remaining_limits:
            rem = max(0, remaining_limits[item_id])
            quantity = min(quantity, rem)

        # Liquidity caps (how much we can reasonably transact within ~1 hour window)
        quantity = min(quantity, buy_capacity_per_hour, sell_capacity_per_hour)
        if quantity <= 0:
            continue

        # Fill-time constraints: cap qty so buy and sell can each complete within max_fill_hours
        max_qty_by_buy_time = int(low_vol * max(0.0, max_fill_hours))
        max_qty_by_sell_time = int(high_vol * max(0.0, max_fill_hours))
        if max_qty_by_buy_time <= 0 or max_qty_by_sell_time <= 0:
            continue
        quantity = min(quantity, max_qty_by_buy_time, max_qty_by_sell_time)
        if quantity <= 0:
            continue

        # Profit calculations (tax per unit, capped per item)
        unit_tax = _compute_tax_per_unit(sell_price)
        total_sale_value = sell_price * quantity
        total_tax = unit_tax * quantity
        total_buy_cost = buy_price * quantity
        total_profit = (total_sale_value - total_tax) - total_buy_cost
        unit_profit = total_profit // max(1, quantity)
        unit_roi = unit_profit / buy_price

        if unit_profit < min_unit_profit_gp or unit_roi < min_unit_roi:
            continue

        # Compute cycle time = buy fill time + sell fill time
        buy_fill_hours = quantity / max(1, low_vol)
        sell_fill_hours = quantity / max(1, high_vol)
        expected_fill_hours = buy_fill_hours + sell_fill_hours
        profit_per_hour = total_profit / expected_fill_hours if expected_fill_hours > 0 else float(total_profit)

        suggestion = Suggestion(
            item_id=item_id,
            item_name=item.name if item else str(item_id),
            buy_limit=buy_limit,
            buy_price_gp=buy_price,
            sell_price_gp=sell_price,
            unit_profit_gp=unit_profit,
            unit_roi=unit_roi,
            quantity=quantity,
            total_profit_gp=total_profit,
            hourly_volume=min(low_vol, high_vol),
            expected_fill_hours=expected_fill_hours,
            profit_per_hour_gp=profit_per_hour,
            buy_hourly_volume=low_vol,
            sell_hourly_volume=high_vol,
            buy_fill_hours=buy_fill_hours,
            sell_fill_hours=sell_fill_hours,
            remaining_limit=rem,
        )
        results.append(suggestion)

    # Rank primarily by profit per hour, then by total profit, then by ROI
    results.sort(key=lambda s: (s.profit_per_hour_gp, s.total_profit_gp, s.unit_roi), reverse=True)

    return results[: max(1, top_n)]
