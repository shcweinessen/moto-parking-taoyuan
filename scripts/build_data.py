#!/usr/bin/env python3
"""Build data/segments.geojson and data/osm_moto.geojson.

1. Download Taoyuan curb-parking segments (XML, no coordinates).
2. Keep motorcycle segments, parse fare rules.
3. Resolve geometry from OpenStreetMap roads: intersect the main road with the
   from/to cross streets, then cut the road between those two points.
   Results are cached in data/geocache.json so only new segments hit Overpass.
4. Fetch OSM amenity=motorcycle_parking points.
"""
import heapq
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CACHE = DATA / "geocache.json"
SOURCE_URL = ("https://opendata.tycg.gov.tw/api/dataset/ffd3180e-75b0-419d-b680-2286bade7958"
              "/resource/99a8b764-8407-4c00-9e6c-57dc8452ecce/download")
LOTS_URL = ("https://opendata.tycg.gov.tw/api/dataset/f4cc0b12-86ac-40f9-8745-885bddc18f79"
            "/resource/0daad6e6-0632-44f5-bd25-5e1de1e9146f/download")
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter"]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = "moto-parking-taoyuan/1.0 (github pages; open data)"
GEOCODER_VERSION = 2  # bump to invalidate cache when the algorithm changes

DAY_NAMES = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}


def http(url, data=None, timeout=240):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def overpass(query):
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(3):
        for url in OVERPASS:
            try:
                res = json.loads(http(url, body))
                if "runtime error" in res.get("remark", "") or "timed out" in res.get("remark", ""):
                    raise RuntimeError(res["remark"][:120])
                return res
            except Exception as e:  # noqa: BLE001 - try the next mirror
                last = e
                print(f"  overpass retry ({url}): {e}", file=sys.stderr, flush=True)
                time.sleep(20 * (attempt + 1))
    raise RuntimeError(f"Overpass failed: {last}")


# ---------- fare parsing ----------

def parse_fares(text):
    rules = []
    for part in re.split(r"[,，]\s*(?=\S*車格)", text or ""):
        part = part.strip(" ,，")
        if not part.startswith("機車"):
            continue
        m = re.search(r"週([一二三四五六日])(?:至週([一二三四五六日]))?\s*(\d\d:\d\d)~(\d\d:\d\d)收費\s*(.*)$", part)
        if not m:
            rules.append({"days": list(range(1, 8)), "start": "00:00", "end": "24:00", "price": part, "free": False})
            continue
        d1 = DAY_NAMES[m.group(1)]
        d2 = DAY_NAMES[m.group(2)] if m.group(2) else d1
        days = list(range(d1, d2 + 1)) if d1 <= d2 else list(range(d1, 8)) + list(range(1, d2 + 1))
        price = m.group(5).strip(" ,，")
        rules.append({"days": days, "start": m.group(3), "end": m.group(4), "price": price,
                      "free": bool(re.match(r"^0元", price))})
    return rules


def parse_description(desc):
    """'中央東路1(機車)(元化路-延平路)' -> ('中央東路', '元化路', '延平路', label)"""
    desc = desc.replace("（", "(").replace("）", ")")
    m = re.match(r"^(.*)\(([^()]*)\)\s*$", desc)
    head, span = (m.group(1), m.group(2)) if m else (desc, "")
    label = re.sub(r"\([^()]*\)", "", head).strip()
    road = re.sub(r"\d+$", "", label).strip()
    road = re.sub(r"([路街])[一二三]$", r"\1", road)  # 中山東路一 -> 中山東路
    a, _, b = span.partition("-")
    return road, a.strip(), b.strip(), label


# ---------- geometry ----------

def dist_m(p, q):
    lat = math.radians((p[1] + q[1]) / 2)
    dx = (q[0] - p[0]) * 111320 * math.cos(lat)
    dy = (q[1] - p[1]) * 110540
    return math.hypot(dx, dy)


def load_city():
    """All named roads in Taoyuan, split by district (2 Overpass requests total)."""
    city = 'area["name"="桃園市"]["admin_level"="4"]->.c;'
    local = os.environ.get("OSM_CACHE")  # optional local cache for development runs
    if local and Path(local).exists():
        roads, rels = json.loads(Path(local).read_text())
    else:
        roads = overpass(f'[out:json][timeout:300];{city}way(area.c)["highway"]["name"];out body geom;')["elements"]
        rels = overpass(f'[out:json][timeout:180];{city}rel(area.c)["boundary"="administrative"]["admin_level"="7"];'
                        f'out geom;')["elements"]
        if len(roads) < 10000 or len(rels) < 13:
            raise RuntimeError(f"Overpass returned too little data ({len(roads)} roads, {len(rels)} districts)")
        if local:
            Path(local).write_text(json.dumps([roads, rels]))
    borders = {}
    for r in rels:
        edges = []
        for m in r.get("members", []):
            if m.get("type") == "way" and m.get("role") in ("outer", "") and m.get("geometry"):
                g = [(p["lon"], p["lat"]) for p in m["geometry"]]
                edges += list(zip(g, g[1:]))
        borders[r["tags"]["name"]] = edges
    print(f"  {len(roads)} ways, {len(borders)} districts", file=sys.stderr, flush=True)

    def inside(pt, edges):
        x, y, odd = pt[0], pt[1], False
        for (x1, y1), (x2, y2) in edges:
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                odd = not odd
        return odd

    by_town = {name: [] for name in borders}
    for w in roads:
        if w.get("type") != "way" or "geometry" not in w:
            continue
        g = w["geometry"]
        probes = {(g[i]["lon"], g[i]["lat"]) for i in (0, len(g) // 2, -1)}
        for name, edges in borders.items():
            if any(inside(p, edges) for p in probes):
                by_town[name].append(w)
    return by_town


class District:
    def __init__(self, name, ways):
        self.name = name
        self.ways = ways
        self.by_name = {}
        for w in self.ways:
            for key in ("name", "alt_name", "old_name"):
                for n in (w["tags"].get(key) or "").split(";"):
                    if n:
                        self.by_name.setdefault(n.strip(), []).append(w)
        print(f"  {name}: {len(self.ways)} ways", file=sys.stderr)

    def road(self, name):
        """Ways for a road name; tolerate 段 suffix mismatches."""
        if not name:
            return []
        if name in self.by_name:
            return self.by_name[name]
        base = re.sub(r"[一二三四五六七八九十]+段$", "", name)
        hits = [w for n, ws in self.by_name.items() if n == base or n.startswith(base) and n[len(base):].endswith("段")
                for w in ws]
        if hits:
            return hits
        return [w for n, ws in self.by_name.items() if len(name) >= 2 and (n.startswith(name) or name.startswith(n) and len(n) >= 3)
                for w in ws]


def intersections(ways_a, ways_b, tol=45):
    """Points where two roads meet: shared nodes, else close approaches."""
    if not ways_a or not ways_b:
        return []
    nodes_b = {}
    for w in ways_b:
        for nid, g in zip(w.get("nodes", []), w["geometry"]):
            nodes_b[nid] = (g["lon"], g["lat"])
    pts = []
    for w in ways_a:
        for nid in w.get("nodes", []):
            if nid in nodes_b:
                pts.append(nodes_b[nid])
    if not pts:
        # grid-bucket road B's vertices (~0.0005 deg ≈ 50 m) so each lookup is local
        cell = 0.0005
        grid = {}
        for w in ways_b:
            for g in w["geometry"]:
                q = (g["lon"], g["lat"])
                grid.setdefault((int(q[0] / cell), int(q[1] / cell)), []).append(q)
        for w in ways_a:
            for g in w["geometry"]:
                p = (g["lon"], g["lat"])
                cx, cy = int(p[0] / cell), int(p[1] / cell)
                near = [q for dx in (-1, 0, 1) for dy in (-1, 0, 1) for q in grid.get((cx + dx, cy + dy), [])]
                if not near:
                    continue
                best = min(near, key=lambda q: dist_m(p, q))
                if dist_m(p, best) < tol:
                    pts.append(((p[0] + best[0]) / 2, (p[1] + best[1]) / 2))
    # merge points within 60 m
    out = []
    for p in pts:
        if all(dist_m(p, q) > 60 for q in out):
            out.append(p)
    return out


def path_along(ways, p1, p2):
    """Shortest path along the road's ways between the nodes nearest p1 and p2."""
    graph, coord = {}, {}
    for w in ways:
        ids, geom = w.get("nodes", []), w["geometry"]
        for i, g in enumerate(geom):
            coord[ids[i]] = (g["lon"], g["lat"])
        for i in range(len(ids) - 1):
            d = dist_m(coord[ids[i]], coord[ids[i + 1]])
            graph.setdefault(ids[i], []).append((ids[i + 1], d))
            graph.setdefault(ids[i + 1], []).append((ids[i], d))
    if not coord:
        return None
    s = min(coord, key=lambda n: dist_m(coord[n], p1))
    t = min(coord, key=lambda n: dist_m(coord[n], p2))
    if dist_m(coord[s], p1) > 80 or dist_m(coord[t], p2) > 80:
        return None
    dist, prev, pq = {s: 0}, {}, [(0, s)]
    while pq:
        d, n = heapq.heappop(pq)
        if n == t:
            break
        if d > dist.get(n, 1e18):
            continue
        for m, w in graph.get(n, []):
            nd = d + w
            if nd < dist.get(m, 1e18):
                dist[m], prev[m] = nd, n
                heapq.heappush(pq, (nd, m))
    if t not in dist:
        return None
    seq, n = [t], t
    while n != s:
        n = prev[n]
        seq.append(n)
    line = [coord[n] for n in reversed(seq)]
    if dist[t] > 3 * max(dist_m(p1, p2), 50):
        return None
    return line


def corridor_line(ways, p1, p2, width=60, bins=24):
    """Fallback when the road graph is disconnected: average road vertices along p1->p2."""
    lat = math.radians((p1[1] + p2[1]) / 2)
    kx, ky = 111320 * math.cos(lat), 110540
    ax, ay = 0.0, 0.0
    bx, by = (p2[0] - p1[0]) * kx, (p2[1] - p1[1]) * ky
    L2 = bx * bx + by * by
    if L2 == 0:
        return None
    buckets = {}
    for w in ways:
        for g in w["geometry"]:
            x, y = (g["lon"] - p1[0]) * kx, (g["lat"] - p1[1]) * ky
            t = (x * bx + y * by) / L2
            if not 0 < t < 1:
                continue
            off = abs(x * by - y * bx) / math.sqrt(L2)
            if off <= width:
                buckets.setdefault(min(int(t * bins), bins - 1), []).append((g["lon"], g["lat"]))
    if len(buckets) < bins // 3:
        return None
    mid = [(sum(q[0] for q in v) / len(v), sum(q[1] for q in v) / len(v)) for _, v in sorted(buckets.items())]
    return [p1] + mid + [p2]


_last_nominatim = [0.0]


def nominatim(q):
    wait = 1.1 - (time.time() - _last_nominatim[0])
    if wait > 0:
        time.sleep(wait)
    _last_nominatim[0] = time.time()
    url = NOMINATIM + "?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "countrycodes": "tw"})
    try:
        res = json.loads(http(url, timeout=30))
    except Exception:  # noqa: BLE001
        return None
    return (float(res[0]["lon"]), float(res[0]["lat"])) if res else None


def endpoint_candidates(dist, road_ways, text, town):
    """Possible coordinates for one end of a segment."""
    if not text:
        return []
    m = re.match(r"^(.+?)與(.+?)口$", text)
    if m:
        return intersections(dist.road(m.group(1)), dist.road(m.group(2)))
    if "號" in text:
        p = nominatim(f"桃園市{town}{text}")
        return [p] if p else []
    cross = dist.road(text)
    if cross and road_ways:
        pts = intersections(road_ways, cross)
        if pts:
            return pts
    return []


def nearest_approach(road_ways, cross_ways, near=None, tol=300):
    """Vertex of the road closest to the cross street (for T-junction offsets / dead ends)."""
    if not road_ways or not cross_ways:
        return None
    cell = 0.003
    grid = {}
    for w in cross_ways:
        for g in w["geometry"]:
            q = (g["lon"], g["lat"])
            grid.setdefault((int(q[0] / cell), int(q[1] / cell)), []).append(q)
    best, best_d = None, tol
    for w in road_ways:
        for g in w["geometry"]:
            p = (g["lon"], g["lat"])
            if near and dist_m(p, near) > 2500:
                continue
            cx, cy = int(p[0] / cell), int(p[1] / cell)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for q in grid.get((cx + dx, cy + dy), []):
                        d = dist_m(p, q)
                        if d < best_d:
                            best, best_d = p, d
    return best


def point(p, method):
    return {"type": "Point", "coordinates": [round(p[0], 6), round(p[1], 6)]}, method


def geocode(dist, seg):
    road, a, b = seg["road"], seg["from"], seg["to"]
    town = seg["town"]
    road_ways = dist.road(road)
    ca = endpoint_candidates(dist, road_ways, a, town)
    cb = ca if b == a else endpoint_candidates(dist, road_ways, b, town)
    # one end missing: use where the cross street comes closest to the main road
    if ca and not cb and b and b != a:
        p = nearest_approach(road_ways, dist.road(b), near=ca[0])
        cb = [p] if p else []
    if cb and not ca and a and b != a:
        p = nearest_approach(road_ways, dist.road(a), near=cb[0])
        ca = [p] if p else []
    if not ca and not cb and road_ways and a and b and a != b:
        pa = nearest_approach(road_ways, dist.road(a))
        pb = nearest_approach(road_ways, dist.road(b), near=pa) if pa else None
        if pa and pb:
            ca, cb = [pa], [pb]
    if ca and cb:
        p1, p2 = min(((p, q) for p in ca for q in cb), key=lambda pq: dist_m(*pq) if pq[0] != pq[1] else 0)
        if dist_m(p1, p2) < 15:
            return {"type": "Point", "coordinates": [round(p1[0], 6), round(p1[1], 6)]}, "intersection"
        if dist_m(p1, p2) < 2500:
            line = path_along(road_ways, p1, p2) if road_ways else None
            if not line and road_ways:
                line = corridor_line(road_ways, p1, p2)
            if not line:
                line = [p1, p2]
            return {"type": "LineString", "coordinates": [[round(x, 6), round(y, 6)] for x, y in line]}, "road"
    one = (ca or cb)
    if one:
        p = one[0]
        return {"type": "Point", "coordinates": [round(p[0], 6), round(p[1], 6)]}, "one-end"
    if not road_ways and a and b and a != b:
        # landmark name (e.g. 青埔棒壘球場): spot between the two bounding streets
        wa, wb = dist.road(a), dist.road(b)
        hit = intersections(wa, wb) or intersections(wa, wb, tol=400)
        if hit:
            return point(hit[0], "between")
        pa = nearest_approach(wa, wb, tol=600)
        if pa:
            return point(pa, "between")
    p = nominatim(f"桃園市{town}{seg['label']}") or nominatim(f"桃園市{town}{road}")
    if p:
        return {"type": "Point", "coordinates": [round(p[0], 6), round(p[1], 6)]}, "nominatim"
    if road_ways:
        g = road_ways[len(road_ways) // 2]["geometry"]
        mid = g[len(g) // 2]
        return {"type": "Point", "coordinates": [round(mid["lon"], 6), round(mid["lat"], 6)]}, "road-mid"
    return None, "failed"


VEH = re.compile(r"(機車|小型車|大型車|汽車)")


def parse_lot_guide(guide):
    """Motorcycle fees from a free-text payGuide: temp/monthly prices and any daily cap."""
    text = re.sub(r"\s+", " ", guide or "")
    clauses, veh = [], None
    for part in re.split(r"[，,;；。、\n]|(?=(?:機車|小型車|大型車|汽車))", text):
        part = part.strip(" :：")
        if not part:
            continue
        m = VEH.match(part) or VEH.search(part)
        if m:
            veh = "moto" if m.group(1) == "機車" else "car"
        if veh == "moto" and re.search(r"\d|不收費|免費", part):
            clauses.append(part)
    daily = "隔日另計" in text  # "per entry, a new day is charged again"
    temp, month, cap = [], [], []
    for c in clauses:
        body = re.sub(r"^機車\s*[-:：]?\s*", "", c)
        when = "平日" if "平日" in c else "假日" if "假日" in c else ""
        if re.search(r"不收費|免費|:0/次", c):
            temp.append("免費")
            continue
        n = re.search(r"(\d+)", body)
        if not n:
            continue
        price = int(n.group(1))
        if re.search(r"月租|/月|季繳", body):
            month.append(f"{price} 元／{'季' if '季繳' in body else '月'}")
            continue
        if re.search(r"上限|最高", body):
            cap.append(f"{when} {price} 元")
            continue
        if re.search(r"/日|當日|一天一次|/當次/日", body) or daily and not re.search(r"/時|/H|/小時", body, re.I):
            temp.append(f"{when} {price} 元／次")
            cap.append(f"{when} 當日一次 {price} 元，不再加收")
        elif re.search(r"/時|元/H|/小時", body, re.I):
            temp.append(f"{when} {price} 元／小時")
        else:  # 次 / 計次 / bare number
            temp.append(f"{when} {price} 元／次")
    return {"temp": [t.strip() for t in dict.fromkeys(temp)], "month": list(dict.fromkeys(month)),
            "cap": [c.strip() for c in dict.fromkeys(cap)], "free": bool(temp) and all(t == "免費" for t in temp)}


def build_lots():
    """Off-street lots whose fee guide mentions motorcycles."""
    root = ET.fromstring(http(LOTS_URL))
    feats = []
    for e in root.iter("parkingLot"):
        guide = e.findtext("payGuide") or ""
        if "機車" not in guide:
            continue
        try:  # the feed swaps the names: wgsX is latitude, wgsY is longitude
            lat, lon = float(e.findtext("wgsX")), float(e.findtext("wgsY"))
        except (TypeError, ValueError):
            continue
        if not (24 < lat < 26 and 120 < lon < 122):
            continue
        fee = parse_lot_guide(guide)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 6), round(lat, 6)]},
                      "properties": {"id": e.findtext("parkId") or e.findtext("parkName"),
                                     "name": e.findtext("parkName"), "town": e.findtext("areaName"),
                                     "address": e.findtext("address"),
                                     "temp": fee["temp"], "month": fee["month"], "cap": fee["cap"],
                                     "guide": re.sub(r"\s+", " ", guide).strip(), "free": fee["free"],
                                     "total": e.findtext("totalSpace") or "",
                                     "owner": "私有" if "私有" in (e.findtext("introduction") or "") else "公有"}})
    return feats


def is_straight(g):
    return bool(g) and g["type"] == "LineString" and len(g["coordinates"]) == 2


# ---------- main ----------

def main():
    DATA.mkdir(exist_ok=True)
    root = ET.fromstring(http(SOURCE_URL))
    segs = []
    for s in root:
        fare = s.findtext("FareDescription") or ""
        rules = parse_fares(fare)
        if not rules:
            continue
        desc = s.findtext("Description") or ""
        road, a, b, label = parse_description(desc)
        segs.append({"id": s.findtext("ParkingSegmentID"), "desc": desc, "label": label, "road": road,
                     "from": a, "to": b, "town": s.findtext("TownName"), "rules": rules})
    print(f"{len(segs)} motorcycle segments", file=sys.stderr)

    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if cache.get("_version") != GEOCODER_VERSION:
        cache = {"_version": GEOCODER_VERSION}
    retry = set(filter(None, os.environ.get("RETRY", "").split(",")))  # e.g. RETRY=failed,one-end
    todo = [s for s in segs if cache.get(s["id"], {}).get("desc") != s["desc"]
            or cache[s["id"]].get("method") in retry
            or "straight" in retry and is_straight(cache[s["id"]].get("geometry"))]
    districts = {}
    by_town = load_city() if todo else {}
    for s in todo:
        if s["town"] not in districts:
            districts[s["town"]] = District(s["town"], by_town.get(s["town"], []))
        geom, method = geocode(districts[s["town"]], s)
        cache[s["id"]] = {"desc": s["desc"], "geometry": geom, "method": method}
        print(f"  {s['id']} {s['desc']} -> {method}", file=sys.stderr, flush=True)
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True))

    feats = []
    for s in segs:
        g = cache[s["id"]]["geometry"]
        if not g:
            continue
        feats.append({"type": "Feature", "geometry": g, "properties": {
            "id": s["id"], "name": s["label"], "from": s["from"], "to": s["to"], "town": s["town"],
            "rules": s["rules"], "approx": cache[s["id"]]["method"] in ("one-end", "nominatim", "road-mid", "between")}})
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    (DATA / "segments.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "updated": now, "source": "桃園市政府交通局 路邊停車路段收費資訊",
         "features": feats}, ensure_ascii=False, separators=(",", ":")))

    q = ('[out:json][timeout:90];area["name"="桃園市"]["admin_level"="4"]->.a;'
         '(nwr(area.a)["amenity"="motorcycle_parking"];'
         'nwr(area.a)["amenity"="parking"]["motorcycle"~"^(yes|designated)$"];'
         'nwr(area.a)["amenity"="parking"]["capacity:motorcycle"];);out center tags;')
    try:
        elements = overpass(q)["elements"]
    except RuntimeError as e:
        print(f"OSM points skipped, keeping previous file: {e}", file=sys.stderr)
        elements = None
    lots = build_lots()
    (DATA / "lots.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "updated": now, "source": "桃園市政府交通局 路外停車資訊",
         "features": lots}, ensure_ascii=False, separators=(",", ":")))
    lot_pts = [f["geometry"]["coordinates"] for f in lots]
    pts = []
    for e in elements or []:
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if c.get("lat") is None:
            continue
        if any(dist_m((c["lon"], c["lat"]), q) < 60 for q in lot_pts):
            continue  # already listed as an official lot
        t = e.get("tags", {})
        pts.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [c["lon"], c["lat"]]},
                    "properties": {"id": f"osm-{e['type'][0]}{e['id']}", "name": t.get("name", ""),
                                   "lot": t.get("amenity") == "parking",
                                   "fee": t.get("fee", ""), "charge": t.get("charge", ""),
                                   "capacity": t.get("capacity:motorcycle") or (
                                       t.get("capacity", "") if t.get("amenity") == "motorcycle_parking" else ""),
                                   "hours": t.get("opening_hours", ""), "covered": t.get("covered", "")}})
    if elements is not None:
        (DATA / "osm_moto.geojson").write_text(json.dumps(
            {"type": "FeatureCollection", "updated": now, "source": "© OpenStreetMap contributors",
             "features": pts}, ensure_ascii=False, separators=(",", ":")))

    methods = {}
    for s in segs:
        m = cache[s["id"]]["method"]
        methods[m] = methods.get(m, 0) + 1
    print(f"segments: {len(feats)}/{len(segs)} placed {methods}; lots: {len(lots)}; osm points: {len(pts)}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
