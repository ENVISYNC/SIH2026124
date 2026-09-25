"""Event schema for the urban intelligence platform.

Defined ONCE here and imported by both the central server (to validate ingest) and
the simulated edge data generator (to construct events). When real edge devices are
built, this file is the contract they must satisfy.

Mirrors the "Event schema" section of CLAUDE.md.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: 0.2.0 adds `surface_defect` and `pedestrian_risk` (Architecture.md 6.6).
#:
#: The version field exists precisely so a fleet running mixed firmware during a staged
#: rollout stays ingestible: events stamped "0.1.0" still validate unchanged, because
#: every field added here is a NEW event type rather than a change to an existing one.
#: Nothing that validated before stops validating.
SCHEMA_VERSION = "0.2.0"


class EventType(StrEnum):
    POTHOLE = "pothole"
    WATERLOGGING = "waterlogging"
    CONGESTION = "congestion"
    INFRA_DEFECT = "infra_defect"
    INCIDENT = "incident"
    #: 0.2.0. Distinct from `pothole` on purpose: a pothole is patched, whereas
    #: cracking means the pavement structure is failing and needs resurfacing
    #: (Architecture.md 5.3.1). Different defect, different maintenance action.
    SURFACE_DEFECT = "surface_defect"
    #: 0.2.0. Answers O11 "vulnerable pedestrian situations".
    PEDESTRIAN_RISK = "pedestrian_risk"


class Priority(StrEnum):
    #: incident events - sent immediately, bypassing the on-bus batch buffer
    REALTIME = "realtime"
    #: everything else - accumulated locally and flushed when connectivity allows
    BATCH = "batch"


class LanePosition(StrEnum):
    LEFT = "left"
    CENTRE = "centre"
    RIGHT = "right"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class GPS(BaseModel):
    """Position of the BUS at detection time - not of the detected object.

    A pothole sits 10-30 m ahead of the bus, so the server projects an estimated
    defect position from this point using heading_deg + extra.est_range_m.
    """

    lat: Latitude
    lon: Longitude


class BBox(BaseModel):
    """Normalised [0,1] image coordinates, so it is resolution independent."""

    x: Annotated[float, Field(ge=0, le=1)]
    y: Annotated[float, Field(ge=0, le=1)]
    w: Annotated[float, Field(gt=0, le=1)]
    h: Annotated[float, Field(gt=0, le=1)]


# --------------------------------------------------------------------------------------
# Per-event-type "extra" payloads
# --------------------------------------------------------------------------------------


class PotholeExtra(BaseModel):
    #: heuristic: bbox area normalised against lane width at that image row
    severity: Severity
    lane_position: LanePosition = LanePosition.UNKNOWN
    #: bus -> defect distance, used to project the true defect position
    est_range_m: Annotated[float, Field(ge=0, le=100)] | None = None
    road_surface: str | None = None


class WaterloggingExtra(BaseModel):
    #: approximated from bbox area fraction while Model A stays bbox-only
    coverage_pct: Annotated[float, Field(ge=0, le=100)]
    lane_position: LanePosition = LanePosition.UNKNOWN
    est_range_m: Annotated[float, Field(ge=0, le=100)] | None = None


class CongestionExtra(BaseModel):
    """Frame-level aggregate over a window - NOT a single detection (hence no bbox)."""

    vehicle_count: Annotated[int, Field(ge=0)]
    vehicle_class_breakdown: dict[str, int] = Field(default_factory=dict)
    #: the BUS's own speed from GPS. Other-vehicle speed from a monocular moving
    #: camera is unreliable, so this is the congestion proxy.
    own_speed_kmph: Annotated[float, Field(ge=0)]
    density_score: Annotated[float, Field(ge=0, le=1)]
    #: aggregation window this count covers, in seconds
    window_s: Annotated[float, Field(gt=0)] = 30.0
    #: requires map-matching against OSM way ids, which does not exist yet
    segment_id: str | None = None


class SurfaceDefectExtra(BaseModel):
    """Damaged road surface as distinct from a pothole (Architecture.md 5.3.1).

    Confirms like a pothole - a persistent physical thing, multi-bus agreement, and
    resolution when the road is resurfaced.
    """

    subtype: Literal["longitudinal_crack", "transverse_crack", "alligator_crack",
                     "ravelling", "rutting", "edge_break", "failed_patch"]
    severity: Severity
    #: length of the affected stretch, where the detector can estimate it
    extent_m: Annotated[float, Field(ge=0, le=500)] | None = None
    lane_position: LanePosition = LanePosition.UNKNOWN
    est_range_m: Annotated[float, Field(ge=0, le=100)] | None = None


class PedestrianRiskExtra(BaseModel):
    """A vulnerable pedestrian situation (Architecture.md 5.3.5).

    Carries COUNTS, POSITIONS AND TRAJECTORIES ONLY - never images or identities. There
    is deliberately no age field: age classification from a moving bus is unreliable in
    exactly the conditions it would exist for, and running it on video of children is
    its own problem. The schema is where that decision is made unavailable, not merely
    discouraged.

    Aggregates into hotspots like congestion (7.9.4); high-severity `near_miss` may also
    go out on the realtime lane.
    """

    subtype: Literal["school_zone_crossing", "unprotected_crossing",
                     "pedestrian_in_carriageway", "near_miss"]
    pedestrian_count: Annotated[int, Field(ge=0)] = 1
    #: pedestrians moving together; a secondary signal only, never the trigger
    group_size: Annotated[int, Field(ge=0)] = 1
    #: smallest time-to-collision observed, seconds. Includes the bus itself as a
    #: hazard - 5.3.5 point 2 makes this a driver-safety signal, not only a planning one.
    min_ttc_s: Annotated[float, Field(ge=0)] | None = None
    #: needs the GIS crossing layer; null until that layer exists
    nearest_crossing_distance_m: Annotated[float, Field(ge=0)] | None = None
    #: needs the school-zone geofence layer; null until that layer exists
    school_zone_id: str | None = None
    own_speed_kmph: Annotated[float, Field(ge=0)] | None = None


class InfraDefectExtra(BaseModel):
    #: only damaged_signboard is edge-detectable. The missing_* subtypes are
    #: central-side inferences (absence cannot be detected by a bbox model).
    subtype: Literal["damaged_signboard"]


class VehicleInvolved(BaseModel):
    """One vehicle in an incident. ANPR reads every plate visible on any camera."""

    plate_number: str | None = None
    plate_confidence: Confidence | None = None
    vehicle_class: str | None = None
    camera_id: str | None = None


class IncidentExtra(BaseModel):
    #: hit_and_run is deliberately absent - it is a central-side correlation
    subtype: Literal["accident", "rash_driving"]
    vehicles_involved: list[VehicleInvolved] = Field(default_factory=list)
    trajectory_anomaly_score: Annotated[float, Field(ge=0, le=1)] | None = None
    contributing_camera_ids: list[str] = Field(default_factory=list)
    #: IMU peak acceleration (g) corroborating an impact
    impact_signature: float | None = None


EXTRA_MODELS: dict[EventType, type[BaseModel]] = {
    EventType.POTHOLE: PotholeExtra,
    EventType.WATERLOGGING: WaterloggingExtra,
    EventType.CONGESTION: CongestionExtra,
    EventType.INFRA_DEFECT: InfraDefectExtra,
    EventType.SURFACE_DEFECT: SurfaceDefectExtra,
    EventType.PEDESTRIAN_RISK: PedestrianRiskExtra,
    EventType.INCIDENT: IncidentExtra,
}


# --------------------------------------------------------------------------------------
# The event itself
# --------------------------------------------------------------------------------------


class Event(BaseModel):
    """A single distilled event from one bus. Raw video never travels with it."""

    model_config = ConfigDict(extra="forbid")

    #: generated ON THE BUS so a retry after buffered upload is idempotent
    event_id: UUID
    schema_version: str = SCHEMA_VERSION
    bus_id: str = Field(min_length=1, max_length=64)
    camera_id: str = Field(min_length=1, max_length=64)
    event_type: EventType

    #: event time from the bus clock (NTP-synced). NOT ingest time - buses buffer
    #: offline and may upload hours late; the server stamps received_at itself.
    timestamp: datetime

    gps: GPS
    gps_accuracy_m: Annotated[float, Field(ge=0)] | None = None
    heading_deg: Annotated[float, Field(ge=0, lt=360)] | None = None
    speed_kmph: Annotated[float, Field(ge=0)] | None = None

    confidence: Confidence
    #: model -> version. An incident involves Model C *and* Model D (ANPR).
    model_versions: dict[str, str] = Field(default_factory=dict)

    #: absent for frame-level aggregates such as congestion
    bbox: BBox | None = None
    #: a POINTER into the on-bus rolling buffer, or an object store key after upload
    media_ref: str | None = None

    priority: Priority = Priority.BATCH
    extra: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_extra_for_type(self):
        """Parse `extra` with the model matching event_type, so a malformed event
        fails loudly at the ingest boundary instead of landing in the table."""
        model = EXTRA_MODELS[self.event_type]
        self.extra = model.model_validate(self.extra).model_dump(mode="json")
        return self

    @model_validator(mode="after")
    def _congestion_has_no_bbox(self):
        if self.event_type is EventType.CONGESTION and self.bbox is not None:
            raise ValueError("congestion is a frame-level aggregate and cannot carry a bbox")
        return self

    @model_validator(mode="after")
    def _incidents_are_realtime(self):
        """An accident must not sit in the batch buffer waiting for a flush."""
        if self.event_type is EventType.INCIDENT and self.priority is not Priority.REALTIME:
            raise ValueError("incident events must be sent on the realtime lane")
        return self


class EventBatch(BaseModel):
    """Buses upload in batches after a connectivity gap; incidents arrive alone.

    The items are dicts, not Events, on purpose: they are validated INDIVIDUALLY at
    ingest. A batch is a backlog from an unreliable device, so one malformed event
    must be rejected on its own rather than discarding the hundreds of good events
    queued behind it. Each item is still validated against `Event` - nothing malformed
    reaches the table - and the rejection is reported back per event.
    """

    events: list[dict] = Field(min_length=1, max_length=1000)


class EventAck(BaseModel):
    event_id: UUID | None
    status: Literal["accepted", "duplicate", "rejected"]
    #: set when this report was ignored for confirmation counting (same-bus cooldown)
    cooldown_suppressed: bool = False
    #: why the event was rejected, when it was
    error: str | None = None


class IngestResponse(BaseModel):
    received: int
    accepted: int
    duplicates: int
    rejected: int
    results: list[EventAck]
