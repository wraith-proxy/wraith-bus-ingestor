FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
# tzdata: zoneinfo is stdlib but python:3.12-slim ships no zone database, and
# hour_band/day_type must resolve Europe/London to agree with the dbt marts.
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ .
RUN mkdir -p /data

CMD ["uv", "run", "--no-sync", "python", "app.py"]
