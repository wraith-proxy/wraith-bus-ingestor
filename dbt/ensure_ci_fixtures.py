"""Bootstrap empty placeholder source files for a docs-only build with no
live data on disk (pages.yml runs `dbt docs generate` without ever calling
extract.py). No-op the moment real data exists -- a real box's data/
directory is never touched.

`ping_columns()` and stg_naptan__stops both read via {{ source(...) }},
which dbt-duckdb expands straight to read_parquet/read_csv against these
paths. `dbt docs generate` runs those for real (execute=True), so with zero
matching files DuckDB raises an IO Error before a single model can even be
introspected for the catalog. A zero-row file with the right columns lets
that introspection succeed instead -- ping_columns() then reports no
optional columns present, which stg_bods__vehicle_pings already handles
(see its own header comment).
"""

import csv
import pathlib

import duckdb

DATA = pathlib.Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

if not list(DATA.glob("bus_positions_*.parquet")):
    placeholder = DATA / "bus_positions_ci_placeholder.parquet"
    duckdb.sql(
        f"""
        copy (
            select
                cast(null as timestamptz) as timestamp,
                cast(null as varchar)     as bus_id,
                cast(null as varchar)     as line,
                cast(null as double)      as lat,
                cast(null as double)      as lon,
                cast(null as double)      as bearing
            where false
        ) to '{placeholder}' (format parquet)
        """
    )

naptan = DATA / "naptan_stops.csv"
if not naptan.exists():
    with naptan.open("w", newline="") as f:
        csv.writer(f).writerow(
            [
                "ATCOCode",
                "CommonName",
                "StopType",
                "TimingStatus",
                "Status",
                "Latitude",
                "Longitude",
                "LocalityName",
                "Town",
                "ParentLocalityName",
            ]
        )
