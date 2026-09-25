"""Internal ops views - what the public map deliberately does not show.

Pending defects are the reports that have not yet reached the distinct-bus threshold.
They are real data and useful to an operator, but showing them on the confirmed heatmap
would defeat the point of confirming anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from uip_schema.events import EventType
from uip_central.auth import require_bus_token
from uip_central.config import settings
from uip_central.logic import maintenance
from uip_central.logic.confirmation import RULES

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])


@router.get("/pending/{event_type}")
async def pending(
    request: Request,
    event_type: EventType,
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    """Reports awaiting corroboration: near no confirmed cluster, and not cooled down."""
    rule = RULES.get(event_type)
    if rule is None:
        return {"type": "FeatureCollection", "features": [],
                "note": f"{event_type} is never confirmed, so it is never pending"}

    table = "confirmed_potholes" if event_type is EventType.POTHOLE else "confirmed_waterlogging"
    sql = f"""
        SELECT json_build_object(
            'type', 'FeatureCollection',
            'features', COALESCE(json_agg(json_build_object(
                'type', 'Feature',
                'geometry', ST_AsGeoJSON(COALESCE(r.defect_geog, r.bus_geog))::json,
                'properties', json_build_object(
                    'event_id', r.event_id,
                    'bus_id', r.bus_id,
                    'timestamp', r.event_time,
                    'confidence', round(r.confidence::numeric, 3),
                    'extra', r.extra
                )
            )), '[]'::json)
        ) AS fc
        FROM (
            SELECT * FROM raw_events r
            WHERE r.event_type = %(event_type)s
              AND r.counts_toward_confirmation
              AND NOT EXISTS (
                  SELECT 1 FROM {table} c
                  WHERE c.status = 'confirmed'
                    AND ST_DWithin(c.geog, COALESCE(r.defect_geog, r.bus_geog), %(radius)s)
              )
            ORDER BY r.event_time DESC
            LIMIT %(limit)s
        ) r
    """
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(
            sql,
            {"event_type": str(event_type), "radius": rule.radius_m, "limit": limit},
        )).fetchone()
    return row["fc"]


@router.get("/stats")
async def stats(request: Request) -> dict:
    """One-glance view of what the fleet has reported and what survived confirmation."""
    async with request.app.state.pool.connection() as conn:
        by_type = await (await conn.execute(
            """
            SELECT event_type,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT counts_toward_confirmation) AS cooldown_suppressed,
                   COUNT(DISTINCT bus_id) AS reporting_buses,
                   MIN(event_time) AS earliest,
                   MAX(event_time) AS latest
            FROM raw_events GROUP BY event_type ORDER BY event_type
            """
        )).fetchall()
        potholes = await (await conn.execute(
            "SELECT status, COUNT(*) AS n FROM confirmed_potholes GROUP BY status"
        )).fetchall()
        water = await (await conn.execute(
            "SELECT status, COUNT(*) AS n FROM confirmed_waterlogging GROUP BY status"
        )).fetchall()

    return {
        "raw_events": {r["event_type"]: r for r in by_type},
        "confirmed_potholes": {r["status"]: r["n"] for r in potholes},
        "confirmed_waterlogging": {r["status"]: r["n"] for r in water},
        "thresholds": {
            "pothole": {"radius_m": settings.pothole_radius_m,
                        "window_days": settings.pothole_window_days,
                        "min_confirmations": settings.pothole_min_confirmations},
            "waterlogging": {"radius_m": settings.waterlogging_radius_m,
                             "window_hours": settings.waterlogging_window_hours,
                             "min_confirmations": settings.waterlogging_min_confirmations},
        },
    }


@router.post("/reap", dependencies=[Depends(require_bus_token)])
async def reap(request: Request) -> dict:
    """Run the staleness pass on demand (it also runs on a timer)."""
    async with request.app.state.pool.connection() as conn:
        async with conn.transaction():
            return await maintenance.run_all(conn)
