FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN pip install --no-cache-dir requests protobuf gtfs-realtime-bindings
COPY ingest.py .
CMD ["python", "-u", "ingest.py"]
