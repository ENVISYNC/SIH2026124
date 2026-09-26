# Urban Intelligence Platform (UIP)

**SIH26124** — AI-Powered Mobile Urban Intelligence Platform (prototype)

Public transport buses already drive every road in a city, every day. This project turns
that fleet into a roving sensor network: an on-bus AI pipeline detects potholes, road
surface damage, waterlogging, traffic congestion, and incidents (accidents, rash
driving) from dashcam footage, and a central server fuses reports from many independent
buses into a **trusted, city-wide map** — no single bus's false positive can put a
phantom pothole on the map, because nothing is confirmed until multiple vehicles agree.

> A pothole is only believed once several *distinct* buses report it near the same
> place within a time window. One bus reporting the same spot repeatedly proves
> nothing — it's one detector, possibly one false positive, seen several times.

<p align="center">
  <img src="findings/multi_model_split.png" width="800" alt="Edge AI: multi-model detection split (road defects + vehicles)">
</p>

---

## Table of contents

- [Why this exists](#why-this-exists)
- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Event schema](#event-schema)
- [Confirmation logic](#confirmation-logic)
- [Getting started](#getting-started)
  - [Prerequisites](#prerequisites)
  - [One-command demo](#one-command-demo)
  - [Two-laptop / real deployment](#two-laptop--real-deployment)
- [Walking through the demo](#walking-through-the-demo)
- [Manually triggering events](#manually-triggering-events)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [Edge AI on real footage](#edge-ai-on-real-footage)
- [Known failure cases](#known-failure-cases)
- [Tech stack](#tech-stack)
- [License](#license)

---

## Why this exists

Municipal bodies today rely on citizen complaints and periodic manual road surveys to
find infrastructure problems — slow, sparse, and easy to miss until a pothole becomes a
crater. Buses already cover the whole road network on a fixed schedule. If each bus can
cheaply detect defects, incidents, and traffic from a dashcam, and a central service can
tell a *real, persistent* defect from noise, a city gets a continuously refreshed map of
road health, flooding, congestion, and incidents at close to zero marginal cost.

This repo is a working, end-to-end prototype of that idea, built for Smart India
Hackathon problem statement **SIH 26124**, simulated on the real bus routes of Pune
(PMPML), and validated against real (anonymised) Indian dashcam footage.

## How it works

1. **Edge (bus).** Each bus runs a small on-device pipeline that watches the road ahead
   and classifies what it sees into typed events — pothole, surface defect, waterlogging,
   pedestrian risk, congestion, or incident. Detection is intentionally imperfect and
   probabilistic (a pass can miss a defect, or throw a false positive), because that's
   what real detectors do.
2. **Upload.** Non-urgent events (potholes, congestion, etc.) are batched and flushed
   when connectivity allows. Incidents skip the buffer and go out immediately on a
   realtime lane.
3. **Central server.** Every event is validated against a shared schema, then run
   through confirmation logic: defects only become "confirmed" once enough *independent*
   evidence exists — either several distinct buses, or (on low-frequency routes) the same
   bus passing repeatedly over a long enough span.
4. **Serve.** Confirmed defects, live congestion, and incidents are served as GeoJSON
   layers and rendered on a Leaflet map, refreshed continuously. Pending (unconfirmed)
   reports are visible separately for operators, but kept off the public/confirmed layer
   so noise never reaches the headline map.
5. **Maintenance.** A background job continuously demotes potholes the fleet has stopped
   seeing (repaired) and expires waterlogging once the rain event is over.

## Architecture

```
┌─────────────────────────┐        HTTPS / JSON         ┌──────────────────────────────┐
│   Edge (bus fleet)       │  ───────────────────────▶   │   Central server              │
│   uip_edge                     │  batch (potholes,      │   uip_central (FastAPI)       │
│                                 │  congestion, ...)      │                               │
│  • per-bus detection sim │  ───────────────────────▶   │  ingest  → validate (schema)  │
│  • realtime incidents    │      realtime (incidents)    │  confirm → multi-bus rules    │
│  • local batch buffer    │                              │  aggregate → congestion grid  │
│  • simulated GPS/route   │                              │  serve   → GeoJSON layers     │
└─────────────────────────┘                              │  maintain → reap/expire       │
                                                           └───────────────┬───────────────┘
                                                                           │
                                                                  PostGIS  │  (Docker)
                                                                           ▼
                                                           ┌──────────────────────────────┐
                                                           │  Leaflet map (static web/)    │
                                                           │  served by the same process   │
                                                           └──────────────────────────────┘
```

The two halves are deliberately kept separable — a real deployment runs them on two
different machines (the bus fleet and the operations server) talking only over the LAN
via the `uip-schema` contract. `shared/uip_schema` is the only code either side depends
on from the other.

## Repository layout

```
.
├── central/                 # Central server — runs on the ops/server machine
│   ├── uip_central/
│   │   ├── main.py          # FastAPI app, lifespan, static map hosting
│   │   ├── config.py        # Every tunable threshold (confirmation, aggregation...)
│   │   ├── auth.py          # Bearer-token auth for bus clients
│   │   ├── db.py            # Postgres/PostGIS connection pool + schema apply
│   │   ├── logic/
│   │   │   ├── confirmation.py   # Multi-bus confirmation rules (potholes/waterlogging)
│   │   │   └── maintenance.py    # Background reaper: demote/expire stale defects
│   │   └── routers/
│   │       ├── ingest.py    # POST /api/v1/events
│   │       ├── layers.py    # GET  /api/v1/layers/* (GeoJSON for the map)
│   │       └── ops.py       # GET  /api/v1/ops/*    (pending reports, stats, reap)
│   ├── web/                 # Static Leaflet dashboard (index.html, app.js, style.css)
│   ├── sql/001_schema.sql   # PostGIS schema (idempotent — applied on startup)
│   ├── scripts/             # db-up.sh, start.sh, reset_db.py, smoke_test.py
│   └── docker-compose.yml   # PostGIS database container
│
├── edge/                    # Simulated bus fleet — runs on the edge/field machine
│   ├── uip_edge/
│   │   ├── bus.py           # One simulated bus: drives, detects, emits events
│   │   ├── world.py         # Ground-truth potholes/waterlogging/congestion zones
│   │   ├── routes.py        # Route loading + per-route load model
│   │   ├── geo.py           # Polyline / great-circle geometry helpers
│   │   ├── uploader.py      # Batch buffering, realtime lane, retry/dropout handling
│   │   ├── inject.py        # Scripted event injection (used by send.sh recipes)
│   │   ├── run.py           # CLI: backfill / live simulation modes
│   │   └── data/pune_routes.json  # Real PMPML route geometry (OSM-derived)
│   └── scripts/             # start.sh, send.sh (demo recipes), verify_against_truth.py
│
├── shared/
│   └── uip_schema/          # Event schema — the ONE contract both halves depend on
│
├── findings/                 # Stills illustrating the edge model's known failure cases
├── videos/                   # Annotated dashcam footage + ANPR burst clips (demo assets)
├── scripts/demo.sh           # One-command: DB + server + backfill + live fleet, one box
├── DEMO_GUIDE.md              # Step-by-step script for presenting the demo
├── pyproject.toml / uv.lock  # uv workspace root (shared + central + edge)
└── LICENSE                   # MIT
```

## Event schema

Defined once in `shared/uip_schema/events.py` and imported by both halves, so the bus
constructs events against the exact contract the server validates against — no
duplicated definitions to drift apart.

| Event type        | Lane      | Confirmed by                         | Notes |
|--------------------|-----------|----------------------------------------|-------|
| `pothole`           | batch     | multi-bus agreement (or repeat-pass)   | persists for weeks; long confirmation window |
| `surface_defect`    | batch     | —                                       | structural pavement damage, distinct from a pothole |
| `waterlogging`       | batch     | multi-bus agreement                    | rain event; short window, auto-expires |
| `congestion`         | batch     | aggregated (not confirmed per-report)  | grid-bucketed, fixed cadence |
| `pedestrian_risk`    | batch     | —                                       | vulnerable pedestrian situations |
| `incident`           | **realtime** | none (reported immediately)          | accidents / rash driving, includes ANPR |

Every event carries the reporting bus's GPS (with realistic consumer-grade error), a
timestamp, a schema version, and a typed `extra` payload specific to its event type
(severity, lane position, estimated range to the defect, bounding box, etc). The
`SCHEMA_VERSION` field lets old and new firmware coexist during a staged rollout: new
versions only ever *add* event types, never change existing ones.

## Confirmation logic

The heart of the platform, in `central/uip_central/logic/confirmation.py`:

- **Potholes**: confirmed once **3 distinct buses** report within **20 m** of each other
  inside a **30-day** window. A single bus repeating the same spot inside a 60-minute
  cooldown doesn't count twice. On low-frequency routes that can never gather 3 distinct
  buses, a **single bus passing 4+ times over at least 6 hours** confirms instead — each
  pass is weaker evidence than an independent bus, so the bar is higher.
- **Waterlogging**: confirmed with just **2 buses** inside a much shorter **3-hour**
  window (it's a transient rain event, not a persistent defect), and auto-expires 6
  hours after its last report.
- **Congestion**: aggregated on a 100 m grid in 5-minute buckets — no per-report
  confirmation needed, since it's a statistical signal rather than a discrete claim.
- **Incidents**: never confirmation-gated — they're rare, urgent, and go straight to the
  map above a minimum confidence threshold.
- **Maintenance**: a background loop (every 5 minutes by default) demotes potholes that
  several buses have since passed without seeing, and expires stale waterlogging.

Every threshold above lives in `central/uip_central/config.py` and can be tuned without
touching SQL or logic code.

## Getting started

### Prerequisites

Works on any Linux / macOS / WSL machine:

- **[Docker](https://www.docker.com/)** — runs the PostGIS database (pulls a ~400 MB
  image on first run)
- **[uv](https://docs.astral.sh/uv/)** — installs the right Python version and all
  dependencies automatically
- **curl**
- Internet access on the first run only (Docker image + Python packages)

### One-command demo

Runs the whole stack — database, server, a 24-hour backfill, and a live simulated
fleet — on a single machine:

```bash
bash scripts/demo.sh              # full demo: DB + server + 24h backfill + live fleet
bash scripts/demo.sh --hours 6    # faster warm-up (~1 min) for a rehearsal
bash scripts/demo.sh --reset      # wipe the database first, for a clean run
bash scripts/demo.sh --port 9000  # use a custom port
```

Wait roughly 3–4 minutes for the backfill to complete, then open **http://localhost:8000/**.
`Ctrl-C` stops both the server and the fleet; the database keeps its data (run
`docker stop uip-db` to stop that too).

> Start the demo shortly before presenting — confirmed waterlogging expires 6 hours
> after its last report, and the simulated rain falls in the evening.

### Two-laptop / real deployment

For a genuine demonstration of the edge/central split, run each half on its own
machine, talking over the LAN:

```bash
# On the server laptop:
bash central/scripts/start.sh

# On the edge (bus fleet) laptop:
bash edge/scripts/start.sh --url http://<server-ip>:8000
```

`scripts/bundle.sh <part>` (central or edge) produces a self-contained, standalone copy
of either half for deployment onto a machine without the full workspace.

## Walking through the demo

A suggested run-through, once the server is up and the map is loaded at
**http://localhost:8000/** (about 5 minutes):

**1. The city, already mapped.** Toggle the layers in the sidebar: congestion heatmap,
confirmed potholes, confirmed waterlogging, incident markers. Set auto-refresh to 10 s.
Click an incident marker for its popup (ANPR plates, cameras, timestamps). In a second
terminal, run the accuracy check against ground truth the buses never saw:

```bash
uv run python edge/scripts/verify_against_truth.py
```

**2. One bus is not enough.** Each command below prints a map link — refresh the map to
see the effect (see [Manually triggering events](#manually-triggering-events) for the
full list):

```bash
bash edge/scripts/send.sh pending     # 1 bus  -> stays off the map (pending)
bash edge/scripts/send.sh pothole     # 3 buses -> CONFIRMED, appears on the map
bash edge/scripts/send.sh solo        # 1 bus x 4 passes over 8h -> confirmed (repeated-pass rule)
bash edge/scripts/send.sh solo-fail   # 1 bus x 4 passes in 1h  -> still pending
bash edge/scripts/send.sh water       # waterlogging, 2 buses in 3h -> confirmed
```

**3. No confirmation needed for these** — they appear on the map immediately:

```bash
bash edge/scripts/send.sh jam         # congestion, red on the heatmap
bash edge/scripts/send.sh accident    # incident with ANPR plates
bash edge/scripts/send.sh rash        # rash-driving incident
```

**4. The edge AI, on real Indian dashcam footage** (any video player, no GPU needed) —
see [Edge AI on real footage](#edge-ai-on-real-footage) below.

**Notes**

- Simulated city is Pune on the real PMPML bus routes: 26 buses, 16 routes.
- Results vary slightly per run (random seed): potholes 10/10 and waterlogging 5/5 are
  stable; a couple of false positives is normal, and worth quoting honestly.

## Manually triggering events

With the server running, `edge/scripts/send.sh` fires scripted scenarios so you can
watch the confirmation logic behave live (each prints a map link — refresh to see it):

```bash
bash edge/scripts/send.sh pending     # 1 bus  -> stays off the map (pending)
bash edge/scripts/send.sh pothole     # 3 buses -> CONFIRMED pothole
bash edge/scripts/send.sh solo        # 1 bus x 4 passes over 8h -> confirmed (fallback rule)
bash edge/scripts/send.sh solo-fail   # 1 bus x 4 passes in 1h  -> still pending (span too short)
bash edge/scripts/send.sh water       # waterlogging, 2 buses in 3h -> confirmed
bash edge/scripts/send.sh jam         # congestion -> shows immediately on heatmap
bash edge/scripts/send.sh accident    # incident with ANPR plates -> shows immediately
bash edge/scripts/send.sh rash        # rash-driving incident
bash edge/scripts/send.sh status      # current fleet/event status
bash edge/scripts/send.sh truth       # print ground truth (for comparison)
bash edge/scripts/send.sh             # list every available recipe
```

To check overall accuracy against a ground truth the simulated buses never had access
to:

```bash
uv run python edge/scripts/verify_against_truth.py
```

## API reference

All served by the central server at `http://<host>:8000`.

| Method | Path                                  | Description |
|--------|----------------------------------------|--------------|
| `GET`  | `/healthz`                              | Health check + PostGIS version |
| `POST` | `/api/v1/events`                        | Ingest a batch of edge events (bearer token required) |
| `GET`  | `/api/v1/layers/potholes`               | Confirmed potholes as GeoJSON |
| `GET`  | `/api/v1/layers/waterlogging`           | Confirmed waterlogging as GeoJSON |
| `GET`  | `/api/v1/layers/congestion`             | Aggregated congestion grid as GeoJSON |
| `GET`  | `/api/v1/layers/incidents`              | Recent incidents (with ANPR data) as GeoJSON |
| `GET`  | `/api/v1/ops/pending/{event_type}`      | Reports not yet confirmed (operator view) |
| `GET`  | `/api/v1/ops/stats`                     | Ingest/confirmation stats |
| `POST` | `/api/v1/ops/reap`                      | Manually trigger the maintenance pass (bearer token required) |
| `GET`  | `/`                                       | Static Leaflet map dashboard |

Read endpoints are intentionally unauthenticated in this prototype (the map is a static
page served on the LAN); ingest and admin endpoints require the bearer token configured
via `UIP_INGEST_TOKEN`.

## Configuration

All tunables live in `central/uip_central/config.py` and can be overridden via
environment variables (prefix `UIP_`) or a `.env` file — for example:

```bash
UIP_DATABASE_URL=postgresql://uip:uip@localhost:5432/uip
UIP_INGEST_TOKEN=dev-token-change-me
UIP_POTHOLE_MIN_CONFIRMATIONS=3
UIP_WATERLOGGING_WINDOW_HOURS=3
```

## Edge AI on real footage

The `videos/` and `findings/` directories contain results from the on-bus detection
pipeline run against real (Indian) dashcam footage — no GPU required to view them:

- `videos/annotated.mp4` — potholes, surface damage, vehicle tracking, and pedestrian
  risk detection, all overlaid on one clip
- `videos/anpr_burst_1.5s.mp4`, `videos/anpr_burst_31.8s.mp4` — automatic number-plate
  recognition, triggered only around a flagged incident (not run continuously)
- `findings/*.png` — annotated stills of the model's known failure modes (see below)

> ⚠️ These clips contain **unblurred vehicle number plates**. Do not screen or publish
> them anywhere public or recorded.

## Known failure cases

Documented honestly in `findings/`, because a hackathon judge (and a real deployment)
should know exactly where the model still struggles:

| File | Failure mode |
|------|----------------|
| `anpr_burst_gated.png` | ANPR triggers only in short bursts around incidents, by design — shown here for review |
| `auto_rickshaw_miss.png` | Vehicle classifier occasionally misses auto-rickshaws |
| `ego_hood_false_positive.png` | The bus's own hood/bonnet occasionally triggers a false detection |
| `multi_model_split.png` | How detection is split across the two on-bus models (road defects vs. vehicles) |
| `pedestrian_road_mask.png` | Pedestrian-risk masking on the drivable road area |
| `pothole_confidence_inversion.png` | A case where confidence ranking inverted between two candidate detections |

Results vary slightly run-to-run (random seed): in the simulated 24-hour Pune dataset,
pothole and waterlogging recall are stable (10/10 and 5/5 respectively), and a couple of
false positives per run is normal and worth reporting honestly rather than hiding.

## Tech stack

- **Language**: Python 3.11+, managed as a [uv](https://docs.astral.sh/uv/) workspace
  (`shared` / `central` / `edge`)
- **Central server**: [FastAPI](https://fastapi.tiangolo.com/), Uvicorn, `psycopg[binary,pool]`
  (async), Pydantic Settings
- **Database**: PostgreSQL + [PostGIS](https://postgis.net/) (via Docker)
- **Schema/contract**: Pydantic models shared between both halves (`uip-schema`)
- **Edge simulation**: `httpx` for transport, custom geo/route/world simulation modules
- **Frontend**: static Leaflet.js dashboard (`central/web`), served directly by FastAPI
- **Simulated environment**: real PMPML (Pune) bus routes derived from OpenStreetMap

## License

Released under the [MIT License](LICENSE) — Copyright (c) 2026 ENVISYNC.
