"""
demo_cache.py  –  Cache-aside pattern demo with Redis Cluster
=============================================================
Simulates a slow database of product records.
Reads 20 products twice, reporting HIT/MISS and latency each time.
Prints a summary table and shows which cluster node/slot each key uses.
"""

import json
import time
import sys
from redis.cluster import RedisCluster, ClusterNode

# ── Connection ────────────────────────────────────────────────────────────────
STARTUP_NODE = ClusterNode("redis-1", 6379)
TTL_SECONDS  = 60          # cache time-to-live
PRODUCT_IDS  = list(range(1, 21))   # products 1..20


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
def get_product_from_db(product_id: int) -> dict:
    """Pretend to query a slow relational database (200 ms latency)."""
    time.sleep(0.200)
    return {
        "id":       product_id,
        "name":     f"Product {product_id}",
        "price":    round(9.99 + product_id * 1.5, 2),
        "stock":    (product_id * 7) % 100,
        "category": ["Electronics", "Books", "Clothing", "Food"][product_id % 4],
    }


# ── Cache-aside read ──────────────────────────────────────────────────────────
def get_product(rc: RedisCluster, product_id: int) -> tuple[dict, str, float]:
    """
    Cache-aside pattern:
      1. Check Redis for key  'product:{id}'
      2. On HIT  → deserialise and return immediately
      3. On MISS → fetch from DB, store in Redis with TTL, then return
    Returns (product_dict, "HIT"|"MISS", elapsed_ms)
    """
    key = f"product:{product_id}"
    t0  = time.perf_counter()

    cached = rc.get(key)
    if cached is not None:
        product  = json.loads(cached)
        status   = "HIT"
    else:
        product  = get_product_from_db(product_id)
        rc.set(key, json.dumps(product), ex=TTL_SECONDS)
        status   = "MISS"

    elapsed_ms = (time.perf_counter() - t0) * 1000
    return product, status, elapsed_ms


# ── Slot / node mapping ───────────────────────────────────────────────────────
def build_slot_map(rc: RedisCluster) -> dict[int, str]:
    """
    Return a dict mapping slot_number → 'host:port' for every master.
    Uses CLUSTER SHARDS (Redis 7+) for efficiency.
    """
    slot_map: dict[int, str] = {}
    try:
        shards = rc.cluster_shards()        # list of shard dicts
        for shard in shards:
            nodes  = shard.get("nodes", [])
            ranges = shard.get("slots", [])
            # Find the primary node
            primary = next(
                (n for n in nodes if n.get("role") == "master"), None
            )
            if primary is None:
                continue
            addr = f"{primary['ip']}:{primary['port']}"
            # Slot ranges come as a flat list [start, end, start, end, ...]
            it = iter(ranges)
            for start, end in zip(it, it):
                for slot in range(start, end + 1):
                    slot_map[slot] = addr
    except Exception as exc:
        print(f"[WARN] Could not build slot map: {exc}")
    return slot_map


def key_location(rc: RedisCluster, key: str, slot_map: dict[int, str]) -> tuple[int, str]:
    """Return (slot, 'host:port') for a given key."""
    slot = rc.keyslot(key)
    node = slot_map.get(slot, "unknown")
    return slot, node


# ── Pretty printers ───────────────────────────────────────────────────────────
COL = 60   # separator width

def section(title: str):
    print()
    print("=" * COL)
    print(f"  {title}")
    print("=" * COL)


def run_pass(rc: RedisCluster, pass_num: int, slot_map: dict[int, str]) -> list[tuple]:
    """
    Read all 20 products, print one line per product, return timing rows.
    Returns list of (product_id, status, elapsed_ms).
    """
    section(f"PASS {pass_num}  –  reading {len(PRODUCT_IDS)} products")
    print(f"  {'ID':>4}  {'STATUS':6}  {'ms':>8}  {'Slot':>6}  Node")
    print(f"  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*6}  {'-'*21}")

    rows = []
    for pid in PRODUCT_IDS:
        product, status, ms = get_product(rc, pid)
        key                  = f"product:{pid}"
        slot, node           = key_location(rc, key, slot_map)
        flag = "✓" if status == "HIT" else "·"
        print(f"  {pid:>4}  {flag} {status:5}  {ms:>7.1f}  {slot:>6}  {node}")
        rows.append((pid, status, ms))
    return rows


# ── Summary table ─────────────────────────────────────────────────────────────
def print_summary(pass1: list[tuple], pass2: list[tuple]):
    section("SUMMARY")

    def avg_ms(rows):
        return sum(r[2] for r in rows) / len(rows)

    hits   = sum(1 for _, s, _ in pass2 if s == "HIT")
    misses = sum(1 for _, s, _ in pass2 if s == "MISS")
    hit_rate = hits / len(pass2) * 100

    avg1 = avg_ms(pass1)
    avg2 = avg_ms(pass2)
    speedup = avg1 / avg2 if avg2 > 0 else float("inf")

    print(f"  {'Metric':<30}  {'Pass 1':>10}  {'Pass 2':>10}")
    print(f"  {'-'*30}  {'-'*10}  {'-'*10}")
    print(f"  {'Requests':<30}  {len(pass1):>10}  {len(pass2):>10}")
    print(f"  {'Cache HITs':<30}  {'0':>10}  {hits:>10}")
    print(f"  {'Cache MISSes':<30}  {len(pass1):>10}  {misses:>10}")
    print(f"  {'Hit rate':<30}  {'0.0 %':>10}  {hit_rate:>9.1f}%")
    print(f"  {'Avg latency (ms)':<30}  {avg1:>9.1f}  {avg2:>9.1f}")
    print(f"  {'Speed-up factor':<30}  {'1.0 ×':>10}  {speedup:>9.1f}×")
    print()


# ── Sharding overview ─────────────────────────────────────────────────────────
def print_sharding(rc: RedisCluster, slot_map: dict[int, str]):
    section("SHARDING OVERVIEW  –  product keys by cluster node")

    # Group keys by node
    by_node: dict[str, list] = {}
    for pid in PRODUCT_IDS:
        key        = f"product:{pid}"
        slot, node = key_location(rc, key, slot_map)
        by_node.setdefault(node, []).append((pid, slot))

    for node, items in sorted(by_node.items()):
        ids   = ", ".join(str(p) for p, _ in items)
        slots = ", ".join(str(s) for _, s in items)
        print(f"  Node {node}")
        print(f"    Products : {ids}")
        print(f"    Slots    : {slots}")
        print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print()
    print("Redis Cluster Cache-Aside Demo")
    print("=" * COL)

    rc       = connect()
    slot_map = build_slot_map(rc)

    # Flush only our keys so the demo is repeatable without restarting cluster
    print("\n[INFO] Clearing product keys for a clean run...")
    pipe = rc.pipeline()
    for pid in PRODUCT_IDS:
        rc.delete(f"product:{pid}")
    print("[INFO] Keys cleared.\n")

    pass1 = run_pass(rc, 1, slot_map)   # all MISSes → hits DB
    pass2 = run_pass(rc, 2, slot_map)   # all HITs  → served from cache

    print_sharding(rc, slot_map)
    print_summary(pass1, pass2)


if __name__ == "__main__":
    main()
