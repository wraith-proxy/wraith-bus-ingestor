"""Choose between timetable patterns that share a bus's origin and destination.

ADR-0008 (h)(i). Endpoints leave several candidates for about 18% of buses (a
diversion, a school-day variant), and where the bus is right now does not tell
them apart: the variants share most stops. Evidence accumulated over the whole
journey does, modestly: the running mean distance from each ping to each
candidate's stop-to-stop polyline. Measured: the prior alone is right 72.4% of
the time, this hybrid 77.8%.

The rule (all numbers from the ADR, tuned on one snapshot, so treat them as
provisional):
  * each ping adds min(distance to the polyline, CAP_M) to that candidate's sum;
  * if the best candidate's mean leads the runner-up's by LEAD_M or more, and is
    itself under INFORMATIVE_M, take it;
  * otherwise take the prior -- the pattern most trips run, then the lowest
    pattern_id (the order gtfs.candidates already returns) -- and say so, so
    callers can show the result as unresolved.

State is a per-vehicle running sum, not a list of pings, and lives in memory: it
is lost on restart and re-seeded from QuestDB by app.py. It resets when the
journey changes or the GTFS file is swapped (pattern ids only mean something
within one build).
"""

import math
import threading
import time

CAP_M = 250.0            # one wild ping cannot dominate a mean
LEAD_M = 3.0             # required lead of the best candidate over the runner-up
INFORMATIVE_M = 60.0     # straight stop-to-stop lines are this wrong by themselves
FORGET_S = 3 * 3600      # drop a vehicle's trail this long after we last saw it

_lock = threading.Lock()
_trails = {}             # vehicle -> {journey, gen, acc, last_seen, live_from, seeded}
_pruned = {'at': 0.0}

M_PER_DEG_LAT = 110540.0
M_PER_DEG_LON = 111320.0


def distance_m(lat, lon, stops):
    """Metres from a point to the polyline through these stops, in a local flat
    projection (fine at bus scale). inf for fewer than one usable stop."""
    kx = M_PER_DEG_LON * math.cos(math.radians(lat))
    pts = [((s['lon'] - lon) * kx, (s['lat'] - lat) * M_PER_DEG_LAT)
           for s in stops if s.get('lat') is not None and s.get('lon') is not None]
    if not pts:
        return math.inf
    best = math.inf
    for (ax, ay), (bx, by) in list(zip(pts, pts[1:])) or [(pts[0], pts[0])]:
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = 0.0 if seg2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / seg2))
        px, py = ax + t * dx, ay + t * dy
        d = px * px + py * py
        if d < best:
            best = d
    return math.sqrt(best)


def _label(cand):
    return (cand['direction'], cand['pattern_id'])


def observe(vehicle, journey, gen, cands, lat, lon, ts, seeding=False):
    """Add one ping to a vehicle's trail. A live ping (seeding=False) ahead of a
    seeded one marks where live data starts, so seeding never double-counts it."""
    if lat is None or lon is None or len(cands) < 2:
        return
    if not seeding:
        # A repeat of the ping we already scored (most polls, for most buses):
        # decide that before paying for the distances.
        with _lock:
            t = _trails.get(vehicle)
            if (t is not None and t['journey'] == journey and t['gen'] == gen
                    and ts is not None and ts <= t.get('last_ts', '')):
                return
    scored = {_label(c): min(distance_m(lat, lon, c['stops']), CAP_M) for c in cands}
    with _lock:
        t = _trails.get(vehicle)
        if t is None or t['journey'] != journey or t['gen'] != gen:
            t = {'journey': journey, 'gen': gen, 'acc': {}, 'last_seen': 0.0,
                 'live_from': None, 'seeded': False}
            _trails[vehicle] = t
        if seeding:
            if t['live_from'] is not None and ts is not None and ts >= t['live_from']:
                return
        else:
            if t['live_from'] is None:
                t['live_from'] = ts
            elif ts is not None and ts <= t.get('last_ts', ''):
                return                     # same ping seen on an earlier poll
            t['last_ts'] = ts or ''
            t['last_seen'] = time.time()
        for label, d in scored.items():
            acc = t['acc'].setdefault(label, [0.0, 0])
            acc[0] += d
            acc[1] += 1


def needs_seed(vehicle, journey, gen):
    """True once per (vehicle, journey): the caller should backfill its trail."""
    with _lock:
        t = _trails.get(vehicle)
        if t is None or t['journey'] != journey or t['gen'] != gen or t['seeded']:
            return False
        t['seeded'] = True
        return True


def choose(vehicle, journey, gen, cands):
    """(candidate, resolved). resolved is False when the prior was used."""
    if len(cands) == 1:
        return cands[0], True
    prior = cands[0]
    with _lock:
        t = _trails.get(vehicle)
        if t is None or t['journey'] != journey or t['gen'] != gen:
            return prior, False
        means = []
        for c in cands:
            s, n = t['acc'].get(_label(c), (0.0, 0))
            if n:
                means.append((s / n, c))
    if len(means) < 2:
        return prior, False
    means.sort(key=lambda m: m[0])           # stable: ties keep the prior's order
    (best, cand), (second, _other) = means[0], means[1]
    if best <= INFORMATIVE_M and second - best >= LEAD_M:
        return cand, True
    return prior, False


def prune(now=None):
    """Forget vehicles not seen for FORGET_S; at most once a minute."""
    now = now or time.time()
    if now - _pruned['at'] < 60:
        return
    _pruned['at'] = now
    with _lock:
        for v in [v for v, t in _trails.items()
                  if t['last_seen'] and now - t['last_seen'] > FORGET_S]:
            del _trails[v]
