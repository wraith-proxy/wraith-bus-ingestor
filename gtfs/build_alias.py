"""Learn live-label -> GTFS-label aliases from the live feed's own origin/dest refs.

A live (operator, line) that already exists in gtfs_route_patterns needs no
alias. For the rest, a bus's origin_ref/dest_ref pin it to a GTFS pattern
(first_atco/last_atco), which names the GTFS operator+line it really runs.
Never raises on a QuestDB failure: degrades to the previous served table.
"""
import json
import os
import tempfile
import urllib.parse
import urllib.request

MIN_BUSES = 3
MIN_SHARE = 0.6
MAX_ROWS = 200000
# ponytail: 24h only. A wider window risks the QuestDB query-memory trouble
# this repo already suffered (ROADMAP 1.1.1). Bus-level rows (so distinct buses
# can be counted across origin/dest pairs) capped at MAX_ROWS; hitting the cap
# is an error, not a silent truncation.
QUERY = (
    "SELECT DISTINCT operator, line, origin_ref, dest_ref, bus_id "
    "FROM bus_positions WHERE timestamp > dateadd('h', -24, now()) "
    "AND operator IS NOT NULL AND line IS NOT NULL "
    "AND origin_ref IS NOT NULL AND dest_ref IS NOT NULL "
    f"LIMIT {MAX_ROWS}"
)
DDL = """create or replace table gtfs_line_alias(
    live_operator VARCHAR, live_line VARCHAR, gtfs_operator VARCHAR,
    gtfs_line VARCHAR, n_buses BIGINT, share DOUBLE)"""


def _fetch(questdb_http):
    qs = urllib.parse.urlencode({"query": QUERY, "timeout": 30000})  # ms, server side
    with urllib.request.urlopen(f"{questdb_http}/exec?{qs}", timeout=45) as r:
        d = json.load(r)
    if "error" in d:
        raise RuntimeError(d["error"])
    if len(d["dataset"]) >= MAX_ROWS:
        raise RuntimeError(f"result hit the {MAX_ROWS}-row cap; refusing a truncated answer")
    return d["dataset"]


def _learn(con, rows):
    # executemany is ~30s for 23k rows in DuckDB; a JSON file read is instant.
    with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
        json.dump([dict(zip("operator line origin_ref dest_ref bus".split(), r)) for r in rows], f)
        f.flush()
        con.execute("create or replace temp table _live as select operator::varchar as operator, "
                    "line::varchar as line, origin_ref::varchar as origin_ref, dest_ref::varchar as dest_ref, "
                    f"bus::varchar as bus from read_json('{f.name}')")
    # One candidate per (bus, gtfs op/line). Another operator is allowed only
    # when the live operator has no same-operator match on ANY of its rows AND
    # the live operator exists in GTFS at all (a renamed/consolidated NOC).
    # An operator absent from GTFS -- TfL, TFLO, is 7,000+ live buses -- must
    # never be relabelled as some other operator's route just because two
    # termini coincide: the first build did exactly that (604 of 631 aliases
    # were TFLO -> ARVA/GAHL/...) and it is a wrong route, not a coverage gain.
    con.execute("""
    create or replace temp table _cand as
    with lab as (select distinct operator, line from gtfs_route_patterns),
    m as (
      select distinct l.operator lo, l.line ll, l.bus, p.operator go, p.line gl
      from (select _live.* from _live left join lab using (operator, line)
            where lab.line is null) l
      join gtfs_route_patterns p
        on p.first_atco = l.origin_ref and p.last_atco = l.dest_ref),
    same as (select distinct lo from m where go = lo)
    select * from m where go = lo
       or (lo not in (select lo from same) and lo in (select agency_noc from gtfs_agency))""")
    con.execute(DDL)
    # Buses are distinct per label and per candidate. A bus pinned to several
    # GTFS lines (shared termini, 1/1A) supports each of them, so a rival at or
    # above MIN_SHARE means the evidence cannot separate them: drop, never guess.
    con.execute(f"""
    insert into gtfs_line_alias
    with tot as (select lo, ll, count(distinct bus) t from _cand group by 1, 2),
         sup as (select lo, ll, go, gl, count(distinct bus) n from _cand group by all),
         r as (select sup.*, n / t as share,
                      row_number() over (partition by lo, ll order by n desc, go, gl) rk
               from sup join tot using (lo, ll))
    select a.lo, a.ll, a.go, a.gl, a.n, a.share from r a
    left join r b on b.lo = a.lo and b.ll = a.ll and b.rk = 2
    where a.rk = 1 and a.n >= {MIN_BUSES} and a.share >= {MIN_SHARE}
      and coalesce(b.share, 0) < {MIN_SHARE}
    order by a.lo, a.ll""")


def _carry(con, previous_db_path, seen_live=False):
    """Copy still-valid previous aliases: live pair must still lack a pattern, the
    target must still exist. With seen_live, labels the fresh window covered are
    left to it (a fresh drop as ambiguous must not be resurrected)."""
    if not previous_db_path or not os.path.exists(previous_db_path):
        print("build_alias: no previous DB, nothing carried over", flush=True)
        return
    try:
        con.execute(f"attach '{previous_db_path.replace(chr(39), chr(39)*2)}' as _prev (read_only)")
        pool = "select * from _prev.gtfs_line_alias"
        n_prev = con.execute(f"select count(*) from ({pool})").fetchone()[0]
        skip = "and (live_operator, live_line) not in (select operator, line from _live)" if seen_live else ""
        con.execute(f"""insert into gtfs_line_alias
            select live_operator, live_line, gtfs_operator, gtfs_line, n_buses, share from ({pool})
            where (live_operator, live_line) not in (select operator, line from gtfs_route_patterns)
              and (gtfs_operator, gtfs_line) in (select operator, line from gtfs_route_patterns)
              and (live_operator, live_line) not in (select live_operator, live_line from gtfs_line_alias)
              {skip}""")
        print(f"build_alias: carried previous aliases over (previous table had {n_prev}; "
              "rows now invalid, re-learned or superseded were not copied)", flush=True)
    except Exception as e:
        print(f"build_alias: carry-over failed ({e})", flush=True)
    finally:
        try:
            con.execute("detach _prev")
        except Exception:
            pass


def build_alias(con, questdb_http, previous_db_path):
    try:
        rows = _fetch(questdb_http)
        if not rows:
            raise RuntimeError("QuestDB returned no rows")
        _learn(con, rows)
        n = con.execute("select count(*) from gtfs_line_alias").fetchone()[0]
        print(f"build_alias: learned {n} aliases from {len(rows)} live bus rows; "
              "merging previous aliases for labels not seen in the window", flush=True)
        _carry(con, previous_db_path, seen_live=True)
    except Exception as e:
        print(f"build_alias: LEARNING FAILED ({type(e).__name__}: {e}); carrying over", flush=True)
        con.execute(DDL)
        _carry(con, previous_db_path)
    return con.execute("select count(*) from gtfs_line_alias").fetchone()[0]
