"""End-to-end check of the central server against a live PostGIS instance.

This is NOT the simulated bus generator (that is prototype component 2). It is the
smallest set of requests that proves the parts of the server that can actually be
wrong: idempotency, the same-bus cooldown, multi-bus confirmation, query-time
congestion aggregation, and single-report incidents.

Usage:  uv run python scripts/smoke_test.py [base_url]
"""

from __future__ import annotations

import math
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("UIP_INGEST_TOKEN", "dev-token-change-me")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}

# Well clear of the simulated route network: the fleet blankets central Pune with
# congestion events, and a shared 100 m grid cell makes the congestion assertions count
# the fleet's samples as well as the test's. The jitter keeps repeated runs from
# clustering into each other.
BASE_LAT = 18.5204 + 0.35 + random.uniform(-0.02, 0.02)
BASE_LON = 73.8567 + 0.35 + random.uniform(-0.02, 0.02)
NOW = datetime.now(timezone.utc)

passed, failed = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {label}")
    else:
        failed += 1
        print(f"  FAIL  {label}  {detail}")


def offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    return (lat + north_m / 111_320.0,
            lon + east_m / (111_320.0 * math.cos(math.radians(lat))))


def event(event_type: str, lat: float, lon: float, *, bus: str, extra: dict,
          minutes_ago: float = 0, conf: float = 0.9, heading: float | None = 90.0,
          camera: str = "front", priority: str = "batch") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "bus_id": bus,
        "camera_id": camera,
        "event_type": event_type,
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "gps": {"lat": lat, "lon": lon},
        "gps_accuracy_m": 6.0,
        "heading_deg": heading,
        "speed_kmph": 32.0,
        "confidence": conf,
        "model_versions": {"model_a": "yolov8n-pothole-0.1"},
        "priority": priority,
        "extra": extra,
    }


def post(client: httpx.Client, events: list[dict]) -> dict:
    r = client.post(f"{BASE}/api/v1/events", json={"events": events}, headers=HEADERS)
    r.raise_for_status()
    return r.json()


def near(client: httpx.Client, path: str, lat: float, lon: float, radius_deg=0.001, **params):
    """Fetch a layer restricted to a tight box around a point."""
    bbox = f"{lon-radius_deg},{lat-radius_deg},{lon+radius_deg},{lat+radius_deg}"
    r = client.get(f"{BASE}{path}", params={"bbox": bbox, **params})
    r.raise_for_status()
    return r.json()["features"]


def main() -> int:
    with httpx.Client(timeout=30) as client:
        health = client.get(f"{BASE}/healthz").json()
        print(f"server up - PostGIS {health['postgis'].split()[0]}\n")

        # -- auth ------------------------------------------------------------------
        print("auth")
        r = client.post(f"{BASE}/api/v1/events", json={"events": []})
        check("unauthenticated ingest is rejected", r.status_code in (401, 422),
              f"got {r.status_code}")

        # -- schema validation -----------------------------------------------------
        print("\nschema validation")
        bad = event("incident", BASE_LAT, BASE_LON, bus="BUS-X",
                    extra={"subtype": "accident"}, priority="batch")
        res = post(client, [bad])
        check("an incident on the batch lane is rejected",
              res["rejected"] == 1, str(res["results"]))

        bad = event("congestion", BASE_LAT, BASE_LON, bus="BUS-X",
                    extra={"vehicle_count": 5, "own_speed_kmph": 10, "density_score": 0.5})
        bad["bbox"] = {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
        res = post(client, [bad])
        check("congestion carrying a bbox is rejected",
              res["rejected"] == 1, str(res["results"]))

        # the property that matters for a bus uploading a backlog over a flaky modem
        good = event("pothole", *offset(BASE_LAT, BASE_LON, 800, 800), bus="BUS-ISO",
                     extra={"severity": "low", "est_range_m": 15.0})
        res = post(client, [bad, good, dict(bad, event_id=str(uuid.uuid4()))])
        check("one malformed event does not discard the rest of the batch",
              res["accepted"] == 1 and res["rejected"] == 2, str(res))
        check("the rejection says what was wrong",
              any(r["error"] for r in res["results"] if r["status"] == "rejected"),
              str(res["results"]))

        # -- pothole confirmation --------------------------------------------------
        # Two buses drive the same stretch eastward. Each detects the pothole ~20 m
        # ahead, so both project onto roughly the same ground position.
        print("\npothole: multi-bus confirmation")
        p_lat, p_lon = BASE_LAT, BASE_LON
        pothole_extra = {"severity": "high", "lane_position": "left", "est_range_m": 20.0}

        e1 = event("pothole", p_lat, p_lon, bus="BUS-101", minutes_ago=90,
                   extra=pothole_extra)
        post(client, [e1])
        feats = near(client, "/api/v1/layers/potholes", p_lat, p_lon)
        check("one bus alone does not confirm a pothole", len(feats) == 0,
              f"{len(feats)} features")

        pend = client.get(f"{BASE}/api/v1/ops/pending/pothole").json()["features"]
        check("the unconfirmed report is visible in the ops pending view",
              any(f["properties"]["event_id"] == e1["event_id"] for f in pend))

        # same bus, same spot, 5 minutes later - inside the 60 min cooldown
        e2 = event("pothole", *offset(p_lat, p_lon, 1, 2), bus="BUS-101",
                   minutes_ago=85, extra=pothole_extra)
        res = post(client, [e2])
        check("a repeat report from the same bus is cooldown-suppressed",
              res["results"][0]["cooldown_suppressed"] is True, str(res["results"][0]))

        feats = near(client, "/api/v1/layers/potholes", p_lat, p_lon)
        check("the suppressed repeat still does not confirm it", len(feats) == 0,
              f"{len(feats)} features")

        # a DIFFERENT bus, 8 m away - real corroboration, but still short of the bar
        e3 = event("pothole", *offset(p_lat, p_lon, 5, 6), bus="BUS-202",
                   minutes_ago=40, extra={**pothole_extra, "severity": "medium"})
        post(client, [e3])
        feats = near(client, "/api/v1/layers/potholes", p_lat, p_lon)
        check("two distinct buses are still not enough (threshold is 3)",
              len(feats) == 0, f"{len(feats)} features")

        # the third distinct bus crosses the threshold
        e4 = event("pothole", *offset(p_lat, p_lon, -4, 7), bus="BUS-303",
                   minutes_ago=20, extra={**pothole_extra, "severity": "low"})
        post(client, [e4])
        feats = near(client, "/api/v1/layers/potholes", p_lat, p_lon)
        check("a third distinct bus confirms the pothole", len(feats) == 1,
              f"{len(feats)} features")
        if feats:
            props = feats[0]["properties"]
            check("the cluster counts 3 distinct buses", props["distinct_buses"] == 3,
                  str(props))
            check("the cooldown-suppressed report is excluded from the count",
                  props["report_count"] == 3, f"report_count={props['report_count']}")
            check("the worst severity in the cluster wins", props["severity"] == "high",
                  str(props["severity"]))
            lon_c, lat_c = feats[0]["geometry"]["coordinates"]
            east_m = (lon_c - p_lon) * 111_320 * math.cos(math.radians(p_lat))
            check("the centroid sits ~20 m ahead of the buses, not under them",
                  12 < east_m < 28, f"{east_m:.1f} m east of the bus positions")

        # -- idempotency -----------------------------------------------------------
        print("\nidempotency")
        res = post(client, [e3])
        check("re-uploading a buffered event is a duplicate, not a second report",
              res["duplicates"] == 1, str(res))
        feats = near(client, "/api/v1/layers/potholes", p_lat, p_lon)
        check("the duplicate did not inflate the report count",
              bool(feats) and feats[0]["properties"]["report_count"] == 3,
              str(feats[0]["properties"]) if feats else "no features")

        # -- the low-frequency-route fallback --------------------------------------
        # A route served by one bus can never field three of them. Separate passes,
        # spread over hours, are independent looks and confirm on their own.
        print("\npothole: repeated-pass fallback for low-frequency routes")
        f_lat, f_lon = offset(BASE_LAT, BASE_LON, 0, -600)
        for n, mins in enumerate((480, 330, 180, 30)):
            post(client, [event("pothole", *offset(f_lat, f_lon, n, n * 2),
                                bus="BUS-SOLO", minutes_ago=mins, extra=pothole_extra)])
            feats = near(client, "/api/v1/layers/potholes", f_lat, f_lon)
            if n < 3:
                check(f"{n + 1} pass(es) by one bus is not yet enough", len(feats) == 0,
                      f"{len(feats)} features after {n + 1}")
        check("4 passes by ONE bus spanning 7.5 h confirms it", len(feats) == 1,
              f"{len(feats)} features")
        if feats:
            check("it is confirmed on a single bus's evidence",
                  feats[0]["properties"]["distinct_buses"] == 1,
                  str(feats[0]["properties"]))

        # the same four passes crammed into one hour must NOT confirm
        t_lat, t_lon = offset(BASE_LAT, BASE_LON, 0, -900)
        for n, mins in enumerate((60, 45, 30, 15)):
            post(client, [event("pothole", *offset(t_lat, t_lon, n, n * 2),
                                bus="BUS-BURST", minutes_ago=mins, extra=pothole_extra)])
        feats = near(client, "/api/v1/layers/potholes", t_lat, t_lon)
        check("4 passes inside one hour do NOT confirm (span requirement)",
              len(feats) == 0, f"{len(feats)} features")

        # -- waterlogging ----------------------------------------------------------
        print("\nwaterlogging: short window, separate rule")
        w_lat, w_lon = offset(BASE_LAT, BASE_LON, 300, 0)
        for bus, mins, cov in (("BUS-101", 40, 55.0), ("BUS-303", 20, 65.0)):
            post(client, [event("waterlogging", w_lat, w_lon, bus=bus, minutes_ago=mins,
                                extra={"coverage_pct": cov, "lane_position": "centre",
                                       "est_range_m": 15.0})])
        feats = near(client, "/api/v1/layers/waterlogging", w_lat, w_lon)
        check("two buses confirm waterlogging", len(feats) == 1, f"{len(feats)} features")
        if feats:
            check("coverage is averaged across reports",
                  abs(feats[0]["properties"]["avg_coverage_pct"] - 60.0) < 0.1,
                  str(feats[0]["properties"]["avg_coverage_pct"]))

        # a report older than the 3 h window must not join this cluster
        old_lat, old_lon = offset(w_lat, w_lon, 2, 2)
        post(client, [event("waterlogging", old_lat, old_lon, bus="BUS-404",
                            minutes_ago=60 * 20,
                            extra={"coverage_pct": 40.0, "est_range_m": 15.0})])
        feats = near(client, "/api/v1/layers/waterlogging", w_lat, w_lon)
        check("a report from 20 h ago is a different flood, not more evidence",
              len(feats) == 1 and feats[0]["properties"]["report_count"] == 2,
              str([f["properties"]["report_count"] for f in feats]))

        # -- congestion ------------------------------------------------------------
        print("\ncongestion: aggregated, never confirmed")
        c_lat, c_lon = offset(BASE_LAT, BASE_LON, -300, 0)
        post(client, [event("congestion", *offset(c_lat, c_lon, i * 3, i * 3),
                            bus="BUS-101", minutes_ago=10 - i, heading=90,
                            extra={"vehicle_count": 20 + i,
                                   "vehicle_class_breakdown": {"car": 14, "bike": 6},
                                   "own_speed_kmph": 8.0, "density_score": 0.8,
                                   "window_s": 30})
                      for i in range(5)])
        feats = near(client, "/api/v1/layers/congestion", c_lat, c_lon, minutes=60)
        check("a single bus's congestion reading renders with no confirmation",
              len(feats) >= 1, f"{len(feats)} cells")
        if feats:
            total = sum(f["properties"]["sample_count"] for f in feats)
            check("all 5 samples land in the grid", total == 5, f"{total} samples")
            check("density is exposed as the heatmap weight",
                  abs(feats[0]["properties"]["weight"] - 0.8) < 0.001,
                  str(feats[0]["properties"]["weight"]))

        # -- incidents -------------------------------------------------------------
        print("\nincidents: single high-confidence report, realtime lane")
        i_lat, i_lon = offset(BASE_LAT, BASE_LON, 0, 400)
        inc = event("incident", i_lat, i_lon, bus="BUS-202", conf=0.93, camera="side_left",
                    priority="realtime",
                    extra={"subtype": "accident",
                           "vehicles_involved": [
                               {"plate_number": "KA01AB1234", "plate_confidence": 0.88,
                                "vehicle_class": "car", "camera_id": "side_left"},
                               {"plate_number": "KA05CD5678", "plate_confidence": 0.71,
                                "vehicle_class": "two_wheeler", "camera_id": "front"}],
                           "trajectory_anomaly_score": 0.95,
                           "contributing_camera_ids": ["front", "side_left"],
                           "impact_signature": 3.4})
        post(client, [inc])
        feats = near(client, "/api/v1/layers/incidents", i_lat, i_lon, hours=24)
        check("one high-confidence report is enough for an incident", len(feats) == 1,
              f"{len(feats)} features")
        if feats:
            check("ANPR returns every visible plate, not just one offender",
                  len(feats[0]["properties"]["vehicles_involved"]) == 2,
                  str(feats[0]["properties"]["vehicles_involved"]))

        low = event("incident", *offset(i_lat, i_lon, 5, 5), bus="BUS-202", conf=0.4,
                    camera="rear", priority="realtime",
                    extra={"subtype": "rash_driving", "trajectory_anomaly_score": 0.4})
        post(client, [low])
        feats = near(client, "/api/v1/layers/incidents", i_lat, i_lon, hours=24)
        check("a low-confidence incident is withheld from the map", len(feats) == 1,
              f"{len(feats)} features")

        # -- stats -----------------------------------------------------------------
        print("\nops")
        s = client.get(f"{BASE}/api/v1/ops/stats").json()
        check("stats report the suppressed pothole report",
              s["raw_events"]["pothole"]["cooldown_suppressed"] >= 1, str(s["raw_events"]))
        r = client.post(f"{BASE}/api/v1/ops/reap", headers=HEADERS)
        check("the maintenance reaper runs", r.status_code == 200, r.text[:120])

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
