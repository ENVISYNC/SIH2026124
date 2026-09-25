"""Central urban intelligence platform - prototype server.

Receives distilled events from the (simulated) bus fleet, confirms road defects by
multi-bus agreement, aggregates congestion, and serves GeoJSON layers to the GIS map.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from uip_central import db
from uip_central.config import settings
from uip_central.logic import maintenance
from uip_central.routers import ingest, layers, ops

log = logging.getLogger("uip")


async def _maintenance_loop() -> None:
    """Periodically demote defects that the fleet has stopped seeing."""
    while True:
        await asyncio.sleep(settings.maintenance_interval_seconds)
        try:
            async with db.get_pool().connection() as conn:
                async with conn.transaction():
                    result = await maintenance.run_all(conn)
            if any(result.values()):
                log.info("maintenance: %s", result)
        except Exception:
            log.exception("maintenance pass failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.pool = await db.open_pool()
    await db.apply_schema()  # idempotent, so there is no separate migration step
    task = asyncio.create_task(_maintenance_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await db.close_pool()


app = FastAPI(
    title="Urban Intelligence Platform - Central Server",
    description=(
        "SIH 26124 prototype. Ingests distilled events from a public transport fleet, "
        "confirms road defects across multiple buses, and serves GIS layers."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# the map is served as a static page from anywhere on the LAN in this prototype
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(layers.router)
app.include_router(ops.router)


@app.get("/healthz", tags=["meta"])
async def healthz() -> dict:
    async with app.state.pool.connection() as conn:
        row = await (await conn.execute("SELECT postgis_version() AS v")).fetchone()
    return {"status": "ok", "postgis": row["v"]}


# The GIS map is served by this same process: one URL for the whole demo, so no CORS
# or file:// origin problems on the demo laptop. Mounted at "/" and therefore LAST -
# a StaticFiles mount swallows every path below it, including /api and /docs.
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
