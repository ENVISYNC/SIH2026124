"""GeoJSON layer endpoints - everything the Leaflet map draws.

Each layer is built entirely in PostGIS (ST_AsGeoJSON + json_agg) and returned in one
round trip, so there is no serialisation layer to keep in sync.

Read endpoints are unauthenticated: the map is a static page in this prototype.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

from uip_central.config import settings

router = APIRouter(prefix="/api/v1/layers", tags=["layers"])

#: optional viewport filter, "minlon,minlat,maxlon,maxlat"
BBOX_FILTER = """
AND (%(bbox)s::text IS NULL OR ST_Intersects(
        {geom_col}::geometry,
        ST_MakeEnvelope(%(minlon)s, %(minlat)s, %(maxlon)s, %(maxlat)s, 4326)))
"""


def _bbox_params(bbox: str | None) -> dict:
    if not bbox:
        return {"bbox": None, "minlon": None, "minlat": None, "maxlon": None, "maxlat": None}
    minlon, minlat, maxlon, maxlat = (float(v) for v in bbox.split(","))
    return {"bbox": bbox, "minlon": minlon, "minlat": minlat,
            "maxlon": maxlon, "maxlat": maxlat}


def _collection(features_sql: str) -> str:
    return f"""
    SELECT json_build_object(
        'type', 'FeatureCollection',
        'features', COALESCE(json_agg(f.feature), '[]'::json)
    ) AS fc
    FROM ({features_sql}) f
    """


async def _fetch(request: Request, sql: str, params: dict) -> dict:
    async with request.app.state.pool.connection() as conn:
        row = await (await conn.execute(sql, params)).fetchone()
    return row["fc"]


@router.get("/potholes")
async def potholes(
    request: Request,
    bbox: str | None = Query(None, description="minlon,minlat,maxlon,maxlat"),
    status: str = Query("confirmed", pattern="^(confirmed|resolved|all)$"),
) -> dict:
    """Confirmed potholes only - pending reports stay in the ops view.

    `weight` is what Leaflet.heat uses for intensity: more corroborating buses and a
    worse severity make a hotter point.
    """
    sql = _collection(f"""
        SELECT json_build_object(
            'type', 'Feature',
            'geometry', ST_AsGeoJSON(geog)::json,
            'properties', json_build_object(
                'id', id,
                'report_count', report_count,
                'distinct_buses', distinct_buses,
                'severity', severity,
                'avg_confidence', round(avg_confidence::numeric, 3),
                'first_seen_at', first_seen_at,
                'last_seen_at', last_seen_at,
                'status', status,
                'weight', LEAST(1.0, (distinct_buses::float / 5.0)
                          * CASE severity WHEN 'high' THEN 1.0
                                          WHEN 'medium' THEN 0.7
                                          ELSE 0.4 END)
            )
        ) AS feature
        FROM confirmed_potholes
        WHERE (%(status)s = 'all' OR status = %(status)s)
        {BBOX_FILTER.format(geom_col="geog")}
    """)
    return await _fetch(request, sql, {**_bbox_params(bbox), "status": status})


@router.get("/waterlogging")
async def waterlogging(
    request: Request,
    bbox: str | None = Query(None),
    status: str = Query("confirmed", pattern="^(confirmed|expired|all)$"),
) -> dict:
    """Confirmed waterlogging. Transient by nature - the reaper expires these."""
    sql = _collection(f"""
        SELECT json_build_object(
            'type', 'Feature',
            'geometry', ST_AsGeoJSON(geog)::json,
            'properties', json_build_object(
                'id', id,
                'report_count', report_count,
                'distinct_buses', distinct_buses,
                'avg_coverage_pct', round(avg_coverage_pct::numeric, 1),
                'avg_confidence', round(avg_confidence::numeric, 3),
                'first_seen_at', first_seen_at,
                'last_seen_at', last_seen_at,
                'status', status,
                'weight', LEAST(1.0, COALESCE(avg_coverage_pct, 0) / 100.0)
            )
        ) AS feature
        FROM confirmed_waterlogging
        WHERE (%(status)s = 'all' OR status = %(status)s)
        {BBOX_FILTER.format(geom_col="geog")}
    """)
    return await _fetch(request, sql, {**_bbox_params(bbox), "status": status})


@router.get("/congestion")
async def congestion(
    request: Request,
    bbox: str | None = Query(None),
    minutes: int = Query(60, ge=1, le=60 * 24 * 7, description="look-back window"),
    collapse: bool = Query(True, description="one feature per cell averaged over the "
                                             "window; set false for a bucketed series "
                                             "suitable for a time slider"),
    stat: str = Query("p90", pattern="^(avg|p90|max)$",
                     description="which statistic over the window becomes the heat "
                                 "weight. Averaging a 24 h window buries a 40 minute "
                                 "jam under 23 hours of free flow, so the default is "
                                 "the 90th percentile - 'how bad does this road get'"),
    grid_m: int | None = Query(None, ge=50, le=2000,
                               description="aggregation cell size; defaults to the "
                                           "configured grid. A client zoomed out asks "
                                           "for a coarser one so that cells do not "
                                           "land closer together than it can draw them"),
) -> dict:
    """Congestion is AGGREGATED, never confirmed.

    Counting vehicle classes is a high-confidence task, so one bus's reading is directly
    usable - requiring several buses per cell per bucket would leave the map empty.

    Aggregation happens at query time (grid cell x time bucket) rather than in a
    materialised table: at prototype volume it is fast enough and there is no staleness
    to manage. Cells are snapped in EPSG:3857, whose metres are inflated by 1/cos(lat)
    (~5.4% at Pune's latitude) - irrelevant for a heatmap, but it is an approximation.

    `collapse=true` (the default) returns ONE feature per grid cell, averaged over the
    whole window - what a heatmap wants. `collapse=false` keeps the time buckets
    separate, so the same cell appears once per bucket; that is what a time slider
    needs, but it multiplies the feature count by the number of buckets.

    `stat` decides what the heat weight means. A congestion heatmap answers "how bad
    does this road get", and the mean over a long window does not: 23 hours of free flow
    drown a 40 minute jam, so every road converges on the same lukewarm average and the
    map goes flat. The default is the 90th percentile of the readings in the window,
    which is the busy period rather than an average of busy and empty. `avg` and `max`
    are available for the same cells - all three are returned in the properties.

    `grid_m` overrides the cell size. A map at city zoom cannot draw 100 m cells without
    them overlapping, and overlapping heat points accumulate until ordinary traffic looks
    like a jam; asking for a coarser aggregation instead keeps the colour an honest
    average rather than an artefact of how many neighbours bled into a pixel.
    """
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    bucket_s = settings.congestion_bucket_minutes * 60

    sql = _collection(f"""
        SELECT json_build_object(
            'type', 'Feature',
            'geometry', ST_AsGeoJSON(ST_Transform(cell, 4326))::json,
            'properties', json_build_object(
                'bucket_start', bucket_start,
                'sample_count', sample_count,
                'reporting_buses', reporting_buses,
                'avg_vehicle_count', round(avg_vehicle_count::numeric, 1),
                'avg_density_score', round(avg_density_score::numeric, 3),
                'p90_density_score', round(p90_density::numeric, 3),
                'peak_density_score', round(max_density::numeric, 3),
                'avg_bus_speed_kmph', round(avg_bus_speed::numeric, 1),
                'stat', %(stat)s::text,
                'weight', round((CASE %(stat)s::text
                                 WHEN 'avg' THEN avg_density_score
                                 WHEN 'max' THEN max_density
                                 ELSE p90_density END)::numeric, 3)
            )
        ) AS feature
        FROM (
            SELECT ST_SnapToGrid(ST_Transform(bus_geog::geometry, 3857),
                                 %(grid)s, %(grid)s) AS cell,
                   CASE WHEN %(collapse)s THEN NULL ELSE
                        to_timestamp(floor(extract(epoch FROM event_time) / %(bucket_s)s)
                                     * %(bucket_s)s) END AS bucket_start,
                   COUNT(*)                                        AS sample_count,
                   COUNT(DISTINCT bus_id)                          AS reporting_buses,
                   AVG((extra->>'vehicle_count')::float)           AS avg_vehicle_count,
                   AVG((extra->>'density_score')::float)           AS avg_density_score,
                   percentile_cont(0.9) WITHIN GROUP (
                       ORDER BY (extra->>'density_score')::float)  AS p90_density,
                   MAX((extra->>'density_score')::float)           AS max_density,
                   AVG((extra->>'own_speed_kmph')::float)          AS avg_bus_speed
            FROM raw_events
            WHERE event_type = 'congestion'
              AND event_time >= %(since)s
              {BBOX_FILTER.format(geom_col="bus_geog")}
            GROUP BY cell, 2
        ) cells
    """)
    return await _fetch(request, sql, {
        **_bbox_params(bbox),
        "since": since,
        "grid": grid_m or settings.congestion_grid_m,
        "stat": stat,
        "bucket_s": bucket_s,
        "collapse": collapse,
    })


@router.get("/incidents")
async def incidents(
    request: Request,
    bbox: str | None = Query(None),
    hours: int = Query(24, ge=1, le=24 * 30),
    min_confidence: float | None = Query(None, ge=0, le=1),
) -> dict:
    """Individual incident markers - no confirmation, no aggregation.

    A single high-confidence report is enough, and each incident is a case with plate
    data rather than a point on a density surface.

    NOTE: plate numbers are personal data. This endpoint is open in the prototype;
    production needs access control, a retention window and an audit trail.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    threshold = (min_confidence if min_confidence is not None
                 else settings.incident_min_confidence)

    sql = _collection(f"""
        SELECT json_build_object(
            'type', 'Feature',
            'geometry', ST_AsGeoJSON(bus_geog)::json,
            'properties', json_build_object(
                'event_id', event_id,
                'bus_id', bus_id,
                'camera_id', camera_id,
                'subtype', extra->>'subtype',
                'timestamp', event_time,
                'received_at', received_at,
                'confidence', round(confidence::numeric, 3),
                'vehicles_involved', extra->'vehicles_involved',
                'trajectory_anomaly_score', extra->'trajectory_anomaly_score',
                'contributing_camera_ids', extra->'contributing_camera_ids',
                'impact_signature', extra->'impact_signature',
                'media_ref', media_ref
            )
        ) AS feature
        FROM raw_events
        WHERE event_type = 'incident'
          AND event_time >= %(since)s
          AND confidence >= %(threshold)s
          {BBOX_FILTER.format(geom_col="bus_geog")}
        ORDER BY event_time DESC
    """)
    return await _fetch(request, sql,
                        {**_bbox_params(bbox), "since": since, "threshold": threshold})
