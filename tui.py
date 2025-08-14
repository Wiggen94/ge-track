#!/usr/bin/env python3
import argparse
import os
import sys
import time
from datetime import datetime
from typing import List, Optional, Set, Dict

from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from ge_api import (
    WikiItemMapping,
    fetch_item_mapping,
    fetch_latest_prices,
    fetch_one_hour_prices,
    fetch_ge_guide_price,
)
from suggest import Suggestion, build_suggestions
from limits import compute_remaining_limits
from flipper2_adapter import compute_remaining_from_flipper2
from flip_cli import (
    parse_gp_input,
    DEFAULT_FU_PATH_CANDIDATES,
    format_gp,
)

DEFAULT_FL2_PATH = os.path.expanduser("~/.local/share/bolt-launcher/.runelite/flipper2")


def _auto_discover_fu_path() -> Optional[str]:
    env_override = os.environ.get("FLIPPING_UTILITIES_PATH")
    if env_override and os.path.exists(env_override):
        return env_override
    for p in DEFAULT_FU_PATH_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _auto_discover_flipper2_path() -> Optional[str]:
    env = os.environ.get("FLIPPER2_PATH")
    if env and os.path.isdir(env):
        return env
    if os.path.isdir(DEFAULT_FL2_PATH):
        return DEFAULT_FL2_PATH
    return None


def _style_change(current: int | float, previous: Optional[int | float], *, positive_good: bool, abbreviate: bool) -> Text:
    value_txt = format_gp(current, abbreviate=abbreviate)
    if previous is None:
        return Text(value_txt)
    delta = current - previous
    if delta == 0:
        return Text(value_txt, style="yellow")
    is_positive = delta > 0
    good = is_positive if positive_good else not is_positive
    style = "bold green" if good else "bold red"
    arrow = "▲" if is_positive else "▼"
    return Text(f"{value_txt} {arrow}", style=style)


def _style_plain(value: int | float, *, abbreviate: bool) -> Text:
    return Text(format_gp(value, abbreviate=abbreviate))


def _style_percent(pct: float) -> Text:
    style = "green" if pct >= 1.0 else ("yellow" if pct >= 0.3 else "red")
    return Text(f"{pct:.2f}%", style=style)


def _style_remaining(remaining: Optional[int], buy_limit: Optional[int]) -> Text:
    if remaining is None or buy_limit is None or buy_limit <= 0:
        return Text("-")
    ratio = remaining / max(1, buy_limit)
    style = "green" if ratio >= 0.5 else ("yellow" if ratio >= 0.2 else "red")
    return Text(str(remaining), style=style)


def _read_gp_from_file(path: Optional[str]) -> Optional[int]:
    if not path:
        return None
    try:
        import json
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key in ("gp_available", "gp", "coins", "cash"):
            if isinstance(data.get(key), (int, float)):
                v = int(data[key])
                if v >= 0:
                    return v
        return None
    except Exception:
        return None


def build_table(suggestions: List[Suggestion], *, full_gp: bool, prev: Dict[int, Suggestion], current_gp: Optional[int]) -> Panel:
    abbr_prices = not full_gp

    table = Table(show_header=True, header_style="bold cyan")
    for col, justify in [
        ("Item (ID)", "left"),
        ("Buy", "right"), ("Sell", "right"), ("Qty", "right"),
        ("Unit Profit", "right"), ("Total Profit", "right"), ("ROI", "right"),
        ("Limit", "right"), ("Remain", "right"), ("BuyVol", "right"), ("SellVol", "right"),
        ("Buy h", "right"), ("Sell h", "right"), ("Cycle h", "right"), ("Gp/h", "right"),
    ]:
        table.add_column(col, justify=justify)

    for s in suggestions:
        p = prev.get(s.item_id)
        table.add_row(
            Text(f"{s.item_name} ({s.item_id})", style="bold"),
            _style_change(s.buy_price_gp, p.buy_price_gp if p else None, positive_good=False, abbreviate=abbr_prices),
            _style_change(s.sell_price_gp, p.sell_price_gp if p else None, positive_good=True, abbreviate=abbr_prices),
            Text(str(s.quantity)),
            _style_change(s.unit_profit_gp, p.unit_profit_gp if p else None, positive_good=True, abbreviate=abbr_prices),
            # Always abbreviate Total Profit
            _style_change(s.total_profit_gp, p.total_profit_gp if p else None, positive_good=True, abbreviate=True),
            _style_percent(s.unit_roi * 100.0),
            Text(str(s.buy_limit) if s.buy_limit is not None else "-"),
            _style_remaining(s.remaining_limit, s.buy_limit),
            _style_change(s.buy_hourly_volume, p.buy_hourly_volume if p else None, positive_good=True, abbreviate=True),
            _style_change(s.sell_hourly_volume, p.sell_hourly_volume if p else None, positive_good=True, abbreviate=True),
            Text(f"{s.buy_fill_hours:.2f}"),
            Text(f"{s.sell_fill_hours:.2f}"),
            Text(f"{s.expected_fill_hours:.2f}"),
            _style_change(s.profit_per_hour_gp, p.profit_per_hour_gp if p else None, positive_good=True, abbreviate=True),
        )

    title = Text("OSRS Flip Monitor", style="bold magenta")
    subtitle = Text(
        f"Updated {datetime.now().strftime('%H:%M:%S')}"
        + (f"  |  Budget {format_gp(current_gp, abbreviate=True)}" if current_gp is not None else "")
        + "  |  Ctrl-C to quit",
        style="dim",
    )
    return Panel(table, title=title, subtitle=subtitle, border_style="blue", expand=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Live TUI for OSRS flipping suggestions")
    parser.add_argument("--budget", type=str, required=True, help="Budget (e.g. 50m, 900k)")
    parser.add_argument("--top", type=int, default=30, help="Number of initial items to watch (default 30)")
    parser.add_argument("--interval", type=float, default=10.0, help="Refresh seconds (default 10)")
    parser.add_argument("--full-gp", action="store_true", default=True, help="Full GP integers for prices/profits (default on)")
    parser.add_argument("--ua", type=str, default=os.environ.get("WIKI_USER_AGENT", "ge-track/0.1 (+https://github.com/)"))
    parser.add_argument("--gp-file", type=str, default=None, help="Optional JSON file with current gp (keys: gp_available/gp/coins)")
    parser.add_argument("--auto-add", action="store_true", default=True, help="Auto-add new top items on refresh without removing existing ones (default on)")
    parser.add_argument("--auto-add-top", type=int, default=50, help="How many new top items to consider for auto-add each tick (default 50)")
    parser.add_argument("--max-watch", type=int, default=100, help="Maximum items to keep in the watch list (default 100)")
    args, unknown = parser.parse_known_args()

    budget_gp = parse_gp_input(args.budget)

    mapping: WikiItemMapping = fetch_item_mapping(user_agent=args.ua)
    latest = fetch_latest_prices(user_agent=args.ua)
    one_hour = fetch_one_hour_prices(user_agent=args.ua)

    buy_limits = {iid: it.buy_limit for iid, it in mapping.items()}

    # Prefer Flipper2 over FU backup over local state
    fl2_path = _auto_discover_flipper2_path()
    remaining_limits = None
    if fl2_path:
        rl = compute_remaining_from_flipper2(fl2_path, buy_limits=buy_limits)
        if rl:
            remaining_limits = rl

    if remaining_limits is None:
        fu_path = _auto_discover_fu_path()
        if fu_path:
            remaining_limits = compute_remaining_limits(os.path.expanduser("~/.ge_track_limits.json"), buy_limits=buy_limits)

    if remaining_limits is None:
        remaining_limits = compute_remaining_limits(os.path.expanduser("~/.ge_track_limits.json"), buy_limits=buy_limits)

    # Initial selection (looser defaults): fresh_policy any, min_hourly_volume 300
    initial: List[Suggestion] = build_suggestions(
        budget_gp=budget_gp,
        mapping=mapping,
        latest=latest,
        one_hour=one_hour,
        min_unit_roi=0.005,
        min_unit_profit_gp=100,
        aggressiveness=0.3,
        liquidity_fraction=0.25,
        min_hourly_volume=300,
        max_fill_hours=1.5,
        price_source="latest",
        latest_max_age_min=20.0,
        fresh_minutes=10.0,
        fresh_policy="any",
        remaining_limits=remaining_limits,
        top_n=args.top,
    )

    if not initial:
        print("No initial suggestions.")
        return

    watch_ids = [s.item_id for s in initial]
    prev: Dict[int, Suggestion] = {s.item_id: s for s in initial}

    # Auto-discover gp file next to FU folder if not provided
    gp_file = args.gp_file
    fu_path = _auto_discover_fu_path()
    if gp_file is None and fu_path:
        candidate = os.path.join(fu_path, "ge_track_state.json")
        if os.path.exists(candidate):
            gp_file = candidate

    current_gp = _read_gp_from_file(gp_file)

    console = Console()
    with Live(build_table(initial, full_gp=args.full_gp, prev={}, current_gp=current_gp), console=console, refresh_per_second=4, screen=True) as live:
        while True:
            try:
                time.sleep(args.interval)
                latest = fetch_latest_prices(user_agent=args.ua)
                one_hour = fetch_one_hour_prices(user_agent=args.ua)

                # Refresh remaining limits every tick with Flipper2/FU preference
                remaining_limits = None
                if fl2_path:
                    rl = compute_remaining_from_flipper2(fl2_path, buy_limits=buy_limits)
                    if rl:
                        remaining_limits = rl
                if remaining_limits is None and fu_path:
                    remaining_limits = compute_remaining_limits(os.path.expanduser("~/.ge_track_limits.json"), buy_limits=buy_limits)
                if remaining_limits is None:
                    remaining_limits = compute_remaining_limits(os.path.expanduser("~/.ge_track_limits.json"), buy_limits=buy_limits)

                # Refresh existing watch items (loose freshness during monitoring)
                refreshed: List[Suggestion] = build_suggestions(
                    budget_gp=budget_gp,
                    mapping=mapping,
                    latest=latest,
                    one_hour=one_hour,
                    min_unit_roi=0.0,
                    min_unit_profit_gp=0,
                    aggressiveness=0.3,
                    liquidity_fraction=0.25,
                    min_hourly_volume=0,
                    max_fill_hours=10.0,
                    price_source="latest",
                    latest_max_age_min=20.0,
                    fresh_minutes=10.0,
                    fresh_policy="any",
                    remaining_limits=remaining_limits,
                    top_n=len(watch_ids),
                    include_item_ids=watch_ids,
                )

                # Auto-add new top items with same looser defaults
                if args.auto_add and len(watch_ids) < args.max_watch:
                    add_candidates: List[Suggestion] = build_suggestions(
                        budget_gp=budget_gp,
                        mapping=mapping,
                        latest=latest,
                        one_hour=one_hour,
                        min_unit_roi=0.005,
                        min_unit_profit_gp=100,
                        aggressiveness=0.3,
                        liquidity_fraction=0.25,
                        min_hourly_volume=300,
                        max_fill_hours=1.5,
                        price_source="latest",
                        latest_max_age_min=20.0,
                        fresh_minutes=10.0,
                        fresh_policy="any",
                        remaining_limits=remaining_limits,
                        top_n=args.auto_add_top,
                    )
                    for cand in add_candidates:
                        if cand.item_id not in watch_ids:
                            if len(watch_ids) >= args.max_watch:
                                break
                            watch_ids.append(cand.item_id)
                            refreshed.append(cand)

                id_to_s = {s.item_id: s for s in refreshed}
                rows = [id_to_s.get(i) or prev.get(i) for i in watch_ids]
                rows = [r for r in rows if r is not None]
                rows.sort(key=lambda s: (s.profit_per_hour_gp, s.total_profit_gp), reverse=True)
                current_gp = _read_gp_from_file(gp_file)
                live.update(build_table(rows, full_gp=args.full_gp, prev=prev, current_gp=current_gp))
                prev = {s.item_id: s for s in rows}
            except KeyboardInterrupt:
                break


if __name__ == "__main__":
    main()
