#!/usr/bin/env python3
"""
Wallet on the Run — data fetcher (privacy build; labels only).

Modes (arg or MODE env var):
  poll      (default)  current location. No Premium needed.
  backfill             full history (last N days). Needs Premium.
  migrate              re-process EXISTING wallet.json: snap + strip house numbers.
                       Preserves any business names baked in by bake_business.py.

This job does NOT look up businesses — that turned out to be unreliable from GitHub's
servers. Instead:
  - the website shows a narrative line for every stop (no lookups needed), and
  - real business names are added by running bake_business.py once from your own Mac.

Privacy:
  - Points SNAPPED to a ~200 m grid at a fixed per-cell offset (lossy; averaging-proof).
  - Street + neighborhood labels only (no house numbers).

Secrets: TILE_EMAIL, TILE_PASSWORD    Optional: TILE_NAME, BACKFILL_DAYS (default 30)
Writes: wallet.json, .geocache.json
"""

import asyncio
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from aiohttp import ClientSession
from pytile import async_login

HERE = pathlib.Path(__file__).parent
DATA = HERE / "wallet.json"
CACHE = HERE / ".geocache.json"
UA = "wallet-on-the-run/1.0 (personal project)"

EMAIL = os.environ.get("TILE_EMAIL", "")
PASSWORD = os.environ.get("TILE_PASSWORD", "")
TILE_NAME = os.environ.get("TILE_NAME", "").strip().lower()
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or "30")

CELL_LAT = 0.0018  # ~200 m grid (privacy)
CELL_LNG = 0.0027

LAT_KEYS = ("latitude", "lat")
LNG_KEYS = ("longitude", "lng", "lon", "long")
TS_KEYS = ("location_timestamp", "timestamp", "logged_timestamp",
           "end_timestamp", "ts", "time", "logged_ts")
CELL_KEY_RE = re.compile(r"^-?\d+\.\d{5},-?\d+\.\d{5}$")


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def to_iso(value):
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


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def snap(lat, lng):
    clat = round(lat / CELL_LAT) * CELL_LAT
    clng = round(lng / CELL_LNG) * CELL_LNG
    key = f"{clat:.5f},{clng:.5f}"
    h = int(hashlib.sha256(key.encode()).hexdigest(), 16)
    ang = math.radians(h % 360)
    dist = 120 + (h % 121)
    dlat = dist * math.cos(ang) / 111_320.0
    dlng = dist * math.sin(ang) / (111_320.0 * math.cos(math.radians(clat)) or 1e-6)
    return round(clat + dlat, 5), round(clng + dlng, 5)


def cell_of(lat, lng):
    return f"{round(lat / CELL_LAT) * CELL_LAT:.5f},{round(lng / CELL_LNG) * CELL_LNG:.5f}"


def find_points(obj, out):
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


def reverse_geocode(lat, lng):
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lng, "format": "jsonv2", "zoom": "18"}
    )
    try:
        j = http_json(url)
        a = j.get("address", {})
        street = a.get("road") or (j.get("display_name", "").split(",")[0])
        hood = a.get("neighbourhood") or a.get("suburb") or a.get("city") or a.get("town")
        return street, hood
    except Exception as e:
        print(f"  geocode failed {lat},{lng}: {e}", file=sys.stderr)
        return None, None


def enrich(dlat, dlng, ts, cache):
    key = cell_of(dlat, dlng)
    c = cache.get(key, {})
    if "street" not in c:
        c["street"], c["hood"] = reverse_geocode(dlat, dlng)
        time.sleep(1.1)  # Nominatim: <= 1 req/sec
    cache[key] = c
    return {"ts": ts, "lat": round(dlat, 5), "lng": round(dlng, 5),
            "street": c.get("street"), "hood": c.get("hood"), "snapped": True}


def merge(existing, new_points):
    by_ts = {p.get("ts"): p for p in existing if p.get("ts")}
    added = 0
    for p in new_points:
        ts = p.get("ts")
        if not ts:
            continue
        if ts not in by_ts:
            added += 1
        by_ts[ts] = p
    return sorted(by_ts.values(), key=lambda p: p.get("ts") or ""), added


def clean_cache(cache):
    return {k: v for k, v in cache.items() if CELL_KEY_RE.match(k)}


async def get_tile_points(mode):
    if not EMAIL or not PASSWORD:
        sys.exit("Missing TILE_EMAIL / TILE_PASSWORD. Set them as repo secrets.")
    async with ClientSession() as session:
        api = await async_login(EMAIL, PASSWORD, session)
        tiles = await api.async_get_tiles()
        chosen = None
        for tile in tiles.values():
            name = (tile.name or "").lower()
            if (TILE_NAME and TILE_NAME in name) or (not TILE_NAME and "wallet" in name):
                chosen = tile
                break
        chosen = chosen or next(iter(tiles.values()), None)
        if chosen is None:
            sys.exit("No tiles found on this account.")
        print(f"Tracking tile: {chosen.name!r} ({chosen.uuid})")

        found = []
        if mode == "backfill":
            end = datetime.now()
            start = end - timedelta(days=BACKFILL_DAYS)
            print(f"Pulling history {start.date()} -> {end.date()} (needs Premium)...")
            raw = await chosen.async_history(start, end)
            find_points(raw, found)
            print(f"Extracted {len(found)} raw points.")
        else:
            await chosen.async_update()
            if chosen.latitude is not None and chosen.longitude is not None:
                found.append({"lat": float(chosen.latitude), "lng": float(chosen.longitude),
                              "ts": to_iso(chosen.last_timestamp)})
        return found


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "poll")).lower()
    cache = clean_cache(load_json(CACHE, {}))

    if mode == "migrate":
        existing = load_json(DATA, [])
        out = []
        for p in existing:
            # already snapped -> keep as-is (this preserves any baked 'flavor')
            if p.get("snapped") and "street" in p:
                out.append(p)
                continue
            try:
                dlat, dlng = snap(float(p["lat"]), float(p["lng"]))
                e = enrich(dlat, dlng, p.get("ts"), cache)
                if isinstance(p.get("biz"), dict):
                    e["biz"] = p["biz"]  # carry over a baked business name
                if p.get("ts"):
                    out.append(e)
            except (KeyError, TypeError, ValueError):
                continue
        merged, _ = merge([], out)
        save_json(CACHE, clean_cache(cache))
        save_json(DATA, merged)
        print(f"Done. wallet.json now holds {len(merged)} snapped points (street + neighborhood).")
        return

    raw = asyncio.run(get_tile_points(mode))
    out = [enrich(*snap(p["lat"], p["lng"]), p["ts"], cache) for p in raw if p.get("ts")]
    existing = load_json(DATA, [])
    merged, added = merge(existing, out)
    save_json(CACHE, clean_cache(cache))
    save_json(DATA, merged)
    print(f"Done. {added} new point(s). wallet.json now holds {len(merged)} total.")


if __name__ == "__main__":
    main()
