"""Demotion of stale confirmed defects.

Without this the map only ever accumulates: potholes get repaired and water drains,
but a confirmed row would sit on the heatmap forever.

The two cases are not symmetric:
  * waterlogging simply expires on a timer - a flood that stopped being reported is over
  * a pothole needs EVIDENCE of absence: buses passed the spot and reported nothing.
    Congestion events are the presence proxy - they are emitted on a fixed cadence
    regardless of defects, so they tell us a bus was there and saw no pothole.
"""

from __future__ import annotations

from psycopg import AsyncConnection, sql

from uip_schema.events import EventType
from uip_central.config import settings
from uip_central.logic.confirmation import RULES

#: how close a bus must have passed for its silence to count as evidence of absence
PASS_RADIUS_M = 30.0


async def resolve_repaired_potholes(conn: AsyncConnection) -> int:
    row = await (await conn.execute(
        """
        WITH stale AS (
            SELECT id, geog, last_seen_at
            FROM confirmed_potholes
            WHERE status = 'confirmed'
              AND last_seen_at < now() - %(stale_after)s::interval
        ),
        passes AS (
            SELECT s.id, COUNT(DISTINCT r.bus_id) AS silent_buses
            FROM stale s
            JOIN raw_events r
              ON r.event_time > s.last_seen_at
             AND ST_DWithin(r.bus_geog, s.geog, %(pass_radius)s)
            GROUP BY s.id
        )
        UPDATE confirmed_potholes p
        SET status = 'resolved', resolved_at = now(), updated_at = now()
        FROM passes
        WHERE p.id = passes.id AND passes.silent_buses >= %(min_passes)s
        RETURNING p.id
        """,
        {
            "stale_after": f"{settings.pothole_resolve_after_days} days",
            "pass_radius": PASS_RADIUS_M,
            "min_passes": settings.pothole_resolve_passes,
        },
    )).fetchall()
    return len(row)


async def expire_waterlogging(conn: AsyncConnection) -> int:
    rows = await (await conn.execute(
        """
        UPDATE confirmed_waterlogging
        SET status = 'expired', updated_at = now()
        WHERE status = 'confirmed'
          AND last_seen_at < now() - %(expire_after)s::interval
        RETURNING id
        """,
        {"expire_after": f"{settings.waterlogging_expire_after_hours} hours"},
    )).fetchall()
    return len(rows)


async def _fragment_groups(conn: AsyncConnection, event_type: EventType) -> list[list[int]]:
    """Group confirmed clusters that are really one defect.

    The criterion is SHARED EVIDENCE, not distance: two clusters are the same defect if
    the same raw reports fall within the confirmation radius of both centroids. A fixed
    merge distance cannot work here - fragments of one pothole sit anywhere from 8 to
    40 m apart depending on how the reports scattered, and any threshold wide enough to
    catch them would also merge genuinely distinct potholes on the same street. Two real
    potholes 40 m apart share no reports, so this rule leaves them alone.
    """
    rule = RULES[event_type]
    table = sql.Identifier(rule.table)

    pairs = await (await conn.execute(
        sql.SQL("""
            SELECT a.id AS a_id, b.id AS b_id
            FROM {table} a
            JOIN {table} b ON b.id > a.id
                          AND b.status = 'confirmed'
                          AND ST_DWithin(a.geog, b.geog, %(span)s)
            JOIN raw_events r
              ON r.event_type = %(event_type)s
             AND r.counts_toward_confirmation
             AND ST_DWithin(COALESCE(r.defect_geog, r.bus_geog), a.geog, %(radius)s)
             AND ST_DWithin(COALESCE(r.defect_geog, r.bus_geog), b.geog, %(radius)s)
            WHERE a.status = 'confirmed'
            GROUP BY a.id, b.id
            HAVING COUNT(*) >= %(min_shared)s
        """).format(table=table),
        {"event_type": str(event_type), "radius": rule.radius_m,
         "span": rule.radius_m * 2, "min_shared": settings.cluster_merge_min_shared},
    )).fetchall()

    # union-find, so a chain of overlapping fragments collapses into one group
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pair in pairs:
        a, b = find(pair["a_id"]), find(pair["b_id"])
        if a != b:
            parent[a] = b

    groups: dict[int, list[int]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    return [g for g in groups.values() if len(g) > 1]


async def merge_fragmented(conn: AsyncConnection, event_type: EventType) -> int:
    """Collapse several confirmed clusters that are really one defect.

    Clusters are created anchored on an incoming event, and a new one is only merged
    into an existing row whose CENTROID is within the radius. Early on, before either
    centroid has settled, two sub-groups of reports for the same pothole can end up
    further apart than that - and once both rows exist, nothing brings them back
    together. The result is one physical pothole counted as two or three.

    The survivor is recomputed from the raw reports around the merged position, so it
    ends up identical to what a single unfragmented cluster would have produced.

    One pass is not always enough: merging moves centroids, which can bring a third
    fragment into range. `merge_fragmented_until_stable` repeats until nothing moves.
    """
    rule = RULES[event_type]
    table = sql.Identifier(rule.table)
    is_pothole = event_type is EventType.POTHOLE

    merged = 0
    for group in await _fragment_groups(conn, event_type):
        ordered = await (await conn.execute(
            sql.SQL("SELECT id FROM {table} WHERE id = ANY(%(ids)s) "
                    "ORDER BY report_count DESC").format(table=table),
            {"ids": group},
        )).fetchall()
        ids = [r["id"] for r in ordered]
        survivor, losers = ids[0], ids[1:]

        centroid = await (await conn.execute(
            sql.SQL("SELECT ST_AsEWKT(ST_Centroid(ST_Collect(geog::geometry))) AS c "
                    "FROM {table} WHERE id = ANY(%(ids)s)").format(table=table),
            {"ids": ids},
        )).fetchone()

        await conn.execute(
            sql.SQL("DELETE FROM {table} WHERE id = ANY(%(ids)s)").format(table=table),
            {"ids": losers},
        )

        extra_set = (
            sql.SQL("severity = CASE s.severity_rank WHEN 3 THEN 'high' "
                    "WHEN 2 THEN 'medium' WHEN 1 THEN 'low' END")
            if is_pothole else
            sql.SQL("avg_coverage_pct = s.avg_coverage_pct")
        )
        extra_sel = (
            sql.SQL("MAX(CASE r.extra->>'severity' WHEN 'high' THEN 3 "
                    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END) AS severity_rank")
            if is_pothole else
            sql.SQL("AVG((r.extra->>'coverage_pct')::float) AS avg_coverage_pct")
        )

        await conn.execute(
            sql.SQL("""
                UPDATE {table} c SET
                    geog           = s.centroid,
                    report_count   = s.report_count,
                    distinct_buses = s.distinct_buses,
                    avg_confidence = s.avg_confidence,
                    first_seen_at  = s.first_seen_at,
                    last_seen_at   = s.last_seen_at,
                    {extra_set},
                    updated_at     = now()
                FROM (
                    SELECT ST_Centroid(ST_Collect(
                               COALESCE(r.defect_geog, r.bus_geog)::geometry))::geography
                               AS centroid,
                           COUNT(*)                 AS report_count,
                           COUNT(DISTINCT r.bus_id) AS distinct_buses,
                           AVG(r.confidence)        AS avg_confidence,
                           MIN(r.event_time)        AS first_seen_at,
                           MAX(r.event_time)        AS last_seen_at,
                           {extra_sel}
                    FROM raw_events r
                    WHERE r.event_type = %(event_type)s
                      AND r.counts_toward_confirmation
                      AND ST_DWithin(COALESCE(r.defect_geog, r.bus_geog),
                                     %(centroid)s::geography, %(radius)s)
                ) s
                WHERE c.id = %(survivor)s
            """).format(table=table, extra_set=extra_set, extra_sel=extra_sel),
            {"event_type": str(event_type), "centroid": centroid["c"],
             "radius": rule.radius_m, "survivor": survivor},
        )
        merged += len(losers)

    return merged


async def merge_fragmented_until_stable(conn: AsyncConnection, event_type: EventType,
                                        max_passes: int = 5) -> int:
    """Merging moves centroids, so repeat until a pass changes nothing."""
    total = 0
    for _ in range(max_passes):
        merged = await merge_fragmented(conn, event_type)
        total += merged
        if merged == 0:
            break
    return total


async def run_all(conn: AsyncConnection) -> dict[str, int]:
    return {
        "potholes_resolved": await resolve_repaired_potholes(conn),
        "waterlogging_expired": await expire_waterlogging(conn),
        "pothole_fragments_merged": await merge_fragmented_until_stable(
            conn, EventType.POTHOLE),
        "waterlogging_fragments_merged": await merge_fragmented_until_stable(
            conn, EventType.WATERLOGGING),
    }
