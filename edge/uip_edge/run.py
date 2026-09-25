"""Run the simulated bus fleet against the central server.

Two modes, both needed for a demo:

  backfill - replay a historical window as fast as the server can take it, so the map
             has a day of data to draw before anyone is watching. Event timestamps are
             the simulated ones, so confirmation windows behave exactly as they would
             have live.

  live     - drive the fleet in real time (optionally time-compressed), so events
             appear on the map while the judges watch.

Usage:
    uv run python -m uip_edge.run backfill --hours 24
    uv run python -m uip_edge.run live --speed 20
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx

from uip_edge.bus import SimulatedBus
from uip_edge.routes import ROUTES
from uip_edge.uploader import Uploader
from uip_edge.world import summary

DEFAULT_URL = os.environ.get("UIP_SERVER_URL", "http://localhost:8000")
DEFAULT_TOKEN = os.environ.get("UIP_INGEST_TOKEN", "dev-token-change-me")

#: simulated seconds per tick. Small enough that a bus cannot skip past a defect.
TICK_S = 4.0


def build_fleet(rng: random.Random) -> list[SimulatedBus]:
    """One SimulatedBus per vehicle, spread evenly along its route."""
    fleet = []
    for route in ROUTES:
        for i in range(route.fleet_size):
            fleet.append(SimulatedBus(
                # route.name is the full OSM relation name ("Bus 208: Hinjawadi Maan
                # Phase 3 => Bhekrai Nagar"); route.id is the short handle
                bus_id=f"BUS-{route.id}-{i + 1:02d}",
                route=route,
                start_offset_m=route.polyline.length * i / route.fleet_size,
                rng=random.Random(rng.randrange(1 << 30)),
            ))
    return fleet


async def simulate(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    fleet = build_fleet(rng)

    if args.mode == "backfill":
        sim_now = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        sim_end = datetime.now(timezone.utc)
    else:
        sim_now = datetime.now(timezone.utc)
        sim_end = sim_now + timedelta(hours=args.hours) if args.hours else None

    # incidents are rare; schedule them up front so a demo always has some to show
    total_span = (sim_end - sim_now) if sim_end else timedelta(hours=1)
    incident_times = sorted(
        sim_now + total_span * rng.random() for _ in range(args.incidents))

    print(f"{len(fleet)} buses on {len(ROUTES)} routes -> {args.url}")
    print(f"mode={args.mode} window={sim_now:%Y-%m-%d %H:%M} -> "
          f"{sim_end:%Y-%m-%d %H:%M}" if sim_end else "(continuous)")
    print(f"{args.incidents} incidents scheduled\n")

    uploader = Uploader(args.url, args.token, dropout_p=args.dropout, rng=rng)
    emitted = 0
    sim_elapsed = 0.0
    last_report = time.monotonic()

    async with httpx.AsyncClient() as client:
        try:
            await client.get(f"{args.url}/healthz", timeout=10)
        except httpx.HTTPError as exc:
            print(f"cannot reach the server at {args.url}: {exc}")
            return 1

        while sim_end is None or sim_now < sim_end:
            batch: list[dict] = []
            for bus in fleet:
                batch.extend(bus.step(sim_now, TICK_S))

            while incident_times and incident_times[0] <= sim_now:
                incident_times.pop(0)
                bus = rng.choice(fleet)
                subtype = rng.choice(["accident", "accident", "rash_driving"])
                # realtime lane: straight out, no buffering
                await uploader.send_now(client, [bus.incident(sim_now, subtype)])
                emitted += 1

            uploader.queue(batch)
            emitted += len(batch)
            await uploader.flush(client, sim_elapsed,
                                 force=(args.mode == "backfill" and not args.dropout))

            sim_now += timedelta(seconds=TICK_S)
            sim_elapsed += TICK_S
            if args.mode == "live":
                await asyncio.sleep(TICK_S / args.speed)

            if time.monotonic() - last_report > 2.0:
                last_report = time.monotonic()
                print(f"  {sim_now:%H:%M}  emitted={emitted:<7} accepted={uploader.sent:<7} "
                      f"dupes={uploader.duplicates:<5} buffered={len(uploader._buffer)}")

        await uploader.flush(client, sim_elapsed, force=True)

    print(f"\nemitted {emitted} events | accepted {uploader.sent} | "
          f"duplicates {uploader.duplicates} | rejected {uploader.rejected} | "
          f"transport failures {uploader.failures} | "
          f"peak on-bus buffer {uploader.max_buffer}")
    for reason, n in sorted(uploader.invalid.items(), key=lambda kv: -kv[1]):
        print(f"  not sent, failed local validation x{n}: {reason}")
    for reason, n in sorted(uploader.rejections.items(), key=lambda kv: -kv[1]):
        print(f"  rejected by server x{n}: {reason}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # optional, because --show-truth prints and exits without simulating anything -
    # requiring a mode there would mean typing `backfill --show-truth`, which reads as
    # if it were about to send 24 hours of events
    p.add_argument("mode", nargs="?", choices=["backfill", "live"])
    p.add_argument("--hours", type=float, default=24,
                   help="span to simulate (backfill), or run duration (live); 0 = forever")
    p.add_argument("--speed", type=float, default=20.0,
                   help="live mode time compression: 20 = one minute per three seconds")
    p.add_argument("--incidents", type=int, default=6)
    p.add_argument("--dropout", type=float, default=0.0,
                   help="per-flush probability the modem drops out (try 0.05)")
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--token", default=DEFAULT_TOKEN)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--show-truth", action="store_true",
                   help="print the ground truth and exit, to diff against the server")
    args = p.parse_args()

    if args.show_truth:
        print(summary())
        return 0
    if args.mode is None:
        p.error("give a mode: backfill or live (or --show-truth on its own)")
    if args.hours == 0:
        args.hours = None
    return asyncio.run(simulate(args))


if __name__ == "__main__":
    raise SystemExit(main())
