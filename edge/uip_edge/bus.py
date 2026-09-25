"""One simulated bus: drives its route, detects defects imperfectly, emits events.

Models the on-bus behaviour that CLAUDE.md requires of a real edge unit:
  * one event per physical defect PER PASS, not one per frame it was visible in
  * detection is probabilistic - a pass can miss a pothole entirely
  * the reported GPS is the BUS's position with realistic error; the defect is
    10-30 m ahead, reported separately as est_range_m
  * congestion is emitted on a fixed cadence, not per detection
  * incidents go out on the realtime lane; everything else is batched
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from uip_edge.geo import destination
from uip_edge.routes import Route, road_load
from uip_edge.world import (CONGESTION_ZONES, POTHOLES, WATERLOGGING, CongestionZone,
                            Pothole, Waterlogging)
from uip_edge.geo import Polyline

MODEL_VERSIONS = {
    "model_a": "yolov8n-roaddefect-0.1",
    "model_b": "yolov8s-vehicle-0.1",
}
VEHICLE_CLASSES = ("car", "two_wheeler", "auto", "bus", "truck")
PLATE_STATES = ("KA01", "KA02", "KA03", "KA05", "KA41", "KA51", "TN10", "AP28")

#: how far ahead of the bus a road defect is detected
DETECT_RANGE_M = (10.0, 30.0)
#: consumer GPS error on a moving vehicle
GPS_ACCURACY_M = (4.0, 9.0)
#: a congestion reading every 30 s of simulated time
CONGESTION_INTERVAL_S = 30.0
#: spurious pothole detections per km driven - tar patches, shadows, manhole covers
FALSE_POSITIVE_PER_KM = 0.02
#: traffic density on the emptiest lane at the busiest hour ...
BASE_DENSITY = 0.08
#: ... and how much a fully loaded trunk road adds on top of it
LOAD_DENSITY = 0.42
#: what a hotspot saturates at when it is at its own peak hour
ZONE_PEAK_DENSITY = 0.95
#: vehicles in frame on an empty lane, and the extra a trunk road adds, at density 1.0
BASE_VEHICLES, LOAD_VEHICLES = 12.0, 26.0


def _diurnal(hour: float) -> float:
    """Citywide traffic multiplier over the day, 0-1.

    Two commuter peaks with a midday shoulder between them and a dead night. Applied to
    every road, so 03:00 is quiet everywhere and 09:30 is busy everywhere - which is what
    makes a 3 h window on the map show today's peak instead of a 24 h average of it.
    """
    morning = math.exp(-((hour - 9.5) ** 2) / (2 * 1.5 ** 2))
    evening = math.exp(-((hour - 18.5) ** 2) / (2 * 1.9 ** 2))
    midday = math.exp(-((hour - 13.5) ** 2) / (2 * 3.2 ** 2))
    return min(1.0, 0.06 + 0.80 * max(morning, evening) + 0.28 * midday)


def _plate() -> str:
    return (f"{random.choice(PLATE_STATES)}"
            f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            f"{random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
            f"{random.randint(1000, 9999)}")


def _breakdown(total: int) -> dict[str, int]:
    """Split a vehicle count into classes with an Indian-city-ish mix."""
    weights = {"car": 0.42, "two_wheeler": 0.34, "auto": 0.12, "bus": 0.06, "truck": 0.06}
    out, left = {}, total
    for cls, w in list(weights.items())[:-1]:
        n = min(left, int(round(total * w)))
        if n:
            out[cls] = n
        left -= n
    if left > 0:
        out["truck"] = left
    return out


@dataclass
class SimulatedBus:
    """A single edge device. `bus_id` is what the server counts distinct reports by."""

    bus_id: str
    route: Route
    #: where on the route this bus starts, so a fleet is spread along the line
    start_offset_m: float
    rng: random.Random

    position_m: float = 0.0
    direction: int = 1                       # +1 outbound, -1 on the return leg
    _next_congestion_at: datetime | None = None
    _reported_this_pass: set[str] = field(default_factory=set)
    #: resolved once at construction: defect -> distance along THIS route
    _defect_positions: list[tuple[float, object]] = field(default_factory=list)
    _zone_positions: list[tuple[float, CongestionZone]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.position_m = self.start_offset_m
        # a defect is a coordinate now, not a named corridor: ask this route where -
        # if anywhere - it drives past it
        for defect in (*POTHOLES, *WATERLOGGING):
            d = self.route.locate(defect.lat, defect.lon)
            if d is not None:
                self._defect_positions.append((d, defect))
        for zone in CONGESTION_ZONES:
            d = self.route.locate(zone.lat, zone.lon)
            if d is not None:
                self._zone_positions.append((d, zone))

    # -- driving -----------------------------------------------------------------------

    def _zone_at(self, position_m: float) -> CongestionZone | None:
        for d, zone in self._zone_positions:
            if abs(position_m - d) <= zone.radius_m:
                return zone
        return None

    def _congestion_level(self, zone: CongestionZone | None, when: datetime) -> float:
        """Traffic density 0-1 at the bus's current position and time.

        Three terms, because with fewer the map goes flat. An earlier version drew the
        off-zone level from uniform(0.05, 0.25) at every location and every hour, which
        produced exactly two colours on the heatmap: a uniform pale wash at 0.15 over
        the whole network, and the six hotspots. Real traffic varies by WHERE (a trunk
        corridor carries more than a side street) and by WHEN (everywhere is empty at
        03:00), and the interesting middle of the colour ramp is where those two meet.

          * road_load  - baseline from how many routes share this stretch
          * _diurnal   - the citywide commuter curve, applied everywhere
          * the zone   - a hotspot layered on top, still peaking at its own hours
        """
        lat, lon, _ = self.route.polyline.at(self.position_m)
        hour = when.hour + when.minute / 60.0
        level = (BASE_DENSITY + LOAD_DENSITY * road_load(lat, lon)) * _diurnal(hour)
        if zone is not None:
            closeness = min(abs(hour - h) for h in zone.peak_hours)
            peak = max(0.0, 1.0 - closeness / 2.0)      # ramps over ~2 h
            # interpolate towards the jam rather than adding to it, so a hotspot on a
            # quiet road and one on a trunk both saturate at the same place
            level += (ZONE_PEAK_DENSITY - level) * peak
        return min(1.0, max(0.0, level))

    def speed_kmph(self, when: datetime) -> float:
        """Free-flow ~34 km/h, crawling in a congested zone at peak."""
        level = self._congestion_level(self._zone_at(self.position_m), when)
        return max(4.0, 34.0 * (1.0 - 0.85 * level)) * self.rng.uniform(0.85, 1.15)

    def advance(self, when: datetime, dt_s: float) -> None:
        step = self.speed_kmph(when) / 3.6 * dt_s
        self.position_m += step * self.direction
        # a bus route is a there-and-back: turn round at each terminus and start a new
        # pass, which is what makes the same bus report the same pothole again later
        if self.position_m >= self.route.polyline.length:
            self.position_m = self.route.polyline.length
            self.direction = -1
            self._reported_this_pass.clear()
        elif self.position_m <= 0:
            self.position_m = 0.0
            self.direction = 1
            self._reported_this_pass.clear()

    def _fix(self, when: datetime) -> tuple[float, float, float, float]:
        """(lat, lon, heading, gps_accuracy) - the noisy fix the bus actually reports."""
        lat, lon, brg = self.route.polyline.at(self.position_m)
        heading = (brg if self.direction == 1 else brg + 180) % 360
        accuracy = self.rng.uniform(*GPS_ACCURACY_M)
        # scatter the reported position within the accuracy circle
        lat, lon = destination(lat, lon, self.rng.uniform(0, 360),
                               self.rng.gauss(0, accuracy / 2))
        # modulo AFTER rounding: round(359.97, 1) is 360.0, which is not a heading
        return lat, lon, round((heading + self.rng.gauss(0, 4)) % 360, 1) % 360, accuracy

    # -- event construction ------------------------------------------------------------

    def _event(self, event_type: str, when: datetime, *, camera: str, confidence: float,
               extra: dict, priority: str = "batch", bbox: dict | None = None) -> dict:
        lat, lon, heading, accuracy = self._fix(when)
        return {
            "event_id": str(uuid.uuid4()),
            "bus_id": self.bus_id,
            "camera_id": camera,
            "event_type": event_type,
            "timestamp": when.isoformat(),
            "gps": {"lat": lat, "lon": lon},
            "gps_accuracy_m": round(accuracy, 1),
            "heading_deg": heading,
            "speed_kmph": round(self.speed_kmph(when), 1),
            "confidence": round(confidence, 3),
            "model_versions": MODEL_VERSIONS,
            "bbox": bbox,
            "media_ref": f"{self.bus_id}/{when:%Y-%m-%d}/buffer.mp4#t={when:%H%M%S}",
            "priority": priority,
            "extra": extra,
        }

    def _bbox(self) -> dict:
        """A plausible box in the lower half of the frame, where road surface is."""
        w = self.rng.uniform(0.05, 0.22)
        h = w * self.rng.uniform(0.5, 0.9)
        return {"x": round(self.rng.uniform(0.15, 0.75 - w), 3),
                "y": round(self.rng.uniform(0.55, 0.9 - h), 3),
                "w": round(w, 3), "h": round(h, 3)}

    # -- the tick ----------------------------------------------------------------------

    def step(self, when: datetime, dt_s: float) -> list[dict]:
        """Advance the bus by dt_s of simulated time and return the events it emitted."""
        before = self.position_m
        self.advance(when, dt_s)
        events: list[dict] = []

        events += self._detections(before, when)
        events += self._congestion(when)
        return events

    def _detections(self, before_m: float, when: datetime) -> list[dict]:
        """Fire for any defect the bus just drove past, once per pass."""
        events = []
        lo, hi = sorted((before_m, self.position_m))
        for defect_m, defect in self._defect_positions:
            # the detection happens while the defect is still DETECT_RANGE_M ahead
            trigger = defect_m - self.direction * self.rng.uniform(*DETECT_RANGE_M)
            if not (lo <= trigger <= hi) or defect.name in self._reported_this_pass:
                continue
            self._reported_this_pass.add(defect.name)

            if isinstance(defect, Waterlogging) and not self._is_wet(defect, when):
                continue
            if self.rng.random() > defect.detect_p:
                continue                                  # a pass that simply missed it

            est_range = abs(defect_m - self.position_m)
            if isinstance(defect, Pothole):
                events.append(self._event(
                    "pothole", when, camera="front",
                    confidence=self.rng.uniform(0.62, 0.95),
                    bbox=self._bbox(),
                    extra={"severity": self._perceived_severity(defect.severity),
                           "lane_position": self.rng.choice(["left", "centre", "right"]),
                           "est_range_m": round(est_range, 1)}))
            else:
                events.append(self._event(
                    "waterlogging", when, camera="front",
                    confidence=self.rng.uniform(0.7, 0.96),
                    bbox=self._bbox(),
                    extra={"coverage_pct": round(
                               max(5.0, min(100.0, self.rng.gauss(defect.coverage_pct, 8))), 1),
                           "lane_position": self.rng.choice(["left", "centre", "right"]),
                           "est_range_m": round(est_range, 1)}))

        events += self._false_positives(hi - lo, when)
        return events

    def _perceived_severity(self, truth: str) -> str:
        """Buses disagree about severity - the server takes the worst in a cluster."""
        order = ["low", "medium", "high"]
        i = order.index(truth)
        if self.rng.random() < 0.25:
            i = max(0, min(2, i + self.rng.choice([-1, 1])))
        return order[i]

    def _is_wet(self, w: Waterlogging, when: datetime) -> bool:
        hour = when.hour + when.minute / 60.0
        return w.active_from_hour <= hour <= w.active_to_hour

    def _false_positives(self, travelled_m: float, when: datetime) -> list[dict]:
        """Spurious potholes. Without these, confirmation has nothing to filter out."""
        if self.rng.random() > FALSE_POSITIVE_PER_KM * travelled_m / 1000.0:
            return []
        return [self._event("pothole", when, camera="front",
                            confidence=self.rng.uniform(0.5, 0.72), bbox=self._bbox(),
                            extra={"severity": self.rng.choice(["low", "medium"]),
                                   "lane_position": "centre",
                                   "est_range_m": round(self.rng.uniform(*DETECT_RANGE_M), 1)})]

    def _congestion(self, when: datetime) -> list[dict]:
        """A frame-level aggregate on a fixed cadence - never one per detection."""
        if self._next_congestion_at is None:
            self._next_congestion_at = when + timedelta(
                seconds=self.rng.uniform(0, CONGESTION_INTERVAL_S))
            return []
        if when < self._next_congestion_at:
            return []
        self._next_congestion_at = when + timedelta(seconds=CONGESTION_INTERVAL_S)

        zone = self._zone_at(self.position_m)
        level = self._congestion_level(zone, when)
        lat, lon, _ = self.route.polyline.at(self.position_m)
        peak = (zone.peak_vehicle_count if zone
                else BASE_VEHICLES + LOAD_VEHICLES * road_load(lat, lon))
        count = max(0, int(round(self.rng.gauss(peak * level, peak * 0.08))))
        return [self._event(
            "congestion", when, camera="front", confidence=self.rng.uniform(0.85, 0.98),
            extra={"vehicle_count": count,
                   "vehicle_class_breakdown": _breakdown(count),
                   "own_speed_kmph": round(self.speed_kmph(when), 1),
                   "density_score": round(min(1.0, level * self.rng.uniform(0.9, 1.1)), 3),
                   "window_s": CONGESTION_INTERVAL_S})]

    def incident(self, when: datetime, subtype: str = "accident") -> dict:
        """An incident at the bus's current position, on the realtime lane.

        ANPR reads every plate visible on any camera, so this carries a list rather
        than a single offending vehicle.
        """
        n = self.rng.choice([1, 2, 2, 3]) if subtype == "accident" else 1
        cameras = self.rng.sample(["front", "side_left", "side_right", "rear"],
                                  k=min(3, max(2, n)))
        return self._event(
            "incident", when, camera=cameras[0], priority="realtime",
            confidence=self.rng.uniform(0.82, 0.97),
            bbox=self._bbox(),
            extra={"subtype": subtype,
                   "vehicles_involved": [
                       {"plate_number": _plate(),
                        "plate_confidence": round(self.rng.uniform(0.55, 0.96), 3),
                        "vehicle_class": self.rng.choice(VEHICLE_CLASSES),
                        "camera_id": self.rng.choice(cameras)} for _ in range(n)],
                   "trajectory_anomaly_score": round(self.rng.uniform(0.7, 0.99), 3),
                   "contributing_camera_ids": cameras,
                   "impact_signature": round(self.rng.uniform(1.8, 4.5), 2)})
