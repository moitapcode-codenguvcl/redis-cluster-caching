"""
eviction_demo.py  –  allkeys-lru vs allkeys-lfu eviction on a Redis Cluster node
================================================================================
Shows how the two eviction policies treat a "hot" key that was read very often
earlier but is idle while a burst of one-off keys (a scan) fills the cache:

  allkeys-lru  evicts the least RECENTLY used key   -> idle hot key is evicted
  allkeys-lfu  evicts the least FREQUENTLY used key -> hot key is kept

Steps for each policy, on ONE master node (connected to directly):
  1. CONFIG SET maxmemory-policy <policy>
  2. CONFIG SET maxmemory = current usage + a small headroom
  3. write the hot key and read it many times, then let it sit idle
  4. write far more filler keys than fit in the headroom
  5. report whether the hot key survived

The original maxmemory / maxmemory-policy are always restored afterwards and
the demo keys are deleted.  NOTE: while maxmemory is small, other cache keys
that live on the chosen node (e.g. product:*) may be evicted as well.

Usage (inside the app container):
    python eviction_demo.py
"""

import sys
import time

import redis
from redis.cluster import ClusterNode, RedisCluster

# ── Configuration ─────────────────────────────────────────────────────────────
STARTUP_NODE   = ClusterNode("redis-1", 6379)
COL            = 60                      # separator width
POLICIES       = ["allkeys-lru", "allkeys-lfu"]
VALUE          = "x" * 1024              # 1 KiB per key
HEADROOM_BYTES = 2 * 1024 * 1024         # memory the demo may use above the baseline
FILL_KEYS      = 4000                    # ~4 MiB of fillers -> roughly half must be evicted
WARMUP_READS   = 500                     # reads that make the hot key "frequent"
IDLE_SECONDS   = 3                       # hot key idle time (LRU clock has 1 s resolution)
PREFIX         = "evict:"


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


def section(title: str):
    print()
    print("=" * COL)
    print(f"  {title}")
    print("=" * COL)


# ── Key selection ─────────────────────────────────────────────────────────────
def keys_owned_by(rc: RedisCluster, node_name: str, pattern: str, count: int) -> list[str]:
    """
    Return `count` key names whose hash slot belongs to node_name.
    A node in cluster mode only accepts keys from its own slots (else MOVED).
    """
    keys: list[str] = []
    i = 0
    while len(keys) < count:
        key = pattern.format(i)
        if rc.get_node_from_key(key, replica=False).name == node_name:
            keys.append(key)
        i += 1
    return keys


def cleanup(node: redis.Redis):
    """
    Delete every key this demo created on the node.
    One DEL per key (pipelined): in cluster mode a multi-key DEL is rejected
    with CROSSSLOT even when all keys live on the same node.
    """
    keys = list(node.scan_iter(match=f"{PREFIX}*", count=1000))
    for i in range(0, len(keys), 500):
        pipe = node.pipeline(transaction=False)
        for key in keys[i:i + 500]:
            pipe.delete(key)
        pipe.execute()


def count_existing(node: redis.Redis, keys: list[str]) -> int:
    """Count how many of `keys` still exist (single-key EXISTS calls, pipelined)."""
    total = 0
    for i in range(0, len(keys), 500):
        pipe = node.pipeline(transaction=False)
        for key in keys[i:i + 500]:
            pipe.exists(key)
        total += sum(pipe.execute())
    return total


def evicted_keys(node: redis.Redis) -> int:
    return int(node.info("stats")["evicted_keys"])


# ── One policy run ────────────────────────────────────────────────────────────
def run_policy(node: redis.Redis, policy: str, hot_key: str, fill_keys: list[str]) -> dict:
    cleanup(node)
    node.config_set("maxmemory-policy", policy)
    node.config_set("maxmemory", 0)                       # unlimited while measuring baseline
    baseline = int(node.info("memory")["used_memory"])
    limit    = baseline + HEADROOM_BYTES
    node.config_set("maxmemory", limit)
    print(f"\n  [{policy}] baseline used_memory={baseline / 1024:.0f} KiB  "
          f"-> maxmemory={limit / 1024:.0f} KiB")

    # Make the hot key frequently used, then leave it idle.
    node.set(hot_key, VALUE)
    pipe = node.pipeline(transaction=False)
    for _ in range(WARMUP_READS):
        pipe.get(hot_key)
    pipe.execute()
    print(f"  [{policy}] hot key read {WARMUP_READS}x, now idle for {IDLE_SECONDS}s ...")
    time.sleep(IDLE_SECONDS)

    # Burst of one-off keys, far more than fit in the headroom.
    evicted_before = evicted_keys(node)
    pipe = node.pipeline(transaction=False)
    for n, key in enumerate(fill_keys, 1):
        pipe.set(key, VALUE)
        if n % 200 == 0:
            pipe.execute()
    pipe.execute()
    evicted = evicted_keys(node) - evicted_before

    hot_alive = bool(node.exists(hot_key))
    fillers_alive = count_existing(node, fill_keys)
    detail = ""
    if hot_alive:
        # OBJECT FREQ only works with LFU policies, OBJECT IDLETIME only with the others.
        detail = (f"access counter={node.object('freq', hot_key)}" if "lfu" in policy
                  else f"idle={node.object('idletime', hot_key)}s")
    used = int(node.info("memory")["used_memory"])
    print(f"  [{policy}] wrote {len(fill_keys)} filler keys -> evicted {evicted} keys, "
          f"{fillers_alive} fillers remain, used_memory={used / 1024:.0f} KiB")
    return {"policy": policy, "hot_alive": hot_alive, "detail": detail,
            "evicted": evicted, "fillers_alive": fillers_alive}


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    print()
    print("Redis Cluster Eviction Policy Demo (LRU vs LFU)")
    print("=" * COL)
    rc = connect()

    # Pick a master and talk to it directly (CONFIG SET is per node).
    primary   = rc.get_primaries()[0]
    node_name = primary.name
    node      = redis.Redis(host=primary.host, port=primary.port, decode_responses=True)

    original = {
        "maxmemory":        node.config_get("maxmemory")["maxmemory"],
        "maxmemory-policy": node.config_get("maxmemory-policy")["maxmemory-policy"],
    }
    section(f"Target node {node_name}")
    print(f"  Original config : maxmemory={original['maxmemory']}  "
          f"maxmemory-policy={original['maxmemory-policy']}")

    hot_key   = keys_owned_by(rc, node_name, PREFIX + "hot:{}", 1)[0]
    fill_keys = keys_owned_by(rc, node_name, PREFIX + "fill:{}", FILL_KEYS)

    results = []
    try:
        for policy in POLICIES:
            section(f"Policy: {policy}")
            results.append(run_policy(node, policy, hot_key, fill_keys))
    finally:
        # Always restore the node, even on Ctrl-C or an error.  Restore the
        # config FIRST so a failing cleanup can never leave a small maxmemory behind.
        node.config_set("maxmemory", original["maxmemory"])
        node.config_set("maxmemory-policy", original["maxmemory-policy"])
        restored = {k: node.config_get(k)[k] for k in original}
        print()
        print(f"[RESET] Config restored: maxmemory={restored['maxmemory']}  "
              f"maxmemory-policy={restored['maxmemory-policy']}")
        cleanup(node)
        print("[RESET] Demo keys deleted.")

    section("RESULT")
    print(f"  {'policy':<14} {'hot key':<10} {'evicted':>8} {'fillers kept':>13}")
    for r in results:
        state = "KEPT" if r["hot_alive"] else "EVICTED"
        print(f"  {r['policy']:<14} {state:<10} {r['evicted']:>8} {r['fillers_alive']:>13}"
              f"   {r['detail']}")
    print()
    lru, lfu = results
    ok = (not lru["hot_alive"]) and lfu["hot_alive"]
    print("  allkeys-lru drops the idle hot key (only recency counts);")
    print("  allkeys-lfu keeps it (it was read often).")
    print(f"  Result: {'PASS' if ok else 'FAIL – unexpected eviction outcome'}")
    print()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
