"""SLA checks for a contract's slaProperties, plus the data-as-of stamp.

This module reads extract Parquet and writes only the staged warehouse file it is
given and the served path. It must never touch a live QuestDB, and an SLA breach
must never fail a completed publish.
"""

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import yaml

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


@dataclass(frozen=True)
class Breach:
    """One SLA breach.

    Attributes:
        property: The slaProperties name that was breached.
        message: Human-readable description of the breach.
    """

    property: str
    message: str


def _delta(value, unit):
    return timedelta(**{_UNITS[unit]: value})


def check_slas(contract: Path, now: datetime, *, as_of: datetime) -> list[Breach]:
    """Breaches of the contract's `latency` SLA: data older than the allowed age.

    Args:
        contract: Path to the ODCS contract YAML.
        now: The current time.
        as_of: When the data is as of.

    Returns:
        A list of breaches, empty if the SLA holds.
    """
    doc = yaml.safe_load(Path(contract).read_text())
    breaches = []
    for sla in doc.get("slaProperties", []):
        if sla["property"] != "latency":
            continue
        allowed, age = _delta(sla["value"], sla["unit"]), now - as_of
        if age > allowed:
            hours = age.total_seconds() / 3600
            breaches.append(
                Breach(
                    "latency", f"data is {hours:g} h old, SLA allows {sla['value']}{sla['unit']}"
                )
            )
    return breaches


def write_meta(db: Path, as_of: datetime, version: str) -> None:
    """Stamp the warehouse with when its data is as of and which contract version built it.

    Args:
        db: Path to the DuckDB warehouse file.
        as_of: When the data is as of.
        version: Contract version string.
    """
    con = duckdb.connect(str(db))
    try:
        con.execute(
            "create or replace table _meta (data_as_of timestamptz, contract_version varchar)"
        )
        con.execute("insert into _meta values (?, ?)", [as_of, version])
    finally:
        con.close()


def _meta(db: Path, column: str):
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(f"select {column} from _meta").fetchone()[0]
    finally:
        con.close()


def data_as_of(db: Path) -> datetime:
    """The data-as-of stamp stored in the warehouse's `_meta` table.

    Args:
        db: Path to the DuckDB warehouse file.

    Returns:
        The stored timestamp.
    """
    return _meta(db, "data_as_of")


def contract_version(db: Path) -> str:
    """The contract version stored in the warehouse's `_meta` table.

    Args:
        db: Path to the DuckDB warehouse file.

    Returns:
        The stored contract version.
    """
    return _meta(db, "contract_version")


def publish(staged: Path, served: Path, as_of: datetime, version: str) -> None:
    """Stamp the staged warehouse, then swap it over the served file atomically.

    The stamp goes INTO the file before the rename, so a sidecar file can never
    disagree with what is served. If stamping fails, the served file is untouched.

    Args:
        staged: Path to the staged warehouse file.
        served: Path of the file being served; replaced atomically.
        as_of: When the data is as of.
        version: Contract version string.
    """
    write_meta(staged, as_of, version)
    os.replace(staged, served)


def extract_data_as_of(data_dir: Path) -> datetime:
    """Newest ping in the extract Parquet: what the served marts are as of.

    Args:
        data_dir: Directory holding the extract Parquet files.

    Returns:
        The newest timestamp, timezone-aware (UTC if the data was naive).

    Raises:
        ValueError: If the extract holds no pings.
    """
    con = duckdb.connect()
    try:
        newest = con.execute(
            "select max(timestamp) from read_parquet(?)",
            [str(Path(data_dir) / "bus_positions_*.parquet")],
        ).fetchone()[0]
    finally:
        con.close()
    if newest is None:
        raise ValueError(f"no pings in {data_dir}")
    return newest if newest.tzinfo else newest.replace(tzinfo=UTC)


def main(argv=None) -> int:
    """`publish STAGED SERVED --data-dir DIR --contract FILE`: stamp, swap, report breaches.

    Called by dbt/build.sh in place of a bare `mv`. A failure to stamp leaves the served file
    untouched (the build stops, the previous marts keep serving). An SLA breach is reported
    but does not fail the publish: it is a ticket for the owner, not a reason to go dark.

    Args:
        argv: Command-line arguments; defaults to `sys.argv[1:]`.

    Returns:
        0 always once the publish has completed.
    """
    ap = argparse.ArgumentParser(description=main.__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pub = sub.add_parser("publish")
    pub.add_argument("staged", type=Path)
    pub.add_argument("served", type=Path)
    pub.add_argument("--data-dir", type=Path, required=True)
    pub.add_argument("--contract", type=Path, required=True)
    args = ap.parse_args(argv)

    as_of = extract_data_as_of(args.data_dir)
    version = str(yaml.safe_load(args.contract.read_text())["version"])
    publish(args.staged, args.served, as_of, version)
    print(f"published {args.served} (data as of {as_of:%Y-%m-%d %H:%M} UTC, contract {version})")
    # The swap is done: reporting must never fail a completed publish.
    try:
        breaches = check_slas(args.contract, datetime.now(UTC), as_of=as_of)
    except Exception as exc:  # malformed slaProperties, unknown unit, ...
        print(f"SLA check could not run: {type(exc).__name__}: {exc}")
        return 0
    for breach in breaches:
        print(f"SLA breach: {breach.property}: {breach.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
