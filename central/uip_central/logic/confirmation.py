"""Multi-bus confirmation for potholes and waterlogging.

The rule, from CLAUDE.md: a road defect is only believed once several DISTINCT buses
report it near the same place inside a time window. One bus reporting the same spot
repeatedly proves nothing - it is one detector, possibly one false positive, seen
several times.

Potholes and waterlogging deliberately do NOT share parameters:
  * a pothole persists for weeks   -> long window, resolution when repaired
  * waterlogging is a rain event   -> short window, expires on its own

Congestion and incidents never come through here (aggregated / single-report).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from psycopg import AsyncConnection, sql

from uip_schema.events import EventType
from uip_central.config import settings

_SEVERITY_RANK = {0: None, 1: "low", 2: "medium", 3: "high"}

#: the position used for clustering: the projected defect position where we could
#: compute one, else the raw bus position
GEOG = sql.SQL("COALESCE({}.defect_geog, {}.bus_geog)")


@dataclass(frozen=True)
class ConfirmRule:
    event_type: EventType
    table: str
    radius_m: float
    window: str  # a Postgres interval literal
    min_confirmations: int
    cooldown: str
    #: low-frequency-route fallback: this many separate passes by the same bus, spanning
    #: at least `min_span_hours`, also confirms. None disables the fallback entirely.
    min_passes: int | None
    min_span_hours: int | None


POTHOLE_RULE = ConfirmRule(
    event_type=EventType.POTHOLE,
    table="confirmed_potholes",
    radius_m=settings.pothole_radius_m,
    window=f"{settings.pothole_window_days} days",
    min_confirmations=settings.pothole_min_confirmations,
    cooldown=f"{settings.pothole_cooldown_minutes} minutes",
    min_passes=settings.pothole_single_bus_min_passes,
    min_span_hours=settings.pothole_single_bus_min_span_hours,
)

WATERLOGGING_RULE = ConfirmRule(
    event_type=EventType.WATERLOGGING,
    table="confirmed_waterlogging",
    radius_m=settings.waterlogging_radius_m,
    window=f"{settings.waterlogging_window_hours} hours",
    min_confirmations=settings.waterlogging_min_confirmations,
    cooldown=f"{settings.waterlogging_cooldown_minutes} minutes",
    #: no repeat-pass fallback: water does not persist, so a later visit sees a
    #: different flood rather than more evidence for this one
    min_passes=None,
    min_span_hours=None,
)

RULES: dict[EventType, ConfirmRule] = {
    EventType.POTHOLE: POTHOLE_RULE,
    EventType.WATERLOGGING: WATERLOGGING_RULE,
}


async def apply_cooldown(conn: AsyncConnection, event_id: UUID, rule: ConfirmRule) -> bool:
    """Suppress a repeat report from the SAME bus at the same spot.

    The row is kept for audit but flagged out of the confirmation count, so a single
    bus passing a pothole six times a day cannot confirm it alone.
    """
    row = await (await conn.execute(
        """
        WITH anchor AS (
            SELECT event_id, bus_id, event_time,
                   COALESCE(defect_geog, bus_geog) AS g
            FROM raw_events WHERE event_id = %(event_id)s
        )
        SELECT EXISTS (
            SELECT 1 FROM raw_events r, anchor a
            WHERE r.event_type   = %(event_type)s
              AND r.bus_id       = a.bus_id
              AND r.event_id    <> a.event_id
              AND r.counts_toward_confirmation
              AND r.event_time  >  a.event_time - %(cooldown)s::interval
              AND r.event_time  <= a.event_time
              AND ST_DWithin(COALESCE(r.defect_geog, r.bus_geog), a.g, %(radius)s)
        ) AS suppressed
        """,
        {
            "event_id": event_id,
            "event_type": str(rule.event_type),
            "cooldown": rule.cooldown,
            "radius": rule.radius_m,
        },
    )).fetchone()

    if row["suppressed"]:
        await conn.execute(
            "UPDATE raw_events SET counts_toward_confirmation = FALSE WHERE event_id = %s",
            (event_id,),
        )
    return bool(row["suppressed"])


async def _cluster_stats(conn: AsyncConnection, event_id: UUID, rule: ConfirmRule) -> dict:
    """Summarise the reports clustered around this event.

    The cluster is ANCHORED on the new event rather than grown transitively, which
    bounds its diameter at 2*radius. Chained "within 20 m of a neighbour" links would
    otherwise smear a single cluster down an entire street.

    The window is symmetric around the anchor's event_time, not "the last N days":
    buses buffer offline, so an event can arrive long after the neighbours it belongs with.
    """
    extra_agg = (
        sql.SQL(
            "MAX(CASE n.extra->>'severity' WHEN 'high' THEN 3 WHEN 'medium' THEN 2 "
            "WHEN 'low' THEN 1 ELSE 0 END) AS severity_rank, NULL::float AS avg_coverage_pct"
        )
        if rule.event_type is EventType.POTHOLE
        else sql.SQL(
            "0 AS severity_rank, AVG((n.extra->>'coverage_pct')::float) AS avg_coverage_pct"
        )
    )

    query = sql.SQL(
        """
        WITH anchor AS (
            SELECT event_time, COALESCE(defect_geog, bus_geog) AS g
            FROM raw_events WHERE event_id = %(event_id)s
        ),
        nearby AS (
            SELECT r.*
            FROM raw_events r, anchor a
            WHERE r.event_type = %(event_type)s
              AND r.counts_toward_confirmation
              AND r.event_time BETWEEN a.event_time - %(window)s::interval
                                   AND a.event_time + %(window)s::interval
              AND ST_DWithin(COALESCE(r.defect_geog, r.bus_geog), a.g, %(radius)s)
        )
        SELECT COUNT(*)                              AS report_count,
               COUNT(DISTINCT n.bus_id)              AS distinct_buses,
               EXTRACT(EPOCH FROM MAX(n.event_time) - MIN(n.event_time)) / 3600.0
                                                     AS span_hours,
               AVG(n.confidence)                     AS avg_confidence,
               MIN(n.event_time)                     AS first_seen_at,
               MAX(n.event_time)                     AS last_seen_at,
               ST_AsEWKT(ST_Centroid(ST_Collect(
                   COALESCE(n.defect_geog, n.bus_geog)::geometry))) AS centroid,
               {extra_agg}
        FROM nearby n
        """
    ).format(extra_agg=extra_agg)

    return await (await conn.execute(
        query,
        {
            "event_id": event_id,
            "event_type": str(rule.event_type),
            "window": rule.window,
            "radius": rule.radius_m,
        },
    )).fetchone()


def _is_confirmed(stats: dict, rule: ConfirmRule) -> bool:
    """Independent buses are the primary evidence; repeated passes are the fallback."""
    if stats["distinct_buses"] >= rule.min_confirmations:
        return True
    if rule.min_passes is None:
        return False
    # A route served by one or two buses can never reach the distinct-bus bar. Each
    # pass past the same spot is an independent look, so enough of them - spread over
    # enough time that a transient cannot masquerade as a defect - also confirms.
    # Cooldown-suppressed reports are already excluded, so every counted report here
    # is a separate pass.
    return (stats["report_count"] >= rule.min_passes
            and (stats["span_hours"] or 0) >= rule.min_span_hours)


async def _upsert_pothole(conn: AsyncConnection, stats: dict, rule: ConfirmRule) -> int:
    """Merge into an existing nearby cluster if there is one, else create it.

    A pothole that reappears after being marked resolved is re-opened rather than
    duplicated - that usually means the repair did not hold.
    """
    row = await (await conn.execute(
        """
        UPDATE confirmed_potholes SET
            geog           = %(centroid)s::geography,
            report_count   = %(report_count)s,
            distinct_buses = %(distinct_buses)s,
            avg_confidence = %(avg_confidence)s,
            severity       = %(severity)s,
            first_seen_at  = LEAST(first_seen_at, %(first_seen_at)s),
            last_seen_at   = GREATEST(last_seen_at, %(last_seen_at)s),
            status         = 'confirmed',
            resolved_at    = NULL,
            updated_at     = now()
        WHERE id = (
            SELECT id FROM confirmed_potholes
            WHERE ST_DWithin(geog, %(centroid)s::geography, %(radius)s)
            ORDER BY ST_Distance(geog, %(centroid)s::geography) LIMIT 1
        )
        RETURNING id
        """,
        {**stats, "severity": _SEVERITY_RANK[stats["severity_rank"]], "radius": rule.radius_m},
    )).fetchone()
    if row:
        return row["id"]

    row = await (await conn.execute(
        """
        INSERT INTO confirmed_potholes
            (geog, report_count, distinct_buses, avg_confidence, severity,
             first_seen_at, last_seen_at, status)
        VALUES (%(centroid)s::geography, %(report_count)s, %(distinct_buses)s, %(avg_confidence)s,
                %(severity)s, %(first_seen_at)s, %(last_seen_at)s, 'confirmed')
        RETURNING id
        """,
        {**stats, "severity": _SEVERITY_RANK[stats["severity_rank"]]},
    )).fetchone()
    return row["id"]


async def _upsert_waterlogging(conn: AsyncConnection, stats: dict, rule: ConfirmRule) -> int:
    row = await (await conn.execute(
        """
        UPDATE confirmed_waterlogging SET
            geog             = %(centroid)s::geography,
            report_count     = %(report_count)s,
            distinct_buses   = %(distinct_buses)s,
            avg_confidence   = %(avg_confidence)s,
            avg_coverage_pct = %(avg_coverage_pct)s,
            first_seen_at    = LEAST(first_seen_at, %(first_seen_at)s),
            last_seen_at     = GREATEST(last_seen_at, %(last_seen_at)s),
            status           = 'confirmed',
            updated_at       = now()
        WHERE id = (
            SELECT id FROM confirmed_waterlogging
            WHERE ST_DWithin(geog, %(centroid)s::geography, %(radius)s)
              AND status = 'confirmed'
            ORDER BY ST_Distance(geog, %(centroid)s::geography) LIMIT 1
        )
        RETURNING id
        """,
        {**stats, "radius": rule.radius_m},
    )).fetchone()
    if row:
        return row["id"]

    row = await (await conn.execute(
        """
        INSERT INTO confirmed_waterlogging
            (geog, report_count, distinct_buses, avg_confidence, avg_coverage_pct,
             first_seen_at, last_seen_at, status)
        VALUES (%(centroid)s::geography, %(report_count)s, %(distinct_buses)s, %(avg_confidence)s,
                %(avg_coverage_pct)s, %(first_seen_at)s, %(last_seen_at)s, 'confirmed')
        RETURNING id
        """,
        stats,
    )).fetchone()
    return row["id"]


async def evaluate(conn: AsyncConnection, event_id: UUID, event_type: EventType) -> dict | None:
    """Run confirmation for one freshly ingested event.

    Returns None for event types that are never confirmed (congestion, incidents,
    infra_defect) and for reports suppressed by the same-bus cooldown.
    """
    rule = RULES.get(event_type)
    if rule is None:
        return None

    if await apply_cooldown(conn, event_id, rule):
        return {"cooldown_suppressed": True, "confirmed": False}

    stats = await _cluster_stats(conn, event_id, rule)
    if not _is_confirmed(stats, rule):
        # stays pending: visible to ops, absent from the public confirmed heatmap
        return {"cooldown_suppressed": False, "confirmed": False,
                "distinct_buses": stats["distinct_buses"],
                "report_count": stats["report_count"]}

    upsert = _upsert_pothole if event_type is EventType.POTHOLE else _upsert_waterlogging
    cluster_id = await upsert(conn, stats, rule)
    return {
        "cooldown_suppressed": False,
        "confirmed": True,
        "cluster_id": cluster_id,
        "distinct_buses": stats["distinct_buses"],
        "report_count": stats["report_count"],
    }
