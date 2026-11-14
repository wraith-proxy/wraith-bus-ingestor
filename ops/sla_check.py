"""SLA checks for a contract's slaProperties, plus the data-as-of stamp."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import yaml

_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


@dataclass(frozen=True)
class Breach:
    property: str
    message: str


def _delta(value, unit):
    return timedelta(**{_UNITS[unit]: value})


def check_slas(contract: Path, now: datetime, *, as_of: datetime) -> list[Breach]:
    """Breaches of the contract's `latency` SLA: data older than the allowed age."""
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
    """Stamp the warehouse with when its data is as of and which contract version built it."""
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
    """The data-as-of stamp stored in the warehouse's `_meta` table."""
    return _meta(db, "data_as_of")


def contract_version(db: Path) -> str:
    """The contract version stored in the warehouse's `_meta` table."""
    return _meta(db, "contract_version")


def publish(staged: Path, served: Path, as_of: datetime, version: str) -> None:
    """Stamp the staged warehouse, then swap it over the served file atomically.

    The stamp goes INTO the file before the rename, so a sidecar file can never
    disagree with what is served. If stamping fails, the served file is untouched.
    """
    write_meta(staged, as_of, version)
    os.replace(staged, served)
