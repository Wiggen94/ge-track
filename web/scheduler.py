from __future__ import annotations

import os
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .db import get_session
from .models import Alert
from ge_api import fetch_latest_prices

WIKI_UA = os.environ.get("WIKI_USER_AGENT", "ge-track/0.1 (+https://github.com/)")


async def check_alerts_job() -> None:
    latest = fetch_latest_prices(WIKI_UA)
    with get_session() as s:
        active = s.query(Alert).where(Alert.active == True).all()
        changed = False
        for a in active:
            lp = latest.get(a.item_id) or {}
            price = lp.get("high") or lp.get("low")
            if not price:
                continue
            if a.direction == "below" and price <= a.target_price:
                a.active = False
                changed = True
            elif a.direction == "above" and price >= a.target_price:
                a.active = False
                changed = True
            if changed:
                s.add(a)
        if changed:
            s.commit()


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_alerts_job, "interval", minutes=2, id="check_alerts", coalesce=True, max_instances=1)
    scheduler.start()
    return scheduler
