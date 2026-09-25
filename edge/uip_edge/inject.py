"""Report a defect at a chosen spot, on demand, as if buses had just driven past.

This is the live-demo tool. The full simulation (`uip_edge.run`) drives a fixed set of
defects on a fixed route network; this injects one wherever you point it, from as many
buses as you choose, and the map picks it up on the next refresh.

It is the clearest way to show the central rule of the system: ONE bus reporting a
pothole is not enough, and the map stays empty. Report the same spot from a second and
third bus and it appears. Nothing about the server is special-cased for this - these
are ordinary events on the ordinary ingest API.

    # not enough evidence - stays pending, map shows nothing
    uv run python -m uip_edge.inject pothole --lat 18.5204 --lon 73.8567 --buses 1

    # three buses agree - confirmed, appears on the map
    uv run python -m uip_edge.inject pothole --lat 18.5204 --lon 73.8567 --buses 3

    # or let it pick a random spot on the Pune route network
    uv run python -m uip_edge.inject waterlogging --random --buses 2
    uv run python -m uip_edge.inject congestion --random --level 0.9
    uv run python -m uip_edge.inject incident --random
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from uip_edge.bus import MODEL_VERSIONS, VEHICLE_CLASSES, _breakdown, _plate
from uip_edge.geo import destination
from uip_edge.routes import ROUTES

DEFAULT_URL = os.environ.get("UIP_SERVER_URL", "http://localhost:8000")
DEFAULT_TOKEN = os.environ.get("UIP_INGEST_TOKEN", "dev-token-change-me")


def random_point(rng: random.Random) -> tuple[float, float]:
    """A random spot on the route network, so an injected defect sits on a real road."""
    route = rng.choice(ROUTES)
    lat, lon, _ = route.polyline.at(rng.uniform(0, route.polyline.length))
    return lat, lon


def build(event_type: str, lat: float, lon: float, bus_id: str, when: datetime,
          rng: random.Random, args: argparse.Namespace) -> dict:
    """One event, positioned as a real bus would report it.

    The bus reports its OWN position and how far ahead the defect is; the server
    projects the two into a defect position. So to place a defect at (lat, lon) we put
    the bus `range` metres BEHIND it, along a heading pointing at it.
    """
    heading = rng.uniform(0, 360)
    est_range = rng.uniform(12, 28)
    # step backwards from the defect to where the bus would have been
    bus_lat, bus_lon = destination(lat, lon, (heading + 180) % 360, est_range)
    # plus ordinary GPS scatter, so the reports do not land on top of each other
    accuracy = rng.uniform(4, 9)
    bus_lat, bus_lon = destination(bus_lat, bus_lon, rng.uniform(0, 360),
                                   rng.gauss(0, accuracy / 2))

    common = {
        "event_id": str(uuid.uuid4()),
        "bus_id": bus_id,
        "camera_id": "front",
        "event_type": event_type,
        "timestamp": when.isoformat(),
        "gps": {"lat": bus_lat, "lon": bus_lon},
        "gps_accuracy_m": round(accuracy, 1),
        "heading_deg": round(heading, 1) % 360,
        "speed_kmph": round(rng.uniform(12, 35), 1),
        "model_versions": MODEL_VERSIONS,
        "media_ref": f"{bus_id}/{when:%Y-%m-%d}/buffer.mp4#t={when:%H%M%S}",
        "priority": "batch",
    }
    bbox = {"x": 0.35, "y": 0.68, "w": 0.14, "h": 0.09}

    if event_type == "pothole":
        return {**common, "confidence": round(rng.uniform(0.7, 0.95), 3), "bbox": bbox,
                "extra": {"severity": args.severity,
                          "lane_position": rng.choice(["left", "centre", "right"]),
                          "est_range_m": round(est_range, 1)}}

    if event_type == "waterlogging":
        return {**common, "confidence": round(rng.uniform(0.75, 0.96), 3), "bbox": bbox,
                "extra": {"coverage_pct": round(rng.gauss(args.coverage, 6), 1),
                          "lane_position": rng.choice(["left", "centre", "right"]),
                          "est_range_m": round(est_range, 1)}}

    if event_type == "congestion":
        count = max(0, int(round(45 * args.level * rng.uniform(0.9, 1.1))))
        return {**common, "confidence": round(rng.uniform(0.85, 0.98), 3),
                # congestion is a frame-level aggregate: no bbox, and the bus reports
                # from where it actually is rather than pointing at anything
                "gps": {"lat": lat, "lon": lon},
                "extra": {"vehicle_count": count,
                          "vehicle_class_breakdown": _breakdown(count),
                          "own_speed_kmph": round(max(4.0, 34 * (1 - 0.85 * args.level)), 1),
                          "density_score": round(min(1.0, args.level), 3),
                          "window_s": 30.0}}

    n = rng.choice([2, 2, 3])
    cameras = rng.sample(["front", "side_left", "side_right", "rear"], k=2)
    return {**common, "confidence": round(rng.uniform(0.85, 0.97), 3), "bbox": bbox,
            "camera_id": cameras[0], "priority": "realtime",
            "extra": {"subtype": args.subtype,
                      "vehicles_involved": [
                          {"plate_number": _plate(),
                           "plate_confidence": round(rng.uniform(0.6, 0.96), 3),
                           "vehicle_class": rng.choice(VEHICLE_CLASSES),
                           "camera_id": rng.choice(cameras)} for _ in range(n)],
                      "trajectory_anomaly_score": round(rng.uniform(0.75, 0.99), 3),
                      "contributing_camera_ids": cameras,
                      "impact_signature": round(rng.uniform(2.0, 4.5), 2)}}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("event_type",
                   choices=["pothole", "waterlogging", "congestion", "incident"])
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--random", action="store_true",
                   help="pick a random point on the Pune route network")
    p.add_argument("--buses", type=int, default=None,
                   help="how many DISTINCT buses report it - the confirmation lever. "
                        "Defaults to 3 for defects (the confirmation threshold) and 1 "
                        "for incidents, which need no corroboration")
    p.add_argument("--passes", type=int, default=1,
                   help="passes per bus; with --buses 1 this drives the repeated-pass "
                        "fallback instead")
    p.add_argument("--spread-hours", type=float, default=None,
                   help="spread the reports back over this many hours, so they clear "
                        "the same-bus cooldown and the fallback's span requirement. "
                        "Defaults to 8 h, but 1.5 h for waterlogging - its confirmation "
                        "window is 3 HOURS (one rain event), so reports spread over 8 h "
                        "would fall outside it and never confirm")
    p.add_argument("--severity", default="high", choices=["low", "medium", "high"])
    p.add_argument("--coverage", type=float, default=60.0, help="waterlogging %%")
    p.add_argument("--level", type=float, default=0.85, help="congestion density 0-1")
    p.add_argument("--subtype", default="accident", choices=["accident", "rash_driving"])
    p.add_argument("--bus-prefix", default="DEMO")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--token", default=DEFAULT_TOKEN)
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()

    if args.buses is None:
        # an incident is a single case, not a density measurement: several buses
        # reporting one would be several separate accidents at the same spot
        args.buses = 1 if args.event_type == "incident" else 3

    if args.spread_hours is None:
        # waterlogging confirms inside a 3 h window on purpose - it is one rain event,
        # not a permanent defect - so the pothole-friendly 8 h spread would place the
        # reports outside each other's window and nothing would ever confirm.
        args.spread_hours = 1.5 if args.event_type == "waterlogging" else 8.0

    rng = random.Random(args.seed)
    if args.random:
        lat, lon = random_point(rng)
    elif args.lat is not None and args.lon is not None:
        lat, lon = args.lat, args.lon
    else:
        p.error("give --lat and --lon, or --random")

    now = datetime.now(timezone.utc)
    total = args.buses * args.passes
    events = []
    for i in range(args.buses):
        bus_id = f"BUS-{args.bus_prefix}-{i + 1:02d}"
        for j in range(args.passes):
            # spread reports backwards in time: repeats from one bus at one spot inside
            # the cooldown are suppressed by design, so they must be far enough apart
            k = i * args.passes + j
            when = now - timedelta(hours=args.spread_hours * (total - 1 - k) / max(1, total - 1)) \
                if total > 1 else now
            events.append(build(args.event_type, lat, lon, bus_id, when, rng, args))

    r = httpx.post(f"{args.url}/api/v1/events", json={"events": events},
                   headers={"Authorization": f"Bearer {args.token}"}, timeout=30)
    if r.status_code >= 400:
        print(f"ingest failed: {r.status_code} {r.text[:300]}")
        return 1
    body = r.json()
    print(f"{args.event_type} at {lat:.5f}, {lon:.5f}")
    print(f"  {args.buses} bus(es) x {args.passes} pass(es) = {len(events)} events -> "
          f"accepted {body['accepted']}, duplicates {body['duplicates']}, "
          f"rejected {body['rejected']}")
    for ack in body["results"]:
        if ack["status"] == "rejected":
            print(f"  rejected: {ack['error']}")

    if args.event_type in ("pothole", "waterlogging"):
        layer = "potholes" if args.event_type == "pothole" else "waterlogging"
        d = 0.0005
        got = httpx.get(f"{args.url}/api/v1/layers/{layer}",
                        params={"bbox": f"{lon-d},{lat-d},{lon+d},{lat+d}"},
                        timeout=30).json()["features"]
        if got:
            f = got[0]["properties"]
            print(f"  CONFIRMED on the map: {f['distinct_buses']} distinct buses, "
                  f"{f['report_count']} reports")
        else:
            # be specific about WHICH rule was not met: with --spread-hours it is often
            # the window rather than the bus count, and "needs a second bus" is
            # actively confusing when you just sent two
            need = ("3 distinct buses within 20 m, or 4+ passes by one bus spanning 6 h"
                    if args.event_type == "pothole"
                    else "2 distinct buses within 20 m AND inside the same 3 h window")
            print(f"  still PENDING - not on the map. Needs {need}.")
    else:
        print("  visible immediately - no confirmation needed for this type")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
