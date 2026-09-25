"""Ground truth: what is actually wrong with the simulated city (Pune).

The buses do not know any of this. They detect it imperfectly - missed detections, GPS
error, occasional false positives - and the central server has to reconstruct it from
their disagreeing reports. That reconstruction is the thing being demonstrated, so the
ground truth is kept here explicitly and diffed against what the server confirmed by
`edge/scripts/verify_against_truth.py`.

Positions are real coordinates on real Pune streets, chosen from a coverage analysis of
the OSM route network (see `edge/scripts/fetch_routes.py`) so that the set spans the
full range of evidence the confirmation rules have to cope with:

  * corridors carrying seven routes, where a defect confirms on distinct buses almost
    immediately;
  * stretches carrying one route with two buses - enough for waterlogging's 2-bus rule,
    never enough for a pothole's 3;
  * the single-bus campus feeder to Symbiosis Lavale, where no defect can EVER reach the
    distinct-bus threshold and only the repeated-pass fallback can confirm it.

The assertion at the bottom fails the import if a defect is placed off the route network
- otherwise a typo'd coordinate would silently become a defect no bus can ever see.
"""

from __future__ import annotations

from dataclasses import dataclass

from uip_edge.routes import buses_serving, routes_serving


@dataclass(frozen=True)
class Pothole:
    name: str
    lat: float
    lon: float
    severity: str            # low | medium | high
    #: probability a passing bus detects it. A shallow pothole is genuinely missed.
    detect_p: float = 0.75


@dataclass(frozen=True)
class Waterlogging:
    name: str
    lat: float
    lon: float
    coverage_pct: float
    #: waterlogging only exists during a rain event, given as sim hours since midnight
    active_from_hour: float
    active_to_hour: float
    detect_p: float = 0.85


@dataclass(frozen=True)
class CongestionZone:
    name: str
    lat: float
    lon: float
    radius_m: float
    #: peak vehicle count seen in one frame at the worst moment
    peak_vehicle_count: int
    #: hours of day when it is worst
    peak_hours: tuple[float, ...]


POTHOLES: tuple[Pothole, ...] = (
    # the Hinjawadi corridor - seven routes, twelve buses. Confirms fastest.
    Pothole("Hinjawadi Phase 2 Road", 18.58510, 73.69649, "high", detect_p=0.85),
    Pothole("Hinjawadi-Wakad Road", 18.59101, 73.74165, "medium"),
    # shallow: usually missed, so it needs many passes before three buses agree
    Pothole("Katraj-Dehu Road Bypass", 18.58556, 73.75968, "low", detect_p=0.35),
    Pothole("Ganeshkhind Road", 18.54041, 73.83165, "high"),
    Pothole("Babasaheb Ambedkar Marg", 18.52740, 73.86402, "medium"),
    Pothole("University Road, Aundh", 18.56516, 73.81252, "high", detect_p=0.8),
    # route 94 only: two buses ever pass, so the 3-bus threshold is unreachable and
    # the repeated-pass fallback has to carry it
    Pothole("Ganesh Path, Kasba Peth", 18.51865, 73.85972, "medium"),
    # one bus, the campus feeder. The purest test of the fallback rule.
    Pothole("Symbiosis Road, Nande", 18.54258, 73.72803, "high", detect_p=0.9),
    # the OSRM corridors, two buses each - also fallback cases, and the only defects
    # anywhere in the south and east of the city
    Pothole("Satara Road, Katraj", 18.46583, 73.85783, "high", detect_p=0.8),
    Pothole("Kharadi Bypass", 18.54267, 73.93510, "medium"),
)

WATERLOGGING: tuple[Waterlogging, ...] = (
    # an evening downpour over the west and the centre
    Waterlogging("Wipro Circle, Marunji", 18.59695, 73.71850, 70.0, 17.0, 20.5,
                 detect_p=0.9),
    Waterlogging("Ganeshkhind Road", 18.53906, 73.83354, 45.0, 17.5, 20.0),
    Waterlogging("Congress Bhavan Path", 18.52334, 73.85446, 55.0, 18.0, 21.0),
    # a separate morning shower - must NOT merge with the evening cluster, which is
    # exactly what the 3 h confirmation window is there to prevent
    Waterlogging("Balewadi Gaon Road", 18.56366, 73.78290, 60.0, 7.0, 9.5),
    # the north corridor: two buses, which is exactly the waterlogging threshold
    Waterlogging("Bhau Patil Path, Bopodi", 18.57001, 73.83259, 50.0, 17.5, 20.5),
)

CONGESTION_ZONES: tuple[CongestionZone, ...] = (
    CongestionZone("Hinjawadi IT park", 18.59397, 73.73302, 700, 45, (9, 10, 18, 19, 20)),
    CongestionZone("Wakad bypass", 18.58556, 73.75968, 500, 38, (9, 10, 18, 19)),
    CongestionZone("Baner Road", 18.56144, 73.78714, 500, 36, (9, 10, 18, 19)),
    CongestionZone("University Road", 18.54041, 73.83165, 450, 34, (10, 11, 17, 18, 19)),
    CongestionZone("Pune Station", 18.52740, 73.86402, 500, 40, (10, 11, 17, 18, 19)),
    CongestionZone("Shivajinagar", 18.52334, 73.85446, 400, 30, (9, 10, 18, 19)),
    # hotspots on the OSRM corridors, so the south, east and north of the map are not
    # uniformly quiet just because OSM never traced a PMPML relation through them
    CongestionZone("Swargate approach", 18.50118, 73.86781, 450, 34, (9, 10, 18, 19)),
    CongestionZone("Hadapsar / Magarpatta", 18.50840, 73.92955, 500, 36,
                   (10, 11, 18, 19, 20)),
    CongestionZone("Pimpri Chowk", 18.62241, 73.80890, 450, 32, (9, 10, 18, 19)),
)


def all_defects() -> list[Pothole | Waterlogging]:
    return [*POTHOLES, *WATERLOGGING]


def summary() -> str:
    lines = ["ground truth:"]
    for p in POTHOLES:
        lines.append(f"  pothole      {p.name:26s} sev={p.severity:6s} "
                     f"detect_p={p.detect_p}  {buses_serving(p.lat, p.lon)} buses pass")
    for w in WATERLOGGING:
        lines.append(f"  waterlogging {w.name:26s} {w.coverage_pct:.0f}% active "
                     f"{w.active_from_hour:.1f}-{w.active_to_hour:.1f}h  "
                     f"{buses_serving(w.lat, w.lon)} buses pass")
    for c in CONGESTION_ZONES:
        lines.append(f"  congestion   {c.name:26s} peak={c.peak_vehicle_count}")
    return "\n".join(lines)


# fail fast if a defect was placed off the route network - no bus would ever see it
for _d in (*POTHOLES, *WATERLOGGING, *CONGESTION_ZONES):
    assert routes_serving(_d.lat, _d.lon), f"{_d.name}: no route passes this point"
