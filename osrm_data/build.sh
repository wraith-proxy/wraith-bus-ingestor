#!/bin/sh
# Builds the OSRM routing graph used by the `osrm` compose service to snap
# /api/route lines to real roads. Not run automatically -- this directory is
# gitignored (multi-GB output) and the graph rarely needs rebuilding, so this
# is a manual, one-off step, not part of `docker compose up`.
#
# Whole Great Britain, not just the ingest bounding box in
# contracts/bus-positions.odcs.yaml: that box already spans several county
# extracts with no single clean regional download, and getting a bbox clip
# wrong silently drops roads at the edges. GB is one file, no seams, and
# OSRM's own car-profile filtering (routable highways only) keeps the graph
# a fraction of the source download's size regardless.
#
# Usage: sh osrm_data/build.sh   (from the repo root, or anywhere -- it cd's
# to its own directory first)
set -eu
cd "$(dirname "$0")"

PBF=great-britain-latest.osm.pbf
GRAPH=great-britain-latest.osrm

if [ ! -f "$PBF" ]; then
    echo "Downloading $PBF (~2GB) from Geofabrik..."
    curl -Lo "$PBF" "https://download.geofabrik.de/europe/great-britain-latest.osm.pbf"
fi

docker run --rm -v "$(pwd)":/data ghcr.io/project-osrm/osrm-backend:v26.10.0-debian \
    osrm-extract -p /opt/car.lua "/data/$PBF"

docker run --rm -v "$(pwd)":/data ghcr.io/project-osrm/osrm-backend:v26.10.0-debian \
    osrm-partition "/data/$GRAPH"

docker run --rm -v "$(pwd)":/data ghcr.io/project-osrm/osrm-backend:v26.10.0-debian \
    osrm-customize "/data/$GRAPH"

echo "Done. Restart the osrm service: docker compose up -d osrm"
