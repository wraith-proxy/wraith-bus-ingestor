#!/usr/bin/env python3
"""One-off diagnostic: EXPLAIN ANALYZE all 80 line_stops batch models, either
serially or with an N-worker concurrent pool (pass N as argv[1], default 4;
1 = plain serial loop, no multiprocessing), to measure real combined memory
cost. Not part of the build -- run manually, wrapped in an external OS-level
memory cap (see the caller)."""
import glob
import multiprocessing
import os
import sys
import time

COMPILED_DIR = 'target/compiled/wraith_bus_ingestor/models/intermediate'
DB_PATH = 'wraith_bus_ingestor_build.duckdb'


def batch_files():
    paths = sorted(glob.glob(os.path.join(COMPILED_DIR, 'int_line_stops_*_b*.sql')))
    return paths


def run_one(path):
    import duckdb
    name = os.path.basename(path).replace('.sql', '')
    sql = open(path).read()
    con = duckdb.connect(DB_PATH, read_only=True, config={'memory_limit': '2GB', 'threads': '1'})
    t0 = time.time()
    try:
        rows = con.execute('EXPLAIN ANALYZE ' + sql).fetchall()
        elapsed = time.time() - t0
        # pull total time + row count out of the profiling text if present
        text = '\n'.join(r[1] for r in rows)
        con.close()
        return (name, elapsed, 'ok', text.count('rows'))
    except Exception as e:
        con.close()
        return (name, time.time() - t0, f'ERROR: {e!r}', 0)


def main():
    n_workers = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    paths = batch_files()
    print(f'{len(paths)} batch files found, running with {n_workers} worker(s)')
    t0 = time.time()
    if n_workers == 1:
        results = [run_one(p) for p in paths]
    else:
        with multiprocessing.Pool(processes=n_workers) as pool:
            results = pool.map(run_one, paths)
    total = time.time() - t0

    ok = [r for r in results if r[2] == 'ok']
    failed = [r for r in results if r[2] != 'ok']

    print(f'\n=== done in {total:.2f}s wall clock ===')
    print(f'ok: {len(ok)}/{len(results)}')
    if ok:
        times = sorted(r[1] for r in ok)
        print(f'per-query time: min={times[0]:.2f}s max={times[-1]:.2f}s median={times[len(times)//2]:.2f}s')
    if failed:
        print(f'FAILED: {len(failed)}')
        for name, elapsed, err, _ in failed:
            print(f'  {name}: {err} (after {elapsed:.2f}s)')


if __name__ == '__main__':
    main()
