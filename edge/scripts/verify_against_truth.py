"""Diff what the server CONFIRMED against what is actually wrong with the city.

The buses report imperfectly on purpose - missed detections, GPS error, false
positives - so this is the measurement that matters: did multi-bus confirmation
reconstruct the ground truth, and did it reject the noise?

Usage:  uv run python scripts/verify_against_truth.py [base_url]
"""

from __future__ import annotations

import sys

import httpx

from uip_edge.geo import haversine
from uip_edge.routes import buses_serving
from uip_edge.world import POTHOLES, WATERLOGGING

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
#: a confirmed cluster this close to a real defect counts as the same thing
MATCH_RADIUS_M = 45.0


def report(kind: str, truths: list, features: list[dict]) -> tuple[int, int, int]:
    print(f"\n{kind}")
    print(f"  {'ground truth':30s} {'buses':>5s} {'reports':>7s} {'error':>7s}  result")
    unmatched = list(features)
    found = 0

    for t in truths:
        lat, lon = t.lat, t.lon
        best, best_d = None, 1e9
        for f in unmatched:
            flon, flat = f["geometry"]["coordinates"]
            d = haversine(lat, lon, flat, flon)
            if d < best_d:
                best, best_d = f, d

        fleet = buses_serving(lat, lon)
        if best is not None and best_d <= MATCH_RADIUS_M:
            unmatched.remove(best)
            p = best["properties"]
            found += 1
            print(f"  {t.name:30s} {p['distinct_buses']:5d} {p['report_count']:7d} "
                  f"{best_d:6.1f}m  CONFIRMED")
        else:
            print(f"  {t.name:30s} {fleet:5d} {'-':>7s} {'-':>7s}  not confirmed "
                  f"(nearest {best_d:.0f}m)" if best else
                  f"  {t.name:30s} {fleet:5d} {'-':>7s} {'-':>7s}  not confirmed")

    # An unmatched cluster still sitting on top of a real defect is a FRAGMENT the
    # reaper did not merge, not something the fleet invented. Counting it as a false
    # positive overstates the error rate and hides the real bug, which is merging.
    spurious, fragments = [], []
    for f in unmatched:
        flon, flat = f["geometry"]["coordinates"]
        d, near = min((haversine(flat, flon, t.lat, t.lon), t.name) for t in truths)
        (fragments if d <= MATCH_RADIUS_M else spurious).append((f, d, near))

    for f, d, near in fragments:
        p = f["properties"]
        print(f"  {'(second cluster)':30s} {p['distinct_buses']:5d} {p['report_count']:7d} "
              f"{d:6.1f}m  FRAGMENT of {near}")
    for f, d, near in spurious:
        p = f["properties"]
        print(f"  {'(no ground truth here)':30s} {p['distinct_buses']:5d} "
              f"{p['report_count']:7d} {'-':>7s}  FALSE POSITIVE "
              f"(nearest real defect {d / 1000:.1f} km away)")

    return found, len(truths), len(spurious), len(fragments)


def main() -> int:
    with httpx.Client(timeout=30) as c:
        potholes = c.get(f"{BASE}/api/v1/layers/potholes",
                         params={"status": "all"}).json()["features"]
        water = c.get(f"{BASE}/api/v1/layers/waterlogging",
                      params={"status": "all"}).json()["features"]
        incidents = c.get(f"{BASE}/api/v1/layers/incidents",
                          params={"hours": 24 * 3}).json()["features"]
        congestion = c.get(f"{BASE}/api/v1/layers/congestion",
                           params={"minutes": 60 * 24 * 2}).json()["features"]
        stats = c.get(f"{BASE}/api/v1/ops/stats").json()

    p_found, p_total, p_false, p_frag = report("POTHOLES", list(POTHOLES), potholes)
    w_found, w_total, w_false, w_frag = report("WATERLOGGING", list(WATERLOGGING), water)

    raw = stats["raw_events"]
    print(f"\nraw events ingested")
    for t, row in sorted(raw.items()):
        print(f"  {t:14s} {row['total']:7d} from {row['reporting_buses']} buses"
              f"  ({row['cooldown_suppressed']} cooldown-suppressed)")

    print(f"\nmap layers")
    def tail(false_n, frag_n):
        bits = [f"{false_n} false positive"]
        if frag_n:
            bits.append(f"{frag_n} unmerged fragment{'s' if frag_n > 1 else ''}")
        return ", ".join(bits)

    print(f"  confirmed potholes      {p_found}/{p_total} real, {tail(p_false, p_frag)}")
    print(f"  confirmed waterlogging  {w_found}/{w_total} real, {tail(w_false, w_frag)}")
    print(f"  congestion grid cells   {len(congestion)}")
    print(f"  incident markers        {len(incidents)}")

    total_raw = sum(r["total"] for r in raw.values())
    shown = p_found + p_false + p_frag + w_found + w_false + w_frag + len(incidents)
    print(f"\n  {total_raw} raw events -> {shown} confirmed map points "
          f"+ {len(congestion)} aggregated cells")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
