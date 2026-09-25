#!/usr/bin/env python3
"""Build the simulated fleet's road network from REAL PMPML routes in OpenStreetMap.

Hand-drawn waypoints were good enough to prove the confirmation logic, but they are not
Pune: they cut corners, ignore one-ways, and their overlaps were arranged by hand. OSM
carries the actual PMPML route relations (43A, 77, 81, 94, 100C, 114, 124, 208, 256,
283, 333 ...), so the simulation can drive the streets those buses really drive, and the
route overlaps that confirmation depends on become a property of the city rather than
something the simulation author arranged.

    uv run python edge/scripts/fetch_routes.py [--cache overpass.json] [--osm-only]

Writes edge/uip_edge/data/pune_routes.json. Checked in, so the generator has no runtime
network dependency - rerun this only to refresh the geometry.

Two things OSM will not give us:
  * Route relations are collections of ways in no guaranteed order or direction, and
    some are incomplete. They have to be stitched end-to-end and the broken ones dropped.
  * Coverage. OSM has 31 PMPML refs inside the bbox and PMPML runs several hundred;
    19 of the 31 are untraced stubs (one to eighteen ways, under 7 km), and of the 12
    usable ones several stop far short of the terminus they are named after - the 43A
    relation is tagged Katraj => Hinjawadi but its ways end 15.6 km short of Katraj.
    What survives covers Hinjawadi, Wakad, Baner, Aundh and the Shivajinagar-Pune
    Station spine, and leaves the whole south, east and north of the city empty.

The SYNTHETIC routes below fill those holes: each is a plausible corridor routed over
the real road network with OSRM, tagged `source: "osrm"` so nothing downstream can
mistake it for a documented PMPML service. They are the reason the simulated fleet
reaches Katraj, Hadapsar, Kharadi and Nigdi at all.
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
import time
import urllib.parse
import urllib.request

BBOX = (18.38, 73.62, 18.75, 74.05)          # south, west, north, east
OVERPASS = "https://overpass-api.de/api/interpreter"
OSRM = "https://router.project-osrm.org/route/v1/driving"
OUT = pathlib.Path(__file__).resolve().parent.parent / "uip_edge" / "data" / "pune_routes.json"

#: OSM spells the operator both ways; matching only "PMPML" silently drops 19 routes
PMPML_NAMES = ("PMPML", "Pune Mahanagar Parivahan")
#: ways whose endpoints are further apart than this do not belong to the same chain
JOIN_TOLERANCE_M = 120.0
#: drop routes shorter than this - the OSM relation is a stub, not a usable route
MIN_ROUTE_KM = 7.0
#: Douglas-Peucker tolerance. A bus that cuts a 10 m corner is well inside GPS noise.
SIMPLIFY_M = 8.0

#: Corridors OSM has no usable relation for, routed with OSRM through these waypoints.
#: Not documented services - plausible feeders, and marked as such in the output.
SYNTHETIC: tuple[dict, ...] = (
    {"id": "SIT-LAVALE", "ref": "SIT",
     "name": "Symbiosis Institute of Technology (Lavale) => Pune Station",
     "note": "campus feeder - no PMPML relation serves Lavale",
     "via": [(18.5412214, 73.7274563),   # Symbiosis Institute of Technology, Lavale
             (18.5590, 73.7770),         # Baner
             (18.5308, 73.8470),         # Shivajinagar
             (18.5285, 73.8743)]},       # Pune Station
    {"id": "SYN-KATRAJ", "ref": "SYN-S",
     "name": "Katraj => Swargate => Pune Station",
     "note": "the south corridor - OSM's 43A and 77 relations are named for it but "
             "their ways stop ~15 km short",
     "via": [(18.4479, 73.8567),         # Katraj Chowk
             (18.4730, 73.8630),         # Bibwewadi
             (18.5018, 73.8586),         # Swargate
             (18.5285, 73.8743)]},       # Pune Station
    {"id": "SYN-KHARADI", "ref": "SYN-E",
     "name": "Pune Station => Hadapsar => Kharadi",
     "note": "the east corridor - no traced relation reaches Hadapsar or Kharadi",
     "via": [(18.5285, 73.8743),         # Pune Station
             (18.5150, 73.8960),         # Ghorpadi
             (18.5089, 73.9260),         # Hadapsar
             (18.5515, 73.9470)]},       # Kharadi
    {"id": "SYN-NIGDI", "ref": "SYN-N",
     "name": "Shivajinagar => Pimpri => Nigdi",
     "note": "the north corridor into Pimpri-Chinchwad",
     "via": [(18.5308, 73.8470),         # Shivajinagar
             (18.5620, 73.8330),         # Bopodi
             (18.6298, 73.7997),         # Pimpri
             (18.6511, 73.7640)]},       # Nigdi
)

QUERY = f"""
[out:json][timeout:180];
relation["type"="route"]["route"="bus"]({BBOX[0]},{BBOX[1]},{BBOX[2]},{BBOX[3]});
out geom;
"""


# -- geometry -------------------------------------------------------------------------

def haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000.0 * math.asin(math.sqrt(h))


def length_m(pts: list[tuple[float, float]]) -> float:
    return sum(haversine(a, b) for a, b in zip(pts, pts[1:]))


def _perp_m(p, a, b) -> float:
    """Distance from p to segment ab, in metres, via a local flat approximation."""
    k = math.cos(math.radians(a[0]))
    ax, ay = 0.0, 0.0
    bx, by = (b[1] - a[1]) * k * 111_320.0, (b[0] - a[0]) * 110_570.0
    px, py = (p[1] - a[1]) * k * 111_320.0, (p[0] - a[0]) * 110_570.0
    seg = bx * bx + by * by
    t = 0.0 if seg == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg))
    return math.hypot(px - bx * t, py - by * t)


def simplify(pts: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Douglas-Peucker, iterative so a 5 000-point way cannot blow the stack."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        far, far_d = i, -1.0
        for k in range(i + 1, j):
            d = _perp_m(pts[k], pts[i], pts[j])
            if d > far_d:
                far, far_d = k, d
        if far_d > eps:
            keep[far] = True
            stack += [(i, far), (far, j)]
    return [p for p, k in zip(pts, keep) if k]


def stitch(ways: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    """Chain ways end-to-end, flipping as needed; return the longest chain found.

    A route relation is a *set* of ways. Editors add them in whatever order, and a way's
    own direction has nothing to do with the direction of travel, so the geometry has to
    be reassembled rather than concatenated. Where the relation is incomplete the chain
    simply ends, which is how the broken routes get detected and dropped.
    """
    remaining = [w for w in ways if len(w) >= 2]
    best: list[tuple[float, float]] = []
    while remaining:
        chain = remaining.pop(0)
        while remaining:
            pick = None
            for i, w in enumerate(remaining):
                for flip in (False, True):
                    pts = w[::-1] if flip else w
                    for d, where in ((haversine(chain[-1], pts[0]), "append"),
                                     (haversine(chain[0], pts[-1]), "prepend")):
                        if pick is None or d < pick[0]:
                            pick = (d, i, flip, where)
            d, i, flip, where = pick
            if d > JOIN_TOLERANCE_M:
                break
            pts = remaining.pop(i)
            pts = pts[::-1] if flip else pts
            chain = chain + pts[1:] if where == "append" else pts[:-1] + chain
        if length_m(chain) > length_m(best):
            best = chain
    return best


# -- sources --------------------------------------------------------------------------

def fetch_overpass(cache: pathlib.Path | None) -> dict:
    if cache and cache.exists():
        print(f"using cached Overpass response {cache}")
        return json.loads(cache.read_text())
    print("querying Overpass ...")
    for attempt in range(4):
        req = urllib.request.Request(OVERPASS, data=QUERY.encode(),
                                     headers={"User-Agent": "uip-prototype/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                body = r.read()
            data = json.loads(body)
        except Exception as exc:                       # rate limit answers with HTML
            print(f"  attempt {attempt + 1} failed ({exc}); waiting")
            time.sleep(30)
            continue
        if cache:
            cache.write_text(json.dumps(data))
        return data
    sys.exit("Overpass did not answer - retry later, or pass --cache with a saved response")


def osrm_route(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    url = f"{OSRM}/{urllib.parse.quote(coords)}?overview=full&geometries=geojson"
    with urllib.request.urlopen(url, timeout=90) as r:
        data = json.loads(r.read())
    if data.get("code") != "Ok":
        sys.exit(f"OSRM refused: {data.get('code')}")
    return [(lat, lon) for lon, lat in data["routes"][0]["geometry"]["coordinates"]]


# -- assembly -------------------------------------------------------------------------

def build(data: dict, with_synthetic: bool) -> list[dict]:
    candidates: dict[str, dict] = {}
    for rel in data["elements"]:
        tags = rel.get("tags", {})
        ref = (tags.get("ref") or "").strip()
        who = tags.get("operator", "") + tags.get("network", "")
        if not ref or not any(n in who for n in PMPML_NAMES):
            continue
        ways = [m["geometry"] for m in rel.get("members", [])
                if m["type"] == "way" and m.get("geometry") and not m.get("role")]
        pts = stitch([[(p["lat"], p["lon"]) for p in w] for w in ways])
        km = length_m(pts) / 1000.0
        if km < MIN_ROUTE_KM:
            continue
        # both directions of a route are separate relations; the simulated bus drives
        # there and back, so keep whichever direction OSM has mapped more completely
        if ref not in candidates or km > candidates[ref]["km"]:
            candidates[ref] = {"ref": ref, "name": tags.get("name", ""), "km": km,
                               "points": simplify(pts, SIMPLIFY_M)}

    routes = [{"id": f"PMPML-{c['ref']}", "ref": c["ref"], "name": c["name"],
               "source": "osm", "points": [[round(a, 6), round(b, 6)] for a, b in c["points"]]}
              for c in sorted(candidates.values(), key=lambda c: -c["km"])]

    for spec in (SYNTHETIC if with_synthetic else ()):
        print(f"routing {spec['id']} with OSRM ...")
        pts = simplify(osrm_route(spec["via"]), SIMPLIFY_M)
        routes.append({
            "id": spec["id"], "ref": spec["ref"], "name": spec["name"],
            "source": "osrm",
            "note": f"not a documented PMPML service - {spec['note']}, routed over "
                    f"the real road network",
            "points": [[round(a, 6), round(b, 6)] for a, b in pts],
        })
        time.sleep(1.0)                    # the public OSRM demo server is rate limited
    return routes


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=pathlib.Path, help="reuse/save the Overpass response")
    ap.add_argument("--osm-only", action="store_true",
                    help="skip the OSRM-routed corridors, keeping only real relations")
    args = ap.parse_args()

    routes = build(fetch_overpass(args.cache), not args.osm_only)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "generated_by": "edge/scripts/fetch_routes.py",
        "source": "OpenStreetMap contributors (ODbL); campus leg routed with OSRM",
        "routes": routes,
    }, indent=1))

    print(f"\n{len(routes)} routes -> {OUT}")
    for r in routes:
        pts = [(a, b) for a, b in r["points"]]
        print(f"  {r['id']:<14} {length_m(pts) / 1000:6.1f} km {len(pts):>4} pts  {r['name'][:52]}")


if __name__ == "__main__":
    main()
