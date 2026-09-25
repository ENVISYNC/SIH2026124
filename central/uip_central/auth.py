"""Shared-bearer-token auth for bus clients.

Stands in for the full design's per-device authentication at the API gateway. Read
endpoints are deliberately left open so the Leaflet map can be a static page.
"""

from fastapi import Header, HTTPException, status

from uip_central.config import settings


async def require_bus_token(authorization: str = Header(default="")) -> None:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or token != settings.ingest_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bus device token",
            headers={"WWW-Authenticate": "Bearer"},
        )
