#!/usr/bin/env python3
"""
Wallet on the Run — data fetcher (privacy build).

Modes (pass as arg or MODE env var):
  poll      (default)  current location. No Premium needed.
  backfill             full history (last N days). Needs Premium.
  migrate              re-process the EXISTING wallet.json in place — re-snap every
                       point, strip house numbers, add business flavor. No Tile call.

Privacy design:
  - Every point is SNAPPED to a ~200 m grid cell and placed at a fixed per-cell
    offset. The true sub-cell position is discarded (lossy), and the offset is
    deterministic, so repeated pings can't be averaged back to the real spot.
    Even with this code public, the most anyone recovers is the 200 m cell.
  - Labels are street + neighborhood only (no house numbers).
  - Business flavor is looked up around the SNAPPED point, so it can't re-pin you.

Secrets (GitHub repo secrets): TILE_EMAIL, TILE_PASSWORD
Optional: TILE_NAME, BACKFILL_DAYS (default 30)

Writes: wallet.json, .geocache.json  (history_raw.json is intentionally NOT saved)
"""

import asyncio
import hashlib
import json
import math
import os
import pathlib
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

EMAIL = os.environ.get("TILE_EMAIL", "")
PASSWORD = os.environ.get("TILE_PASSWORD", "")
TILE_NAME = os.environ.get("TILE_NAME", "").strip().lower()
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS") or "30")

# ~200 m grid at Seattle's latitude
CELL_LAT = 0.0018
CELL_LNG = 0.0027

LAT_KEYS = ("latitude", "lat")
LNG_KEYS = ("longitude", "lng", "lon", "long")
TS_KEYS = ("location_timestamp", "timestamp", "logged_timestamp",
           "end_timestamp", "ts", "time", "logged_ts")


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


def snap(lat, lng):
    """Lossy snap to a ~200 m cell + deterministic per-cell offset. Returns (lat, lng, cell_key)."""
    clat = round(lat / CELL_LAT) * CELL_LAT
    clng = round(lng / CELL_LNG) * CELL_LNG
    cell_key = f"{clat:.5f},{clng:.5f}"
    h = int(hashlib.sha256(cell_key.encode()).hexdigest(), 16)
    ang = math.radians(h % 360)
    dist = 120 + (h % 121)  # fixed 120-240 m offset for this cell
    dlat = dist * math.cos(ang) / 111_320.0
    dlng = dist * math.sin(ang) / (111_320.0 * math.cos(math.radians(clat)) or 1e-6)
    return round(clat + dlat, 5), round(clng + dlng, 5), cell_key


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


def http_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "wallet-on-the-run/1.0 (personal project)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def reverse_geocode(lat, lng):
    """Street + neighborhood only. Returns (street, hood)."""
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


PHRASING = {
    "cafe": "possibly stopped for coffee at {n}",
    "coffee": "possibly stopped for coffee at {n}",
    "bakery": "maybe grabbed a pastry near {n}",
    "bar": "perhaps a drink at {n}",
    "pub": "perhaps a pint at {n}",
    "restaurant": "possibly a bite at {n}",
    "ice_cream": "maybe an ice cream at {n}",
    "books": "maybe browsing {n}",
    "default": "seen loitering near {n}",
}


def nearby_business(lat, lng):
    """Best-effort: nearest cafe/bar/etc within ~400 m of the SNAPPED point.
    Returns (flavor_or_None, ok). ok=False means network trouble (don't cache)."""
    q = (
        "[out:json][timeout:15];("
        f'node(around:400,{lat},{lng})[amenity~"^(cafe|bar|pub|restaurant|bakery|ice_cream)$"][name];'
        f'node(around:400,{lat},{lng})[shop~"^(books|coffee|bakery)$"][name];'
        ");out 40;"
    )
    url = "https://overpass-api.de/api/interpreter?" + urllib.parse.urlencode({"data": q})
    try:
        j = http_json(url, timeout=25)
    except Exception as e:
        print(f"  overpass failed {lat},{lng}: {e}", file=sys.stderr)
        return None, False
    best = None
    for el in j.get("elements", []):
        elat, elng = el.get("lat"), el.get("lon")
        name = (el.get("tags", {}) or {}).get("name")
        if elat is None or not name:
            continue
        d = math.hypot((elat - lat) * 111_320, (elng - lng) * 111_320 * math.cos(math.radians(lat)))
        tags = el.get("tags", {})
        kind = tags.get("amenity") or tags.get("shop") or "default"
        if best is None or d < best[0]:
            best = (d, name, kind)
    if not best:
        return None, True  # genuinely nothing nearby - cache this
    _, name, kind = best
    template = PHRASING.get(kind, PHRASING["default"])
    return template.format(n=name), True


def enrich(lat, lng, ts, cache):
    """Snap a raw point and attach labels + flavor, using the per-cell cache."""
    slat, slng, cell = snap(lat, lng)
    c = cache.get(cell)
    if c is None or "street" not in c:
        street, hood = reverse_geocode(slat, slng)
        time.sleep(1.1)
        flavor, ok = nearby_business(slat, slng)
        time.sleep(1.1)
        c = {"street": street, "hood": hood}
        if ok:
            c["flavor"] = flavor  # may be None (= checked, nothing there)
        cache[cell] = c
    return {
        "ts": ts, "lat": slat, "lng": slng,
        "street": c.get("street"), "hood": c.get("hood"),
        "flavor": c.get("flavor"), "snapped": True,
    }


def merge(existing, new_points):
    by_ts = {p.get("ts"): p for p in existing if p.get("ts")}
    added = 0
    for p in new_points:
        ts = p.get("ts")
        if not ts or ts in by_ts:
            continue
        by_ts[ts] = p
        added += 1
    return sorted(by_ts.values(), key=lambda p: p.get("ts") or ""), added


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
    cache = load_json(CACHE, {})

    if mode == "migrate":
        existing = load_json(DATA, [])
        print(f"Migrating {len(existing)} existing points to snapped/obscured form...")
        cache = {}  # start clean so no old precise-coordinate keys survive
        out = []
        for p in existing:
            if p.get("snapped"):
                out.append(p)  # already obscured — leave it (keeps re-runs stable)
                continue
            try:
                out.append(enrich(float(p["lat"]), float(p["lng"]), p.get("ts"), cache))
            except (KeyError, TypeError, ValueError):
                continue
        merged, _ = merge([], out)
        save_json(CACHE, cache)  # cell-keyed only
        save_json(DATA, merged)
        print(f"Done. wallet.json now holds {len(merged)} snapped points.")
        return

    found = asyncio.run(get_tile_points(mode))
    enriched = [enrich(p["lat"], p["lng"], p["ts"], cache) for p in found if p.get("ts")]
    existing = load_json(DATA, [])
    merged, added = merge(existing, enriched)
    save_json(CACHE, cache)
    save_json(DATA, merged)
    print(f"Done. {added} new point(s). wallet.json now holds {len(merged)} total.")


if __name__ == "__main__":
    main()
