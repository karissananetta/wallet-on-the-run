#!/usr/bin/env python3
"""
Wallet on the Run — data fetcher (privacy build; resilient business lookups).

Modes (arg or MODE env var):
  poll      (default)  current location. No Premium needed.
  backfill             full history (last N days). Needs Premium.
  migrate              re-process EXISTING wallet.json: snap, strip house numbers,
                       and fill in business flavor. INCREMENTAL — only looks up what's
                       still missing, so it's safe to re-run and nothing is wasted.

Privacy:
  - Points SNAPPED to a ~200 m grid at a fixed per-cell offset (lossy; averaging-proof).
  - Street + neighborhood labels only (no house numbers).
  - Business flavor looked up around the SNAPPED point.

Resilience / kindness to free services:
  - Business POIs fetched in a FEW big region queries (not one per point), matched locally.
  - Queries rotate across several Overpass MIRRORS with backoff, so one busy server
    doesn't sink the run.
  - Cumulative: a point is only re-queried until its flavor is confirmed, so repeated
    migrate runs converge (great when a server is having a bad day).

Secrets: TILE_EMAIL, TILE_PASSWORD    Optional: TILE_NAME, BACKFILL_DAYS (default 30)
Writes: wallet.json, .geocache.json   (history_raw.json is intentionally NOT saved)
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

CELL_LAT = 0.0018      # ~200 m grid (privacy)
CELL_LNG = 0.0027
POI_REGION = 0.06      # ~6.5 km tiles for batched business lookups

OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

LAT_KEYS = ("latitude", "lat")
LNG_KEYS = ("longitude", "lng", "lon", "long")
TS_KEYS = ("location_timestamp", "timestamp", "logged_timestamp",
           "end_timestamp", "ts", "time", "logged_ts")
CELL_KEY_RE = re.compile(r"^-?\d+\.\d{5},-?\d+\.\d{5}$")  # 5-decimal = new cell key


# ---------- io ----------
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


def http_json(url, timeout=20, data=None):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


# ---------- geometry ----------
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


def region_key(lat, lng):
    return (round(lat / POI_REGION), round(lng / POI_REGION))


def meters(alat, alng, blat, blng):
    return math.hypot((alat - blat) * 111_320, (alng - blng) * 111_320 * math.cos(math.radians(alat)))


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


# ---------- labels ----------
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


# ---------- business flavor ----------
DENY_KEYS = ("healthcare", "office")
DENY = {
    "amenity": {"pharmacy", "hospital", "clinic", "doctors", "dentist", "veterinary",
                "nursing_home", "social_facility", "childcare", "kindergarten", "school",
                "college", "university", "place_of_worship", "funeral_hall", "crematorium",
                "grave_yard", "police", "fire_station", "prison", "courthouse",
                "ranger_station", "atm", "bank", "bureau_de_change", "parking",
                "parking_space", "parking_entrance", "motorcycle_parking", "bicycle_parking",
                "fuel", "charging_station", "car_wash", "car_rental", "car_sharing", "taxi",
                "bus_station", "bench", "toilets", "shower", "drinking_water", "fountain",
                "clock", "vending_machine", "recycling", "waste_basket", "waste_disposal",
                "telephone", "post_box", "post_office", "shelter", "hunting_stand",
                "fire_hydrant"},
    "shop": {"chemist", "medical_supply", "optician", "hearing_aids", "funeral_directors",
             "erotic", "weapons", "bail_bond", "car", "car_repair", "tyres", "car_parts",
             "vacant", "storage_rental"},
    "leisure": {"playground", "pitch", "track", "fitness_station", "picnic_table", "firepit"},
    "tourism": {"information", "artwork"},
}


def phrase(key, val, n):
    m = {
        ("amenity", "cafe"): f"possibly stopped for coffee at {n}",
        ("amenity", "bar"): f"perhaps a drink at {n}",
        ("amenity", "pub"): f"perhaps a pint at {n}",
        ("amenity", "biergarten"): f"perhaps a pint at {n}",
        ("amenity", "restaurant"): f"possibly a bite at {n}",
        ("amenity", "fast_food"): f"maybe a quick bite at {n}",
        ("amenity", "food_court"): f"grazing the food court at {n}",
        ("amenity", "ice_cream"): f"maybe an ice cream at {n}",
        ("amenity", "cinema"): f"possibly catching a film at {n}",
        ("amenity", "theatre"): f"maybe taking in a show at {n}",
        ("amenity", "nightclub"): f"out late at {n}",
        ("amenity", "library"): f"maybe browsing the shelves at {n}",
        ("amenity", "marketplace"): f"wandering the market at {n}",
        ("amenity", "fitness_centre"): f"maybe a workout at {n}",
        ("shop", "books"): f"maybe browsing {n}",
        ("shop", "coffee"): f"possibly stopped for coffee at {n}",
        ("shop", "bakery"): f"maybe grabbed a pastry at {n}",
        ("shop", "department_store"): f"some retail therapy at {n}",
        ("shop", "mall"): f"wandering the mall at {n}",
        ("shop", "supermarket"): f"a grocery run at {n}",
        ("shop", "greengrocer"): f"a grocery run at {n}",
        ("shop", "convenience"): f"a corner-store run at {n}",
        ("shop", "clothes"): f"possibly shopping at {n}",
        ("shop", "boutique"): f"possibly shopping at {n}",
        ("shop", "shoes"): f"trying on shoes at {n}",
        ("leisure", "park"): f"taking a stroll through {n}",
        ("leisure", "garden"): f"taking a stroll through {n}",
        ("leisure", "dog_park"): f"at the dog park, {n}",
        ("leisure", "sports_centre"): f"maybe a workout at {n}",
        ("leisure", "fitness_centre"): f"maybe a workout at {n}",
        ("leisure", "swimming_pool"): f"maybe a swim at {n}",
        ("leisure", "stadium"): f"catching something at {n}",
        ("tourism", "museum"): f"possibly at the museum, {n}",
        ("tourism", "gallery"): f"taking in art at {n}",
        ("tourism", "attraction"): f"sightseeing at {n}",
        ("tourism", "viewpoint"): f"enjoying the view at {n}",
        ("tourism", "hotel"): f"lying low near {n}",
        ("tourism", "hostel"): f"lying low near {n}",
    }
    if (key, val) in m:
        return m[(key, val)]
    if key == "shop":
        return f"maybe browsing {n}"
    return f"seen loitering near {n}"


def parse_element(el):
    tags = el.get("tags") or {}
    name = tags.get("name")
    if not name or any(k in tags for k in DENY_KEYS):
        return None
    key = next((k for k in ("amenity", "shop", "leisure", "tourism") if k in tags), None)
    if key is None:
        return None
    val = tags.get(key)
    if val in DENY.get(key, ()):
        return None
    c = el.get("center") or el
    lat, lng = c.get("lat"), c.get("lon")
    if lat is None or lng is None:
        return None
    return (lat, lng, key, val, name)


def overpass_bbox(s, w, n, e):
    """One region query, rotating across mirrors with backoff. Returns (pois, ok)."""
    q = (
        "[out:json][timeout:90];("
        f"nwr[amenity][name]({s},{w},{n},{e});"
        f"nwr[shop][name]({s},{w},{n},{e});"
        f"nwr[leisure][name]({s},{w},{n},{e});"
        f"nwr[tourism][name]({s},{w},{n},{e});"
        ");out center 4000;"
    )
    data = urllib.parse.urlencode({"data": q}).encode()
    for attempt in range(6):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        try:
            j = http_json(url, timeout=120, data=data)
            return [p for p in (parse_element(el) for el in j.get("elements", [])) if p], True
        except Exception as ex:
            wait = 4 * (attempt + 1)
            print(f"  overpass busy ({url.split('/')[2]}: {ex}); trying another mirror in {wait}s", file=sys.stderr)
            time.sleep(wait)
    print("  overpass: all mirrors busy this round (will retry on the next migrate)", file=sys.stderr)
    return [], False


def build_poi_index(coords):
    """Fetch POIs for the regions these points touch. Returns (pois, ok_regions)."""
    regions = sorted({region_key(lat, lng) for lat, lng in coords})
    print(f"Business lookup across {len(regions)} region(s)...")
    pois, ok_regions = [], set()
    for i, (ry, rx) in enumerate(regions):
        clat, clng = ry * POI_REGION, rx * POI_REGION
        pad = 0.006
        s, w = clat - POI_REGION / 2 - pad, clng - POI_REGION / 2 - pad
        n, e = clat + POI_REGION / 2 + pad, clng + POI_REGION / 2 + pad
        got, ok = overpass_bbox(s, w, n, e)
        if ok:
            ok_regions.add((ry, rx))
            pois.extend(got)
        print(f"  region {i + 1}/{len(regions)}: {'ok' if ok else 'skipped'}, +{len(got)} places (total {len(pois)})")
        if i < len(regions) - 1:
            time.sleep(3)
    return pois, ok_regions


def nearest_flavor(lat, lng, pois):
    best = None
    for (plat, plng, key, val, name) in pois:
        d = meters(lat, lng, plat, plng)
        if d > 400:
            continue
        if best is None or d < best[0]:
            best = (d, key, val, name)
    if not best:
        return None
    _, key, val, name = best
    return phrase(key, val, name)


# ---------- enrich ----------
def enrich(dlat, dlng, ts, cache, pois, ok_regions):
    key = cell_of(dlat, dlng)
    c = cache.get(key, {})
    if "street" not in c:
        c["street"], c["hood"] = reverse_geocode(dlat, dlng)
        time.sleep(1.1)  # Nominatim: <= 1 req/sec
    checked = "flavor" in c
    if not checked and region_key(dlat, dlng) in ok_regions:
        c["flavor"] = nearest_flavor(dlat, dlng, pois)  # may be None (checked, none nearby)
        checked = True
    cache[key] = c
    return {"ts": ts, "lat": round(dlat, 5), "lng": round(dlng, 5),
            "street": c.get("street"), "hood": c.get("hood"),
            "flavor": c.get("flavor"), "snapped": True, "flavor_checked": checked}


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


# ---------- tile ----------
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


def clean_cache(cache):
    return {k: v for k, v in cache.items() if CELL_KEY_RE.match(k)}


# ---------- main ----------
def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MODE", "poll")).lower()

    if mode == "migrate":
        existing = load_json(DATA, [])
        cache = clean_cache(load_json(CACHE, {}))  # keep known results, drop old precise keys
        done, targets = [], []
        for p in existing:
            if p.get("snapped") and p.get("flavor_checked"):
                done.append(p)  # already complete — no lookups needed
                continue
            try:
                if p.get("snapped"):
                    dlat, dlng = float(p["lat"]), float(p["lng"])
                else:
                    dlat, dlng = snap(float(p["lat"]), float(p["lng"]))
                if p.get("ts"):
                    targets.append((dlat, dlng, p["ts"]))
            except (KeyError, TypeError, ValueError):
                continue
        print(f"Migrating: {len(done)} already complete, {len(targets)} to look up...")
        pois, ok = build_poi_index([(a, b) for a, b, _ in targets]) if targets else ([], set())
        processed = [enrich(a, b, ts, cache, pois, ok) for a, b, ts in targets]
        merged, _ = merge(done, processed)
        pending = sum(1 for p in merged if not p.get("flavor_checked"))
        save_json(CACHE, clean_cache(cache))
        save_json(DATA, merged)
        print(f"Done. {len(merged)} points; {pending} still awaiting a business lookup.")
        if pending:
            print(f"Re-run 'migrate' anytime to fill the remaining {pending} (it only redoes those).")
        return

    raw = asyncio.run(get_tile_points(mode))
    snapped = [(*snap(p["lat"], p["lng"]), p["ts"]) for p in raw if p.get("ts")]
    cache = clean_cache(load_json(CACHE, {}))
    pois, ok = build_poi_index([(a, b) for a, b, _ in snapped]) if snapped else ([], set())
    out = [enrich(a, b, ts, cache, pois, ok) for a, b, ts in snapped]
    existing = load_json(DATA, [])
    merged, added = merge(existing, out)
    save_json(CACHE, clean_cache(cache))
    save_json(DATA, merged)
    print(f"Done. {added} new point(s). wallet.json now holds {len(merged)} total.")


if __name__ == "__main__":
    main()
