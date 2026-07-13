#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

API_URL = "https://buyerapi.shopgoodwill.com/api/Search/ItemListing"
ITEM_URL = "https://shopgoodwill.com/item/{item_id}"

SG_TZ = ZoneInfo("America/Los_Angeles")

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"

DEFAULT_SEARCHES = [
    {
        "search_text": "harley davidson shirt",
        "title_must_include": ["harley"],
    },
]


log = logging.getLogger("sgw")


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def float_env(name: str, default: float) -> float:
    raw = env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


WEBHOOK_URL = env("DISCORD_WEBHOOK_URL")
LEAD_MINUTES = float_env("LEAD_MINUTES", 5.0)
POLL_SECONDS = float_env("POLL_SECONDS", 60.0)
HORIZON_HOURS = float_env("HORIZON_HOURS", 24.0)


def build_query(search_text: str, page: int = 1, page_size: int = 40) -> Dict[str, Any]:
    return {
        "isSize": False,
        "isWeddingCatagory": "false",
        "isMultipleCategoryIds": False,
        "isFromHeaderMenuTab": False,
        "layout": "",
        "isFromCategoryPage": False,
        "selectedGroup": "",
        "selectedCategoryIds": "",
        "selectedSellerIds": "",
        "searchText": search_text,
        "lowPrice": "0",
        "highPrice": "999999",
        "searchBuyNowOnly": "",
        "searchPickupOnly": "false",
        "searchNoPickupOnly": "false",
        "searchDescriptions": "false",
        "searchClosedAuctions": "false",
        "closedAuctionEndingDate": "1/1/1",
        "closedAuctionDaysBack": "7",
        "searchCanadaShipping": "false",
        "searchInternationalShippingOnly": "false",
        "sortColumn": "1",
        "page": str(page),
        "pageSize": str(page_size),
        "sortDescending": "false",
        "savedSearchId": 0,
        "useBuyerPrefs": "true",
        "searchUSOnlyShipping": "false",
        "categoryLevelNo": "1",
        "categoryLevel": 1,
        "categoryId": 0,
        "partNumber": "",
        "catIds": "",
        "categoryColumns": "Name",
    }


_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://shopgoodwill.com",
    "Referer": "https://shopgoodwill.com/",
    "User-Agent": "Mozilla/5.0 (shopgoodwill-tracker)",
}


def _fetch_page(search_text: str, session: requests.Session,
                page: int, page_size: int) -> List[Dict[str, Any]]:
    resp = session.post(
        API_URL,
        json=build_query(search_text, page=page, page_size=page_size),
        headers=_HEADERS,
        timeout=30,
    )
    resp.raise_for_status()
    results = (resp.json() or {}).get("searchResults") or {}
    return results.get("items") or []


def fetch_items(search_text: str, session: requests.Session,
                horizon: Optional[datetime] = None,
                page_size: int = 40, max_pages: int = 25) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        items = _fetch_page(search_text, session, page, page_size)
        if not items:
            break
        all_items.extend(items)
        if len(items) < page_size:
            break
        if horizon is not None:
            last_end = parse_end_time(items[-1].get("endTime") or "")
            if last_end is not None and last_end > horizon:
                break
    return all_items


def parse_end_time(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    text = raw.strip().replace("Z", "")
    if "." in text:
        head, _, frac = text.partition(".")
        text = f"{head}.{frac[:6]}" if frac[:6].isdigit() else head
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y %I:%M:%S %p"):
        try:
            naive = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        try:
            naive = datetime.fromisoformat(text)
        except ValueError:
            log.warning("Could not parse end time: %r", raw)
            return None
    return naive.replace(tzinfo=SG_TZ).astimezone(timezone.utc)


def title_matches(title: str, required: List[str]) -> bool:
    low = title.lower()
    return all(word.lower() in low for word in required)


def normalize(item: Dict[str, Any], required: List[str]) -> Optional[Dict[str, Any]]:
    title = item.get("title") or ""
    if required and not title_matches(title, required):
        return None
    end = parse_end_time(item.get("endTime") or "")
    if end is None:
        return None
    item_id = item.get("itemId") or item.get("id")
    if item_id is None:
        return None
    price = item.get("currentPrice")
    if price is None:
        price = item.get("minimumBid", "?")
    img = item.get("imageUrl") or item.get("imageURL") or ""
    img = img.replace("\\", "/")
    if img and not img.startswith("http"):
        img = "https://shopgoodwill.com/" + img.lstrip("/")
    return {
        "item_id": str(item_id),
        "title": title,
        "end_utc": end,
        "price": price,
        "image": img,
        "url": ITEM_URL.format(item_id=item_id),
    }


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("State file unreadable; starting fresh.")
    return {"alerted": {}}


def save_state(state: Dict[str, Any]) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).timestamp()
    state["alerted"] = {
        k: v for k, v in state.get("alerted", {}).items() if v > cutoff
    }
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_discord(item: Dict[str, Any], minutes_left: float) -> bool:
    if not WEBHOOK_URL:
        log.error("DISCORD_WEBHOOK_URL not set; cannot send notification.")
        return False
    end_local = item["end_utc"].astimezone(SG_TZ)
    embed = {
        "title": f"\U0001f3cd️ {item['title']}"[:256],
        "url": item["url"],
        "description": (
            f"**Ending in ~{minutes_left:.0f} min**\n"
            f"Current price: **${item['price']}**\n"
            f"Closes: {end_local:%b %d %I:%M %p} PT"
        ),
        "color": 0xF60000,
    }
    if item["image"]:
        embed["thumbnail"] = {"url": item["image"]}
    payload = {
        "content": "\U0001f6a8 **Auction ending soon!**",
        "embeds": [embed],
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error("Discord webhook failed: %s", exc)
        return False


def run_once(searches: List[Dict[str, Any]], state: Dict[str, Any],
             session: requests.Session) -> None:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=HORIZON_HOURS)
    alerted = state.setdefault("alerted", {})

    for search in searches:
        text = search["search_text"]
        required = search.get("title_must_include", [])
        try:
            raw_items = fetch_items(text, session, horizon=horizon)
        except requests.RequestException as exc:
            log.warning("Search %r failed: %s", text, exc)
            continue

        in_horizon = 0
        for raw in raw_items:
            item = normalize(raw, required)
            if item is None:
                continue
            if item["end_utc"] > horizon or item["end_utc"] <= now:
                continue
            in_horizon += 1
            minutes_left = (item["end_utc"] - now).total_seconds() / 60.0
            if minutes_left <= LEAD_MINUTES and item["item_id"] not in alerted:
                log.info("ALERT %s (%.1f min left): %s",
                         item["item_id"], minutes_left, item["title"])
                if send_discord(item, minutes_left):
                    alerted[item["item_id"]] = now.timestamp()
        log.info("Search %r: %d matching auctions in horizon.", text, in_horizon)

    save_state(state)


def load_searches() -> List[Dict[str, Any]]:
    cfg = HERE / "searches.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("searches.json unreadable (%s); using defaults.", exc)
    return DEFAULT_SEARCHES


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not WEBHOOK_URL:
        log.error("Set the DISCORD_WEBHOOK_URL environment variable before running.")
        return 1

    searches = load_searches()
    state = load_state()
    session = requests.Session()

    once = "--once" in sys.argv
    log.info("Tracking %d search(es); lead=%.0f min; poll=%.0fs; %s",
             len(searches), LEAD_MINUTES, POLL_SECONDS,
             "single run" if once else "looping")

    while True:
        try:
            run_once(searches, state, session)
        except Exception:
            log.exception("Unexpected error during poll; continuing.")
        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
