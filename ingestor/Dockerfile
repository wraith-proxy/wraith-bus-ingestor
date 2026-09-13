FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Only `requests` is needed here (plus stdlib xml.etree) -- the ingestor used
# to also install protobuf and gtfs-realtime-bindings, which ingest.py never
# imported. duckdb/dbt-core are their own optional extras now and not
# requested here, so this only pulls the small base dependency set flask/
# flask-cors/tzdata/requests/pyyaml -- still more than this script strictly
# needs, but built the same way as the other two images for consistency.
RUN uv sync --frozen --no-dev --no-install-project
COPY ingestor/ingest.py .

CMD ["uv", "run", "--no-sync", "python", "-u", "ingest.py"]
