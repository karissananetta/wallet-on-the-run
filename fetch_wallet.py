#!/usr/bin/env python3
"""
Wallet on the Run — data fetcher.

Two modes:
  poll      (default)  -> grabs the wallet's CURRENT location. No Tile Premium needed.
  backfill             -> pulls location HISTORY for the last N days. Needs Tile Premium.

Reads Tile credentials from environment variables (set as GitHub repo secrets):
  TILE_EMAIL, TILE_PASSWORD
Optional:
  TILE_NAME       substring to pick the right tile (default: anything with "wallet")
  BACKFILL_DAYS   how many days of history to pull in backfill mode (default 30)
  JITTER_METERS   fuzz stored coordinates by up to this many meters (default 0 = exact)

Writes:
  wallet.json        the site's data (list of stops, newest last)
  history_raw.json   raw history dump (backfill mode) — keep this if parsing ever misses
  .geocache.json     cache so we never reverse-geocode the same spot twice
"""

import asyncio
import json
import math
import os
import pathlib
import random
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession
from pytile import async_login

HERE = pathlib.Path(__file__).parent
DATA = HERE / "wallet.json"
RAW = HERE / "history_raw.json"
GEOCACHE = HERE / ".geocache.json"

EMAIL = os.environ.get("TILE_EMAIL", "")
PASSWORD = os.environ.get("TILE_PASSWORD", "")
TILE_NAME = os.environ.get("TILE_NAME", "").strip().lower()
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or "30")
JITTER_METERS = float(os.environ.get("JITTER_METERS") or "0")

LAT_KEYS = ("latitude", "lat")
LNG_KEYS = ("longitude", "lng", "lon", "long")
TS_KEYS = ("timestamp", "logged_timestamp", "end_timestamp", "ts", "time", "logged_ts")


# ---------- small helpers ----------
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def to_iso(value):
    """Normalize a timestamp (ms epoch, s epoch, or ISO string) to ISO-8601 UTC."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        secs = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
    s = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s).astimezone(timezone.utc).isoformat()
    except Exception:
        return str(value)


def jitter(lat, lng):
    if JITTER_METERS <= 0:
        return lat, lng
    r = JITTER_METERS * math.sqrt(random.random())
    theta = random.random() * 2 * math.pi
    dlat = (r * math.cos(theta)) / 111_320.0
    dlng = (r * math.sin(theta)) / (111_320.0 * math.cos(math.radians(lat)) or 1e-6)
    return round(lat + dlat, 6), round(lng + dlng, 6)


def find_points(obj, out):
    """Recursively walk any JSON and collect dicts that contain lat + lng."""
    if isinstance(obj, dict):
        lat = next((obj[k] for k in LAT_KEYS if k in obj), None)
        lng = next((obj[k] for k in LNG_KEYS if k in obj), None)
        if lat is not None and lng is not None:
            ts = next((obj[k] for k in TS_KEYS if k in obj), None)
            try:
                out.append({"lat": float(lat), "lng": float(lng), "ts": to_iso(ts)})
            except (TypeError, ValueError):
                pass
        for v in obj.values():
            find_points(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_points(v, out)


def reverse_geocode(lat, lng, cache):
    key = f"{lat:.4f},{lng:.4f}"
    if key in cache:
        return cache[key]
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lng, "format": "jsonv2", "zoom": "18"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "wallet-on-the-run/1.0 (personal project)"})
    address = hood = None
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.loads(resp.read())
        a = j.get("address", {})
        num = a.get("house_number", "")
        road = a.get("road", "")
        address = (f"{num} {road}").strip() or j.get("display_name", "").split(",")[0]
        hood = a.get("neighbourhood") or a.get("suburb") or a.get("city") or a.get("town")
    except Exception as e:
        print(f"  geocode failed for {key}: {e}", file=sys.stderr)
    cache[key] = {"address": address, "hood": hood}
    time.sleep(1.1)  # Nominatim asks for <= 1 request/sec
    return cache[key]


def merge(existing, new_points):
    by_ts = {p.get("ts"): p for p in existing if p.get("ts")}
    added = 0
    for p in new_points:
        ts = p.get("ts")
        if not ts or ts in by_ts:
            continue
        by_ts[ts] = p
        added += 1
    merged = sorted(by_ts.values(), key=lambda p: p.get("ts") or "")
    return merged, added


# ---------- main ----------
async def run(mode):
    if not EMAIL or not PASSWORD:
        sys.exit("Missing TILE_EMAIL / TILE_PASSWORD. Set them as repo secrets.")

    async with ClientSession() as session:
        api = await async_login(EMAIL, PASSWORD, session)
        tiles = await api.async_get_tiles()

        chosen = None
        for tile in tiles.values():
            name = (tile.name or "").lower()
            if TILE_NAME and TILE_NAME in name:
                chosen = tile
                break
            if not TILE_NAME and "wallet" in name:
                chosen = tile
                break
        if chosen is None:
            chosen = next(iter(tiles.values()), None)
        if chosen is None:
            sys.exit("No tiles found on this account.")
        print(f"Tracking tile: {chosen.name!r} ({chosen.uuid})")

        found = []
        if mode == "backfill":
            end = datetime.now()
            start = end - timedelta(days=BACKFILL_DAYS)
            print(f"Pulling history {start.date()} -> {end.date()} (needs Premium)...")
            raw = await chosen.async_history(start, end)
            save_json(RAW, raw)
            find_points(raw, found)
            print(f"Extracted {len(found)} points from history.")
            if not found:
                print("No points parsed. history_raw.json was saved — send it over and "
                      "the parser can be tuned to Tile's exact shape.", file=sys.stderr)
        else:  # poll
            await chosen.async_update()
            if chosen.latitude is not None and chosen.longitude is not None:
                found.append({
                    "lat": float(chosen.latitude),
                    "lng": float(chosen.longitude),
                    "ts": to_iso(chosen.last_timestamp),
                })
            print(f"Current fix: {found[-1] if found else 'none available'}")

    # apply jitter + reverse-geocode new points
    for p in found:
        p["lat"], p["lng"] = jitter(p["lat"], p["lng"])

    existing = load_json(DATA, [])
    merged, added = merge(existing, found)

    cache = load_json(GEOCACHE, {})
    for p in merged:
        if not p.get("address"):
            g = reverse_geocode(p["lat"], p["lng"], cache)
            p["address"] = g["address"]
            p["hood"] = g["hood"]
    save_json(GEOCACHE, cache)

    save_json(DATA, merged)
    print(f"Done. {added} new point(s). wallet.json now holds {len(merged)} total.")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "poll")).lower()
    asyncio.run(run(mode))
