#!/usr/bin/env bash
# Start the PostGIS container without the docker compose plugin.
set -euo pipefail

docker run -d --name uip-db \
    -e POSTGRES_USER=uip \
    -e POSTGRES_PASSWORD=uip \
    -e POSTGRES_DB=uip \
    -p 5432:5432 \
    -v uip-pgdata:/var/lib/postgresql/data \
    postgis/postgis:16-3.4

echo "waiting for postgres..."
until docker exec uip-db pg_isready -U uip -d uip >/dev/null 2>&1; do sleep 1; done
echo "uip-db is ready on localhost:5432"
