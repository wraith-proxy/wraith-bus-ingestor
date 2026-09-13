FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
# Only `requests` is needed here (plus stdlib xml.etree) -- the ingestor used
# to also install protobuf and gtfs-realtime-bindings, which ingest.py never
# imported. uv sync pulls the full project dependency set regardless, same as
# the other two images; the point of pinning here is reproducibility, not a
# minimal image.
RUN uv sync --frozen --no-dev --no-install-project
COPY ingestor/ingest.py .

CMD ["uv", "run", "--no-sync", "python", "-u", "ingest.py"]
