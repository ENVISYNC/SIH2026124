"""Truncate all event and confirmation tables. For demo runs, not for production."""

import psycopg

from uip_central.config import settings

with psycopg.connect(settings.database_url) as conn:
    conn.execute("TRUNCATE raw_events, confirmed_potholes, confirmed_waterlogging "
                 "RESTART IDENTITY")
    conn.commit()
print("raw_events, confirmed_potholes, confirmed_waterlogging truncated")
