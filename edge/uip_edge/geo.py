"""Spherical geometry helpers for the route simulation.

Enough accuracy for a city-scale prototype; no projection library needed.
"""

from __future__ import annotations

import bisect
import math

EARTH_R = 6_371_000.0


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing in degrees, 0 = north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360.0


def destination(lat: float, lon: float, bearing_deg: float, dist_m: float) -> tuple[float, float]:
    """Point reached by travelling dist_m along bearing_deg."""
    d = dist_m / EARTH_R
    b = math.radians(bearing_deg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1),
                         math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


class Polyline:
    """A route geometry, addressable by distance travelled along it."""

    def __init__(self, points: list[tuple[float, float]]):
        assert len(points) >= 2
        self.points = points
        self.cum = [0.0]
        for (a_lat, a_lon), (b_lat, b_lon) in zip(points, points[1:]):
            self.cum.append(self.cum[-1] + haversine(a_lat, a_lon, b_lat, b_lon))

    @property
    def length(self) -> float:
        return self.cum[-1]

    def at(self, dist_m: float) -> tuple[float, float, float]:
        """(lat, lon, forward bearing) at a distance along the line."""
        d = min(max(dist_m, 0.0), self.length)
        # bisect, not a scan: real OSM routes carry hundreds of points and this is
        # called for every simulated second of every bus
        i = min(max(bisect.bisect_left(self.cum, d) - 1, 0), len(self.points) - 2)
        (a_lat, a_lon), (b_lat, b_lon) = self.points[i], self.points[i + 1]
        seg = self.cum[i + 1] - self.cum[i]
        brg = bearing(a_lat, a_lon, b_lat, b_lon)
        if seg <= 0:
            return a_lat, a_lon, brg
        lat, lon = destination(a_lat, a_lon, brg, d - self.cum[i])
        return lat, lon, brg

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """(distance along the line of the closest point, how far off the line it is).

        A local flat approximation - over one segment of a city street the error is
        far below the GPS noise the simulation adds anyway.
        """
        best = (0.0, float("inf"))
        for i, ((a_lat, a_lon), (b_lat, b_lon)) in enumerate(zip(self.points, self.points[1:])):
            k = math.cos(math.radians(a_lat))
            bx, by = (b_lon - a_lon) * k * 111_320.0, (b_lat - a_lat) * 110_570.0
            px, py = (lon - a_lon) * k * 111_320.0, (lat - a_lat) * 110_570.0
            seg = bx * bx + by * by
            t = 0.0 if seg == 0 else max(0.0, min(1.0, (px * bx + py * by) / seg))
            off = math.hypot(px - bx * t, py - by * t)
            if off < best[1]:
                best = (self.cum[i] + t * (self.cum[i + 1] - self.cum[i]), off)
        return best
