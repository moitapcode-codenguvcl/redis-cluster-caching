"""
cache_problems.py  –  Classic caching problems and their fixes (Redis Cluster)
==============================================================================
Demonstrates three well-known cache problems.  Each one is run twice – first
WITHOUT protection ("problem" run), then WITH the fix ("fixed" run):

  1. Cache stampede    many threads miss the same hot key at once
                       fix: distributed lock (SET NX EX) -> one thread rebuilds
  2. Cache avalanche   many keys expire at the same moment
                       fix: random jitter added to every TTL
  3. Cache penetration requests for ids that do not exist always reach the DB
                       fix: cache a short-lived null marker for missing ids

Usage (inside the app container):
    python cache_problems.py                 # run all three demos
    python cache_problems.py stampede        # or: avalanche | penetration

Exit code is 0 only if every fix produced a measurable improvement.
"""

import json
import math
import random
import sys
import threading
import time
import uuid
from collections import Counter

from redis.cluster import ClusterNode, RedisCluster

# ── Configuration ─────────────────────────────────────────────────────────────
STARTUP_NODE = ClusterNode("redis-1", 6379)
COL          = 60           # separator width

# Stampede
HOT_KEY          = "cp:stampede:hot-product"
HOT_TTL          = 30       # seconds
STAMPEDE_THREADS = 50       # concurrent requests for the same key
STAMPEDE_DB_LAT  = 0.300    # simulated slow query (seconds)
LOCK_TTL         = 5        # lock auto-expires so a crashed rebuilder cannot block forever
LOCK_WAIT        = 5.0      # max seconds a waiting thread polls the cache
LOCK_POLL        = 0.020    # polling interval while waiting

# Avalanche
AVALANCHE_PREFIX = "cp:avalanche:item:"
AVALANCHE_KEYS   = 200
BASE_TTL         = 60       # seconds
TTL_JITTER       = 30       # random 0..30 s added in the fixed run
BUCKET_SECONDS   = 5        # histogram bucket width

# Penetration
MISSING_IDS      = list(range(900001, 900011))   # ids that are NOT in the database
REQUESTS_PER_ID  = 10                            # 10 ids x 10 = 100 requests
PENETRATION_LAT  = 0.050
NULL_MARKER      = "__NULL__"
NULL_TTL         = 30       # short-lived: a row created later becomes visible soon
ITEM_TTL         = 60


def connect(retries: int = 10, delay: float = 2.0) -> RedisCluster:
    """Connect to the Redis Cluster, retrying until it is ready."""
    for attempt in range(1, retries + 1):
        try:
            rc = RedisCluster(startup_nodes=[STARTUP_NODE], decode_responses=True)
            rc.ping()
            print(f"[OK] Connected to Redis Cluster (attempt {attempt})")
            return rc
        except Exception as exc:
            print(f"[WAIT] Redis not ready ({exc}); retry {attempt}/{retries} in {delay}s")
            time.sleep(delay)
    print("[ERROR] Could not connect to Redis Cluster. Aborting.")
    sys.exit(1)


# ── Simulated slow database ───────────────────────────────────────────────────
class FakeDB:
    """A slow 'database' that counts how many times it has been queried."""

    def __init__(self, latency: float, rows: dict | None = None):
        self.latency = latency
        self.rows    = rows or {}
        self.calls   = 0
        self._lock   = threading.Lock()

    def query(self, row_id):
        """Return the row for row_id, or None if it does not exist."""
        with self._lock:
            self.calls += 1
        time.sleep(self.latency)
        return self.rows.get(row_id)


# ── Pretty printers ───────────────────────────────────────────────────────────
def section(title: str):
    print()
    print("=" * COL)
    print(f"  {title}")
    print("=" * COL)


def print_comparison(label: str, before, after, unit: str = ""):
    """Print a before/after line plus the improvement factor."""
    factor = f"{before / after:.1f}x fewer" if after else "all avoided"
    print(f"  {label:<24} problem={before}{unit:<3} fixed={after}{unit:<3} ({factor})")


# ══════════════════════════════════════════════════════════════════════════════
# 1. Cache stampede
# ══════════════════════════════════════════════════════════════════════════════
def fetch_unprotected(rc: RedisCluster, db: FakeDB, key: str) -> str:
    """Plain cache-aside: every thread that misses goes to the database."""
    cached = rc.get(key)
    if cached is not None:
        return cached
    value = json.dumps(db.query("hot"))
    rc.set(key, value, ex=HOT_TTL)
    return value


# Release the lock only if we still own it (compare-and-delete is atomic in Lua).
RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def fetch_with_lock(rc: RedisCluster, db: FakeDB, key: str) -> str:
    """
    Cache-aside protected by a distributed lock:
      1. HIT  -> return.
      2. MISS -> try SET lock:<key> <token> NX EX <ttl>
           winner : re-check cache, query DB, fill cache, release lock
           loser  : poll the cache until the winner has filled it
      3. If the winner is too slow/dead (LOCK_WAIT elapsed) fall back to the DB.
    """
    cached = rc.get(key)
    if cached is not None:
        return cached

    lock_key = f"lock:{key}"
    token    = uuid.uuid4().hex

    if rc.set(lock_key, token, nx=True, ex=LOCK_TTL):
        try:
            cached = rc.get(key)               # double-check: someone may have just filled it
            if cached is None:
                cached = json.dumps(db.query("hot"))
                rc.set(key, cached, ex=HOT_TTL)
            return cached
        finally:
            rc.eval(RELEASE_LOCK, 1, lock_key, token)

    deadline = time.monotonic() + LOCK_WAIT
    while time.monotonic() < deadline:
        time.sleep(LOCK_POLL)
        cached = rc.get(key)
        if cached is not None:
            return cached

    value = json.dumps(db.query("hot"))        # fallback: lock holder failed
    rc.set(key, value, ex=HOT_TTL)
    return value


def run_stampede(rc: RedisCluster, fetch, label: str) -> tuple[int, float, int]:
    """Fire STAMPEDE_THREADS simultaneous requests at an empty cache."""
    rc.delete(HOT_KEY, f"lock:{HOT_KEY}")      # start with a cold cache
    db      = FakeDB(STAMPEDE_DB_LAT, {"hot": {"id": "hot", "name": "Hot product"}})
    barrier = threading.Barrier(STAMPEDE_THREADS)
    errors: list[str] = []

    def worker():
        try:
            barrier.wait()                      # release all threads at the same instant
            fetch(rc, db, HOT_KEY)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker) for _ in range(STAMPEDE_THREADS)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    print(f"  [{label}] {STAMPEDE_THREADS} threads -> DB calls: {db.calls:>3}   "
          f"wall time: {elapsed:.2f}s   errors: {len(errors)}")
    for err in errors[:3]:
        print(f"      [ERROR] {err}")
    return db.calls, elapsed, len(errors)


def demo_stampede(rc: RedisCluster) -> bool:
    section("1. CACHE STAMPEDE  (hot key expires, many threads miss at once)")
    print(f"  {STAMPEDE_THREADS} threads request the same key on an empty cache.")
    print(f"  Each DB query takes {int(STAMPEDE_DB_LAT * 1000)} ms.\n")

    rc.get(HOT_KEY)                             # warm up connections before timing
    calls_before, time_before, err_before = run_stampede(rc, fetch_unprotected, "PROBLEM: no protection  ")
    calls_after,  time_after,  err_after  = run_stampede(rc, fetch_with_lock,   "FIXED:   SET NX lock    ")

    rc.delete(HOT_KEY, f"lock:{HOT_KEY}")
    print()
    print_comparison("DB calls", calls_before, calls_after)
    ok = calls_after < calls_before and calls_after <= 2 and err_before + err_after == 0
    print(f"  Result: {'PASS' if ok else 'FAIL'} – only one thread rebuilt the cache"
          if ok else "  Result: FAIL – lock did not reduce DB calls")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# 2. Cache avalanche
# ══════════════════════════════════════════════════════════════════════════════
def run_avalanche(rc: RedisCluster, ttl_fn, label: str) -> list[int]:
    """Cache AVALANCHE_KEYS keys and return each key's expiry offset in seconds."""
    keys = [f"{AVALANCHE_PREFIX}{i}" for i in range(AVALANCHE_KEYS)]
    t_start = time.time()
    for key in keys:
        rc.set(key, "cached-value", ex=ttl_fn())
    t_end = time.time()

    offsets = []
    for key in keys:
        pttl = rc.pttl(key)
        if pttl > 0:
            # expiry moment relative to the start of the run (removes loop drift)
            offsets.append(round((time.time() + pttl / 1000.0) - t_start))
    rc.delete(*keys)

    print(f"  [{label}]  {AVALANCHE_KEYS} keys written in {t_end - t_start:.2f}s")
    return offsets


def print_distribution(offsets: list[int]):
    """Histogram of expiry times plus the busiest single second."""
    lo = (min(offsets) // BUCKET_SECONDS) * BUCKET_SECONDS
    hi = (max(offsets) // BUCKET_SECONDS) * BUCKET_SECONDS
    counts = Counter((o // BUCKET_SECONDS) * BUCKET_SECONDS for o in offsets)
    for start in range(lo, hi + BUCKET_SECONDS, BUCKET_SECONDS):
        n   = counts.get(start, 0)
        bar = "#" * math.ceil(n * 40 / len(offsets)) if n else ""
        print(f"    expire in {start:>3}-{start + BUCKET_SECONDS - 1:<3}s | {bar:<40} {n:>3}")
    print(f"    TTL range: {min(offsets)}s .. {max(offsets)}s   spread: {max(offsets) - min(offsets)}s")


def peak_per_second(offsets: list[int]) -> int:
    """Largest number of keys that expire in the same second."""
    return Counter(offsets).most_common(1)[0][1]


def demo_avalanche(rc: RedisCluster) -> bool:
    section("2. CACHE AVALANCHE  (many keys expire at the same time)")
    print(f"  {AVALANCHE_KEYS} keys cached with a base TTL of {BASE_TTL}s.\n")

    before = run_avalanche(rc, lambda: BASE_TTL, "PROBLEM: identical TTL    ")
    print_distribution(before)
    print()
    after = run_avalanche(rc, lambda: BASE_TTL + random.randint(0, TTL_JITTER),
                          f"FIXED:   TTL + jitter 0-{TTL_JITTER}s")
    print_distribution(after)

    peak_before, peak_after = peak_per_second(before), peak_per_second(after)
    print()
    print_comparison("Max keys expiring/sec", peak_before, peak_after)
    print(f"  Share of cache lost in the busiest second: "
          f"{peak_before * 100 // AVALANCHE_KEYS}% -> {peak_after * 100 // AVALANCHE_KEYS}%")
    ok = peak_after < peak_before and (max(after) - min(after)) > (max(before) - min(before))
    print(f"  Result: {'PASS – expiries are spread out, no mass expiry' if ok else 'FAIL – expiries still clustered'}")
    return ok


# ══════════════════════════════════════════════════════════════════════════════
# 3. Cache penetration
# ══════════════════════════════════════════════════════════════════════════════
def get_item_unprotected(rc: RedisCluster, db: FakeDB, item_id: int):
    """Plain cache-aside: a row that does not exist is never cached."""
    key    = f"cp:item:{item_id}"
    cached = rc.get(key)
    if cached is not None:
        return json.loads(cached)
    row = db.query(item_id)
    if row is not None:
        rc.set(key, json.dumps(row), ex=ITEM_TTL)
    return row


def get_item_null_cached(rc: RedisCluster, db: FakeDB, item_id: int):
    """Cache-aside that also caches a short-lived null marker for missing rows."""
    key    = f"cp:item:{item_id}"
    cached = rc.get(key)
    if cached == NULL_MARKER:
        return None                             # known-missing: skip the DB
    if cached is not None:
        return json.loads(cached)
    row = db.query(item_id)
    if row is None:
        rc.set(key, NULL_MARKER, ex=NULL_TTL)
    else:
        rc.set(key, json.dumps(row), ex=ITEM_TTL)
    return row


def run_penetration(rc: RedisCluster, getter, label: str) -> int:
    """Request ids that do not exist; return how many requests reached the DB."""
    rc.delete(*[f"cp:item:{i}" for i in MISSING_IDS])
    db       = FakeDB(PENETRATION_LAT, rows={})          # empty: nothing exists
    requests = MISSING_IDS * REQUESTS_PER_ID
    random.shuffle(requests)

    t0 = time.perf_counter()
    found = sum(1 for item_id in requests if getter(rc, db, item_id) is not None)
    elapsed = time.perf_counter() - t0

    print(f"  [{label}] {len(requests)} requests -> DB calls: {db.calls:>3}   "
          f"wall time: {elapsed:.2f}s   rows found: {found}")
    return db.calls


def demo_penetration(rc: RedisCluster) -> bool:
    section("3. CACHE PENETRATION  (requests for ids that do not exist)")
    total = len(MISSING_IDS) * REQUESTS_PER_ID
    print(f"  {total} requests for {len(MISSING_IDS)} non-existent ids "
          f"({REQUESTS_PER_ID} requests each).\n")

    before = run_penetration(rc, get_item_unprotected, "PROBLEM: no null caching")
    after  = run_penetration(rc, get_item_null_cached, "FIXED:   null marker     ")

    sample_key = f"cp:item:{MISSING_IDS[0]}"
    print(f"\n  Null marker example: GET {sample_key} -> {rc.get(sample_key)!r}  "
          f"(TTL {rc.ttl(sample_key)}s)")
    rc.delete(*[f"cp:item:{i}" for i in MISSING_IDS])

    print()
    print_comparison("DB calls", before, after)
    ok = after < before and after <= len(MISSING_IDS)
    print(f"  Result: {'PASS – only the first request per id hit the DB' if ok else 'FAIL – null marker did not help'}")
    return ok


# ── Main ──────────────────────────────────────────────────────────────────────
DEMOS = {
    "stampede":    demo_stampede,
    "avalanche":   demo_avalanche,
    "penetration": demo_penetration,
}


def main() -> int:
    choice = sys.argv[1] if len(sys.argv) > 1 else "all"
    if choice != "all" and choice not in DEMOS:
        print(f"Usage: python cache_problems.py [all|{'|'.join(DEMOS)}]")
        return 2

    print()
    print("Redis Cluster Cache Problems Demo")
    print("=" * COL)
    rc = connect()

    selected = DEMOS if choice == "all" else {choice: DEMOS[choice]}
    results  = {name: demo(rc) for name, demo in selected.items()}

    section("SUMMARY")
    for name, ok in results.items():
        print(f"  {name:<12} {'PASS' if ok else 'FAIL'}")
    print()
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
