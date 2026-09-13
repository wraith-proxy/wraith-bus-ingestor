FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
# archive.py is stdlib-only (urllib, glob, shutil, json) and needs no
# third-party packages -- uv sync still runs so this image is built the same
# way as the other two, not because archive.py depends on anything in the
# lockfile. duckdb/dbt-core are their own optional extras and not requested
# here either way.
RUN uv sync --frozen --no-dev --no-install-project
COPY archiver/archive.py .

# Runs as root, deliberately: the partition directories DETACHed from
# QuestDB are owned by QuestDB's in-container uid (10001), which the host
# user cannot write to, and matching that uid exactly is more fragile
# long-term than running this one narrowly-scoped maintenance container as
# root. See archive.py's module docstring.
CMD ["uv", "run", "--no-sync", "python", "-u", "archive.py"]
