# Demo guide

Buses stream road-defect / traffic / incident events to a central server. The server
only trusts a pothole once several independent buses agree, and the map shows the result.

## Needs (any Linux / macOS / WSL machine)

- **Docker** (runs the PostGIS database; pulls a ~400 MB image the first time)
- **uv** — https://docs.astral.sh/uv/ (installs Python and dependencies by itself)
- **curl**
- Internet on the first run only (image + Python packages)

## Start

```bash
bash scripts/demo.sh              # database + server + 24 h of simulated data + live fleet
bash scripts/demo.sh --hours 6    # quicker warm-up (~1 min) for a rehearsal
bash scripts/demo.sh --reset      # wipe the database first
```

Wait ~3-4 minutes for the backfill, then open **http://localhost:8000/**. To show it on
another device, use the address printed in the banner. Ctrl-C stops it; the database keeps
its data (`docker stop uip-db` to stop that too).

Start it shortly before presenting: confirmed waterlogging expires 6 h after its last
report, and the simulated rain falls in the evening.

## Show it (about 5 minutes)

**1. The city, already mapped.** Toggle the layers in the sidebar: congestion heatmap,
confirmed potholes, confirmed waterlogging, incident markers. Set auto-refresh to 10 s.
Click an incident marker for its popup (ANPR plates, cameras, timestamps).

Then, in a second terminal, the accuracy check against the ground truth the buses never saw:

```bash
uv run python edge/scripts/verify_against_truth.py
```

**2. One bus is not enough.** Each command prints a map link; refresh the map.

```bash
bash edge/scripts/send.sh pending     # 1 bus  -> stays off the map (tick "Pending potholes" to see it)
bash edge/scripts/send.sh pothole     # 3 buses -> CONFIRMED, appears on the map
bash edge/scripts/send.sh solo        # 1 bus x 4 passes over 8 h -> confirmed (repeated-pass rule)
bash edge/scripts/send.sh solo-fail   # 1 bus x 4 passes in 1 h  -> still pending
bash edge/scripts/send.sh water       # waterlogging, 2 buses in 3 h -> confirmed
```

**3. No confirmation needed for these.**

```bash
bash edge/scripts/send.sh jam         # congestion, red on the heatmap
bash edge/scripts/send.sh accident    # incident with ANPR plates, appears immediately
bash edge/scripts/send.sh rash        # rash-driving incident
```

`bash edge/scripts/send.sh` lists every recipe; `status` and `truth` are also there.

**4. The edge AI, on real Indian dashcam footage** (open with any video player, no GPU needed):

- `videos/annotated.mp4` — potholes, road surface damage, vehicle tracking, pedestrian risk, all on one video
- `videos/anpr_burst_1.5s.mp4`, `videos/anpr_burst_31.8s.mp4` — plate reading, triggered only
  when an incident is flagged (not run continuously)
- `findings/*.png` — stills of the model's known failure cases

The videos show **unblurred number plates**. Do not screen them anywhere that is recorded or public.

## Notes

- Simulated city is Pune on the real PMPML bus routes: 26 buses, 16 routes.
- Results vary slightly per run (random seed): potholes 10/10 and waterlogging 5/5 are stable;
  a couple of false positives is normal, and worth quoting honestly.
- Custom port: `bash scripts/demo.sh --port 9000`.
