"""Ingestion API - the endpoint real edge devices will eventually speak to.

Two properties matter more than throughput here:
  * idempotency - buses buffer offline and retry, so the same event_id may arrive twice
  * per-event isolation - one malformed or conflicting event must not roll back a whole
    batch uploaded after a connectivity gap
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from uip_schema.events import Event, EventAck, EventBatch, IngestResponse
from uip_central.auth import require_bus_token
from uip_central.logic import confirmation

router = APIRouter(prefix="/api/v1", tags=["ingest"])

INSERT_SQL = """
INSERT INTO raw_events (
    event_id, schema_version, bus_id, camera_id, event_type, event_time,
    bus_geog, defect_geog,
    gps_accuracy_m, heading_deg, speed_kmph,
    confidence, model_versions, bbox, media_ref, priority, extra
) VALUES (
    %(event_id)s, %(schema_version)s, %(bus_id)s, %(camera_id)s, %(event_type)s, %(event_time)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
    -- project the bus position forward along its heading to estimate where the object
    -- actually is: the defect sits 10-30 m ahead of the camera, not under the bus
    -- casts are load-bearing: these are NULL for event types that carry no range
    -- (congestion), and an untyped NULL makes the ST_Project overload ambiguous
    CASE WHEN %(heading_deg)s::float8 IS NOT NULL AND %(est_range_m)s::float8 IS NOT NULL
         THEN ST_Project(ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                         %(est_range_m)s::float8, radians(%(heading_deg)s::float8))
    END,
    %(gps_accuracy_m)s, %(heading_deg)s, %(speed_kmph)s,
    %(confidence)s, %(model_versions)s, %(bbox)s, %(media_ref)s, %(priority)s, %(extra)s
)
ON CONFLICT (event_id) DO NOTHING
RETURNING event_id
"""


def _params(event: Event) -> dict:
    return {
        "event_id": str(event.event_id),
        "schema_version": event.schema_version,
        "bus_id": event.bus_id,
        "camera_id": event.camera_id,
        "event_type": str(event.event_type),
        "event_time": event.timestamp,
        "lat": event.gps.lat,
        "lon": event.gps.lon,
        "gps_accuracy_m": event.gps_accuracy_m,
        "heading_deg": event.heading_deg,
        "speed_kmph": event.speed_kmph,
        "est_range_m": event.extra.get("est_range_m"),
        "confidence": event.confidence,
        "model_versions": Jsonb(event.model_versions),
        "bbox": Jsonb(event.bbox.model_dump()) if event.bbox else None,
        "media_ref": event.media_ref,
        "priority": str(event.priority),
        "extra": Jsonb(event.extra),
    }


def _summarise(exc: ValidationError) -> str:
    err = exc.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"


@router.post("/events", response_model=IngestResponse, dependencies=[Depends(require_bus_token)])
async def ingest(batch: EventBatch, request: Request) -> IngestResponse:
    """Accept a batch of events from one bus.

    A realtime-lane incident is simply a batch of one, sent the moment it is detected
    rather than waiting for the next buffer flush.

    Events are validated and stored INDIVIDUALLY. A batch arriving after a connectivity
    gap is a backlog, so one malformed event is rejected on its own and reported back
    rather than taking the rest of the backlog down with it. See `Event` in
    `common/events.py` for the payload schema.
    """
    pool = request.app.state.pool
    results: list[EventAck] = []

    async with pool.connection() as conn:
        for raw in batch.events:
            try:
                event = Event.model_validate(raw)
            except ValidationError as exc:
                results.append(EventAck(
                    event_id=raw.get("event_id") if isinstance(raw.get("event_id"), str) else None,
                    status="rejected",
                    error=_summarise(exc),
                ))
                continue

            # one transaction per event so a single failure cannot discard the batch
            async with conn.transaction():
                row = await (await conn.execute(INSERT_SQL, _params(event))).fetchone()
                if row is None:
                    results.append(EventAck(event_id=event.event_id, status="duplicate"))
                    continue

                outcome = await confirmation.evaluate(conn, event.event_id, event.event_type)
                results.append(EventAck(
                    event_id=event.event_id,
                    status="accepted",
                    cooldown_suppressed=bool(outcome and outcome["cooldown_suppressed"]),
                ))

    counts = {"accepted": 0, "duplicate": 0, "rejected": 0}
    for r in results:
        counts[r.status] += 1
    return IngestResponse(
        received=len(results),
        accepted=counts["accepted"],
        duplicates=counts["duplicate"],
        rejected=counts["rejected"],
        results=results,
    )
