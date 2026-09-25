"""The simulated road network: REAL PMPML bus routes, from OpenStreetMap.

Geometry comes from `edge/uip_edge/data/pune_routes.json`, built by
`edge/scripts/fetch_routes.py` from the PMPML route relations in OSM. These are the
actual services - 333 Hinjawadi-Pune Station, 208 to Bhekrai Nagar, 43A to Katraj, 124
to Infosys Phase 3 - following the streets they really follow.

That matters for more than authenticity. Confirmation needs several DISTINCT buses over
the same ground, and with real routes the overlap is a property of Pune rather than
something the simulation author arranged: seven of these routes share the Hinjawadi
corridor, six share Baner Road. Equally, the single-route stretches are real, and those
are what the repeated-pass fallback exists for.

Four routes are not from OSM, and are marked `source: "osrm"` so nothing downstream can
mistake them for documented services. OSM's PMPML coverage is thin and lopsided - 31 refs
inside the bbox, 19 of them untraced stubs, and several of the survivors stop far short of
the terminus they are named after (the 43A relation is tagged Katraj => Hinjawadi and its
ways end 15.6 km short of Katraj). Left alone that puts the entire south, east and north
of the city out of reach of the fleet, so those corridors are routed over the real road
network with OSRM instead: SYN-KATRAJ, SYN-KHARADI, SYN-NIGDI, plus the SIT-LAVALE campus
feeder. SIT-LAVALE runs a single bus, which makes it the route where a defect can never
reach the distinct-bus threshold and the repeated-pass fallback has to carry it.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

from uip_edge.geo import Polyline, haversine

DATA = pathlib.Path(__file__).with_name("data") / "pune_routes.json"

#: How many buses run each route in the simulation. Buses, not routes, are what the
#: server counts, so this is what decides which defects can reach the 3-bus threshold
#: and which have to fall back to repeated passes. Anything unlisted runs one bus.
FLEET_SIZE: dict[str, int] = {
    "PMPML-333": 3,     # the trunk: Hinjawadi to Pune Station
    "PMPML-208": 2,
    "PMPML-208B": 2,
    "PMPML-124": 2,
    "PMPML-94": 2,      # two buses: enough for waterlogging, NOT for a pothole
    "PMPML-256": 2,
    "SIT-LAVALE": 1,    # one bus, campus feeder - the repeated-pass case
    "SYN-KATRAJ": 2,    # the OSRM corridors carry two each: enough to matter on the
    "SYN-KHARADI": 2,   # map without swamping the real routes in the confirmation
    "SYN-NIGDI": 2,     # counts
}

#: how far off a route a point may sit and still count as "on" it
ON_ROUTE_TOLERANCE_M = 30.0


@dataclass(frozen=True)
class Route:
    id: str
    ref: str
    name: str
    source: str          # "osm" | "osrm"
    fleet_size: int
    polyline: Polyline = field(repr=False)

    def locate(self, lat: float, lon: float) -> float | None:
        """Distance along this route of a point, or None if the route misses it.

        Replaces the old (corridor, fraction) addressing: with real geometry there are
        no hand-named shared corridors, so a defect is a coordinate and every route that
        physically passes it finds it here.
        """
        dist, offset = self.polyline.project(lat, lon)
        return dist if offset <= ON_ROUTE_TOLERANCE_M else None


def _load() -> tuple[Route, ...]:
    blob = json.loads(DATA.read_text())
    routes = []
    for r in blob["routes"]:
        routes.append(Route(
            id=r["id"], ref=r["ref"], name=r["name"], source=r["source"],
            fleet_size=FLEET_SIZE.get(r["id"], 1),
            polyline=Polyline([(lat, lon) for lat, lon in r["points"]]),
        ))
    return tuple(routes)


ROUTES: tuple[Route, ...] = _load()

ATTRIBUTION = ("route geometry © OpenStreetMap contributors (ODbL); "
               "the four SYN-/SIT- corridors routed with OSRM")


def routes_serving(lat: float, lon: float) -> list[Route]:
    """Every route that physically passes within tolerance of a point."""
    return [r for r in ROUTES if r.locate(lat, lon) is not None]


def buses_serving(lat: float, lon: float) -> int:
    return sum(r.fleet_size for r in routes_serving(lat, lon))


# -- road load ------------------------------------------------------------------------
#
# How busy a stretch of road is, used by the simulation as the BASELINE traffic density
# before time of day and any hotspot are applied. Proxied by how many routes share the
# stretch: a corridor seven services run down is a trunk road, a stretch only the campus
# feeder touches is a lane. It is a proxy, not a measurement - but it is derived from the
# real network rather than invented, and it is what stops every ordinary road in the city
# reporting the same flat density.

#: densification step along a route when building the index
_LOAD_STEP_M = 40.0
#: two routes within this distance are treated as sharing the road
_LOAD_RADIUS_M = 60.0
#: index cell, in degrees - a shade wider than the search radius
_LOAD_CELL_DEG = 0.0015
#: route count that counts as a fully loaded trunk road
_LOAD_SATURATION = 6.0


def _cell_key(lat: float, lon: float) -> tuple[int, int]:
    return int(lat / _LOAD_CELL_DEG), int(lon / _LOAD_CELL_DEG)


def _build_load_index() -> dict[tuple[int, int], list[tuple[float, float, str]]]:
    index: dict[tuple[int, int], list[tuple[float, float, str]]] = {}
    for route in ROUTES:
        d = 0.0
        while d <= route.polyline.length:
            lat, lon, _ = route.polyline.at(d)
            index.setdefault(_cell_key(lat, lon), []).append((lat, lon, route.id))
            d += _LOAD_STEP_M
    return index


_LOAD_INDEX = _build_load_index()
_LOAD_CACHE: dict[tuple[int, int], float] = {}


def road_load(lat: float, lon: float) -> float:
    """0-1: how heavily trafficked this stretch is, from how many routes share it.

    Cached on a ~11 m grid. Buses drive the same ground over and over, so without the
    cache this is recomputed hundreds of thousands of times for the same few metres.
    """
    key = (round(lat * 10_000), round(lon * 10_000))
    hit = _LOAD_CACHE.get(key)
    if hit is not None:
        return hit
    ci, cj = _cell_key(lat, lon)
    near: set[str] = set()
    for i in range(ci - 1, ci + 2):
        for j in range(cj - 1, cj + 2):
            for plat, plon, rid in _LOAD_INDEX.get((i, j), ()):
                if rid not in near and haversine(lat, lon, plat, plon) <= _LOAD_RADIUS_M:
                    near.add(rid)
    load = min(1.0, len(near) / _LOAD_SATURATION)
    _LOAD_CACHE[key] = load
    return load
