from __future__ import annotations

import asyncio
import os
import time
from typing import Dict, List, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ge_api import (
    fetch_item_mapping,
    fetch_latest_prices,
    fetch_one_hour_prices,
    fetch_timeseries,
)
from suggest import build_suggestions
from .db import init_db, get_session
from .auth import router as auth_router, get_user_from_cookie
from .models import WatchItem, Alert
from .scheduler import start_scheduler

APP_TITLE = "GE Track"
WIKI_UA = os.environ.get("WIKI_USER_AGENT", "ge-track/0.1 (+https://github.com/)")

app = FastAPI(title=APP_TITLE)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Simple in-memory cache
_cache: Dict[str, tuple[float, object]] = {}


@app.on_event("startup")
async def on_startup():
    init_db()
    start_scheduler()


def cache_get(key: str, ttl: float) -> Optional[object]:
    now = time.time()
    entry = _cache.get(key)
    if not entry:
        return None
    ts, value = entry
    if now - ts > ttl:
        return None
    return value


def cache_set(key: str, value: object) -> None:
    _cache[key] = (time.time(), value)


async def get_mapping():
    key = "mapping"
    mapping = cache_get(key, ttl=3600)
    if mapping is None:
        mapping = fetch_item_mapping(WIKI_UA)
        cache_set(key, mapping)
    return mapping


async def get_prices():
    latest = fetch_latest_prices(WIKI_UA)
    one_hour = fetch_one_hour_prices(WIKI_UA)
    return latest, one_hour


app.include_router(auth_router)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = get_user_from_cookie(request)
    mapping = await get_mapping()
    latest, one_hour = await get_prices()
    suggestions = build_suggestions(
        budget_gp=50_000_000,
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
        remaining_limits=None,
        top_n=20,
    )
    watches = []
    alerts = []
    if user:
        with get_session() as s:
            watches = s.query(WatchItem).where(WatchItem.user_id == user.id).all()
            alerts = s.query(Alert).where(Alert.user_id == user.id, Alert.active == True).all()
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "suggestions": suggestions,
            "title": APP_TITLE,
            "user": user,
            "watches": watches,
            "alerts": alerts,
        },
    )


@app.post("/watch/{item_id}")
async def add_watch(request: Request, item_id: int):
    user = get_user_from_cookie(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    mapping = await get_mapping()
    it = mapping.get(item_id)
    if not it:
        return RedirectResponse("/items", status_code=302)
    with get_session() as s:
        existing = s.query(WatchItem).where(WatchItem.user_id == user.id, WatchItem.item_id == item_id).first()
        if not existing:
            s.add(WatchItem(user_id=user.id, item_id=item_id, item_name=it.name))
            s.commit()
    return RedirectResponse(f"/item/{item_id}", status_code=302)


@app.post("/alert/{item_id}")
async def add_alert(
    request: Request,
    item_id: int,
    direction: str = Query("below"),
    price: int = Query(None),
):
    # Accept form fallback
    form = None
    if price is None:
        form = await request.form()
        direction = (form.get("direction") or direction).lower()
        price = int(form.get("price")) if form.get("price") else None
    user = get_user_from_cookie(request)
    if not user or price is None:
        return RedirectResponse("/login", status_code=302)
    mapping = await get_mapping()
    it = mapping.get(item_id)
    if not it:
        return RedirectResponse("/items", status_code=302)
    with get_session() as s:
        s.add(Alert(user_id=user.id, item_id=item_id, item_name=it.name, direction=direction, target_price=price))
        s.commit()
    return RedirectResponse(f"/item/{item_id}", status_code=302)


@app.get("/items", response_class=HTMLResponse)
async def items(request: Request, q: str = Query("", description="Search items")):
    mapping = await get_mapping()
    results = []
    if q:
        qq = q.lower()
        for it in mapping.values():
            if qq in it.name.lower():
                results.append({"id": it.item_id, "name": it.name, "limit": it.buy_limit})
        results.sort(key=lambda x: x["name"])  # alphabetic
    return templates.TemplateResponse("items.html", {"request": request, "results": results, "q": q})


@app.get("/item/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    user = get_user_from_cookie(request)
    mapping = await get_mapping()
    latest, one_hour = await get_prices()
    it = mapping.get(item_id)
    if not it:
        return RedirectResponse(url="/items")
    l = latest.get(item_id) or {}
    h = one_hour.get(item_id) or {}
    # Timeseries for chart (1h timestep over last ~7 days by default per API)
    ts = {}
    try:
        ts = fetch_timeseries(item_id, timestep="1h", user_agent=WIKI_UA)
    except Exception:
        ts = {"data": []}
    context = {
        "request": request,
        "user": user,
        "item": it,
        "latest": l,
        "hour": h,
        "timeseries": ts.get("data", []),
        "title": f"{it.name} - {APP_TITLE}",
    }
    return templates.TemplateResponse("item_detail.html", context)


@app.get("/finder", response_class=HTMLResponse)
async def finder(
    request: Request,
    budget: int = Query(50_000_000),
    min_roi: float = Query(0.005),
    min_profit: int = Query(100),
    min_vol: int = Query(300),
    max_fill_h: float = Query(1.5),
    top: int = Query(20),
):
    mapping = await get_mapping()
    latest, one_hour = await get_prices()
    suggestions = build_suggestions(
        budget_gp=budget,
        mapping=mapping,
        latest=latest,
        one_hour=one_hour,
        min_unit_roi=min_roi,
        min_unit_profit_gp=min_profit,
        aggressiveness=0.3,
        liquidity_fraction=0.25,
        min_hourly_volume=min_vol,
        max_fill_hours=max_fill_h,
        price_source="latest",
        latest_max_age_min=20.0,
        fresh_minutes=10.0,
        fresh_policy="any",
        remaining_limits=None,
        top_n=top,
    )
    return templates.TemplateResponse(
        "finder.html",
        {"request": request, "suggestions": suggestions, "title": f"Finder - {APP_TITLE}"},
    )
