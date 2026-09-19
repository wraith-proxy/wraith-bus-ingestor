FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
# archive.py needs duckdb to read each exported Parquet file back and prove it
# matches QuestDB before the live copy is dropped. It is the project's optional
# `duckdb` extra, pinned <2.0 in pyproject.toml.
RUN uv sync --frozen --no-dev --no-install-project --extra duckdb
COPY archiver/archive.py .

# Runs as root, deliberately and only so it can chown each Parquet file it
# writes to the archive drive's owner (ARCHIVE_OWNER, default 1000:1000) -- the
# host user can then manage the archive without sudo. See archive.py.
CMD ["uv", "run", "--no-sync", "python", "-u", "archive.py"]
