"""On-bus store-and-forward uploader.

Reproduces the transmit policy from CLAUDE.md, because it is what makes the server's
`received_at` differ from `timestamp` and what makes ingest idempotency matter:

  * realtime lane - incidents go out the moment they happen
  * batch lane    - everything else accumulates locally and flushes periodically
  * connectivity gaps - the modem drops out, the buffer grows, and a large backlog
    arrives late. The server stamps received_at on arrival, hours after the event.
"""

from __future__ import annotations

import asyncio
import random

import httpx
from pydantic import ValidationError

from uip_schema.events import Event


class Uploader:
    def __init__(self, base_url: str, token: str, *, batch_size: int = 200,
                 dropout_p: float = 0.0, rng: random.Random | None = None,
                 validate: bool = True):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}
        self.batch_size = batch_size
        self.dropout_p = dropout_p
        self.validate = validate
        self.rng = rng or random.Random()

        self._buffer: list[dict] = []
        self._offline_until = 0.0
        self.sent = 0
        self.duplicates = 0
        self.rejected = 0
        self.failures = 0
        self.max_buffer = 0
        #: reasons the server rejected events, so a generator bug is visible
        self.rejections: dict[str, int] = {}
        #: events this bus refused to send because they failed the shared schema
        self.invalid: dict[str, int] = {}

    def _online(self, sim_elapsed_s: float) -> bool:
        """Connectivity in SIMULATED seconds, so a dropout lasts the same slice of the
        simulated day whether the run is real-time or a fast backfill."""
        if sim_elapsed_s < self._offline_until:
            return False
        if self.dropout_p and self.rng.random() < self.dropout_p:
            # a tunnel, a dead zone, or a congested cell: 2-15 min without a modem
            self._offline_until = sim_elapsed_s + self.rng.uniform(120, 900)
            return False
        return True

    def queue(self, events: list[dict]) -> None:
        self._buffer.extend(self._validated(events))
        self.max_buffer = max(self.max_buffer, len(self._buffer))

    def _validated(self, events: list[dict]) -> list[dict]:
        """Check events against the shared schema BEFORE they leave the bus.

        The server validates too and will reject anything malformed, but catching it
        here is what makes `uip_schema` a real contract rather than documentation: a
        bug in event construction surfaces on the device that produced it, named and
        counted, instead of as an opaque rejection from the far end of a flaky link.
        """
        if not self.validate:
            return events
        ok = []
        for ev in events:
            try:
                Event.model_validate(ev)
                ok.append(ev)
            except ValidationError as exc:
                err = exc.errors()[0]
                reason = f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                self.invalid[reason] = self.invalid.get(reason, 0) + 1
        return ok

    async def send_now(self, client: httpx.AsyncClient, events: list[dict]) -> None:
        """Realtime lane: an accident cannot wait for the next flush."""
        events = self._validated(events)
        if events:
            await self._post(client, events)

    async def flush(self, client: httpx.AsyncClient, sim_elapsed_s: float,
                    *, force: bool = False) -> None:
        if not self._buffer:
            return
        if not force and not self._online(sim_elapsed_s):
            return
        pending, self._buffer = self._buffer, []
        for i in range(0, len(pending), self.batch_size):
            await self._post(client, pending[i:i + self.batch_size])

    async def _post(self, client: httpx.AsyncClient, events: list[dict]) -> None:
        try:
            r = await client.post(f"{self.base_url}/api/v1/events",
                                  json={"events": events}, headers=self.headers,
                                  timeout=30)
            if r.status_code >= 400:
                self.failures += len(events)
                return
            body = r.json()
            self.sent += body["accepted"]
            self.duplicates += body["duplicates"]
            self.rejected += body.get("rejected", 0)
            for ack in body["results"]:
                if ack["status"] == "rejected":
                    reason = ack.get("error") or "unknown"
                    self.rejections[reason] = self.rejections.get(reason, 0) + 1
        except (httpx.HTTPError, asyncio.TimeoutError):
            # a real bus would keep these and retry; the event_id makes that safe
            self._buffer.extend(events)
            self.failures += len(events)
