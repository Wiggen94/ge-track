#!/usr/bin/env python3
import argparse
import os
import sys
from typing import List, Optional

from ge_api import (
    WikiItemMapping,
    fetch_item_mapping,
    fetch_latest_prices,
    fetch_one_hour_prices,
    fetch_ge_guide_price,
)
from suggest import Suggestion, build_suggestions
from limits import compute_remaining_limits, append_event
from flipper2_adapter import compute_remaining_from_flipper2


DEFAULT_FU_PATH_CANDIDATES = [
    os.path.expanduser("~/.local/share/bolt-launcher/.runelite/flipping"),
    os.path.expanduser("~/.runelite/flipping"),
    os.path.expanduser("~/.config/RuneLite/flipping"),
    os.path.expanduser("~/.local/share/RuneLite/flipping"),
]
DEFAULT_FL2_PATH = os.path.expanduser("~/.local/share/bolt-launcher/.runelite/flipper2")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Suggest profitable OSRS GE flips using OSRS Wiki market prices and buy limits."
        )
    )
    parser.add_argument(
        "--budget",
        type=str,
        required=True,
        help="Available GP to allocate (e.g. 900k, 1.5m, 2b)",
    )
    parser.add_argument(
        "--top", type=int, default=10, help="Number of suggestions to show (default 10)"
    )
    parser.add_argument(
        "--min-roi",
        type=float,
        default=0.005,
        help="Minimum ROI per unit, e.g. 0.01 = 1% (default 0.005)",
    )
    parser.add_argument(
        "--min-profit",
        type=int,
        default=100,
        help="Minimum profit per unit in gp (default 100)",
    )
    parser.add_argument(
        "--aggressiveness",
        type=float,
        default=0.3,
        help=(
            "0..1; higher raises buy price slightly and lowers sell price slightly to fill faster (default 0.3)"
        ),
    )
    parser.add_argument(
        "--liquidity-frac",
        type=float,
        default=0.25,
        help=(
            "Fraction of 1h volume to consider safe to attempt within ~1 hour (default 0.25)"
        ),
    )
    parser.add_argument(
        "--min-hourly-volume",
        type=int,
        default=500,
        help="Filter out items with 1h volume below this (default 500)",
    )
    parser.add_argument(
        "--max-fill-hours",
        type=float,
        default=1.5,
        help="Cap quantity so expected fill time is under this many hours (default 1.5)",
    )
    parser.add_argument(
        "--price-source",
        choices=["latest", "1h", "hybrid"],
        default="latest",
        help="Use 'latest' prices, 1h averages, or hybrid per freshness (default latest)",
    )
    parser.add_argument(
        "--latest-max-age-min",
        type=float,
        default=20.0,
        help="Consider 'latest' prices fresh if under this many minutes old (default 20)",
    )
    parser.add_argument(
        "--fresh-minutes",
        type=float,
        default=10.0,
        help="Filter out items with no latest trade in the last N minutes (default 10)",
    )
    parser.add_argument(
        "--fresh-policy",
        choices=["any", "both"],
        default="both",
        help="Require any or both of latest buy/sell timestamps to be within fresh-minutes (default both)",
    )
    parser.add_argument(
        "--flipping-utilities-path",
        type=str,
        default=None,
        help="Path to Flipping Utilities JSON export/logs directory (auto-discovery if omitted)",
    )
    parser.add_argument(
        "--limits-file",
        type=str,
        default=os.path.expanduser("~/.ge_track_limits.json"),
        help="Path to local JSON state for buy-limit tracking (fallback when no FU path)",
    )
    parser.add_argument(
        "--record-buy",
        type=int,
        nargs=2,
        metavar=("ITEM_ID", "QTY"),
        help="Record a buy of QTY for ITEM_ID into the local limits state and exit",
    )
    parser.add_argument(
        "--full-gp",
        action="store_true",
        default=True,
        help="Print full GP values (no k/m/b abbreviations) for prices and profits (default on)",
    )
    parser.add_argument(
        "--with-ge",
        action="store_true",
        help="Also fetch and display official GE guide price for suggested items (slower)",
    )
    parser.add_argument(
        "--ua",
        type=str,
        default=os.environ.get("WIKI_USER_AGENT", "ge-track/0.1 (+https://github.com/)"),
        help="User-Agent for OSRS Wiki Prices API (default from WIKI_USER_AGENT env or a generic UA)",
    )
    return parser.parse_args()


def format_gp(value: int | float, *, abbreviate: bool = True) -> str:
    try:
        value_int = int(value)
    except Exception:
        value_int = int(round(value))
    if not abbreviate:
        return str(value_int)
    abs_val = abs(value_int)
    if abs_val >= 1_000_000_000:
        return f"{value_int/1_000_000_000:.2f}b"
    if abs_val >= 1_000_000:
        return f"{value_int/1_000_000:.2f}m"
    if abs_val >= 1_000:
        return f"{value_int/1_000:.1f}k"
    return str(value_int)


def parse_gp_input(text: str) -> int:
    s = str(text).strip().lower().replace(",", "").replace("_", "")
    if s.endswith("gp"):
        s = s[:-2].strip()
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
    try:
        value = float(s)
    except ValueError as e:
        raise ValueError(f"Invalid GP amount: {text}") from e
    gp = int(value * multiplier)
    if gp < 0:
        raise ValueError("Budget must be non-negative")
    return gp


def _auto_discover_fu_path() -> Optional[str]:
    env_override = os.environ.get("FLIPPING_UTILITIES_PATH")
    if env_override and os.path.exists(env_override):
        return env_override
    for p in DEFAULT_FU_PATH_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _auto_discover_flipper2_path() -> Optional[str]:
    env_override = os.environ.get("FLIPPER2_PATH")
    if env_override and os.path.isdir(env_override):
        return env_override
    if os.path.isdir(DEFAULT_FL2_PATH):
        return DEFAULT_FL2_PATH
    return None


def main() -> None:
    args = parse_args()

    # Allow recording a buy into local state quickly
    if args.record_buy:
        item_id, qty = args.record_buy
        append_event(args.limits_file, item_id=item_id, qty=qty, type="buy")
        print(f"Recorded buy: item {item_id} qty {qty} -> {args.limits_file}")
        return

    try:
        budget_gp = parse_gp_input(args.budget)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        sys.exit(2)

    try:
        mapping: WikiItemMapping = fetch_item_mapping(user_agent=args.ua)
        one_hour = fetch_one_hour_prices(user_agent=args.ua)
        latest = fetch_latest_prices(user_agent=args.ua)
    except Exception as exc:
        print(f"Failed to fetch market data: {exc}", file=sys.stderr)
        sys.exit(2)

    # Build remaining limit map, preferring Flipper2, then Flipping Utilities, then local state
    buy_limits = {iid: it.buy_limit for iid, it in mapping.items()}

    remaining_limits = None
    fl2_path = _auto_discover_flipper2_path()
    if remaining_limits is None and fl2_path:
        rl = compute_remaining_from_flipper2(fl2_path, buy_limits=buy_limits)
        if rl:
            remaining_limits = rl

    if remaining_limits is None:
        fu_path = args.flipping_utilities_path or _auto_discover_fu_path()
        if fu_path:
            remaining_limits = compute_remaining_from_flipping_utilities(fu_path, buy_limits=buy_limits)

    if remaining_limits is None:
        remaining_limits = compute_remaining_limits(args.limits_file, buy_limits=buy_limits)

    suggestions: List[Suggestion] = build_suggestions(
        budget_gp=budget_gp,
        mapping=mapping,
        latest=latest,
        one_hour=one_hour,
        min_unit_roi=args.min_roi,
        min_unit_profit_gp=args.min_profit,
        aggressiveness=args.aggressiveness,
        liquidity_fraction=args.liquidity_frac,
        min_hourly_volume=args.min_hourly_volume,
        max_fill_hours=args.max_fill_hours,
        price_source=args.price_source,
        latest_max_age_min=args.latest_max_age_min,
        fresh_minutes=args.fresh_minutes,
        fresh_policy=args.fresh_policy,
        remaining_limits=remaining_limits,
        top_n=args.top,
    )

    if args.with_ge:
        for s in suggestions:
            try:
                s.official_ge_price = fetch_ge_guide_price(s.item_id)
            except Exception:
                s.official_ge_price = None

    if not suggestions:
        print("No suggestions matched your filters. Try lowering min ROI/profit, volume/freshness filters, or raising budget.")
        return

    # Prices/profits follow --full-gp (default full integers). Gp/h stays abbreviated for readability.
    abbr_prices = not args.full_gp

    headers = [
        "Item (ID)",
        "Buy", "Sell", "Qty",
        "Unit Profit", "Total Profit", "ROI",
        "Limit", "Remain", "BuyVol", "SellVol", "Buy h", "Sell h", "Cycle h", "Gp/h",
    ]
    if args.with_ge:
        headers.append("GE Price")

    rows: List[List[str]] = []
    for s in suggestions:
        row = [
            f"{s.item_name} ({s.item_id})",
            format_gp(s.buy_price_gp, abbreviate=abbr_prices),
            format_gp(s.sell_price_gp, abbreviate=abbr_prices),
            str(s.quantity),
            format_gp(s.unit_profit_gp, abbreviate=abbr_prices),
            # Always abbreviate total profit for readability
            format_gp(s.total_profit_gp, abbreviate=True),
            f"{s.unit_roi*100:.2f}%",
            str(s.buy_limit) if s.buy_limit is not None else "-",
            str(s.remaining_limit) if s.remaining_limit is not None else "-",
            str(s.buy_hourly_volume),
            str(s.sell_hourly_volume),
            f"{s.buy_fill_hours:.2f}",
            f"{s.sell_fill_hours:.2f}",
            f"{s.expected_fill_hours:.2f}",
            format_gp(s.profit_per_hour_gp, abbreviate=True),
        ]
        if args.with_ge:
            row.append(format_gp(s.official_ge_price, abbreviate=abbr_prices) if s.official_ge_price is not None else "-")
        rows.append(row)

    col_widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print(" ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)))
    print(" ".join("-" * col_widths[i] for i in range(len(headers))))
    for r in rows:
        print(" ".join(r[i].ljust(col_widths[i]) for i in range(len(headers))))


if __name__ == "__main__":
    main()
