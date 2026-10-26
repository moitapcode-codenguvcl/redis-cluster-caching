"""
failover_client.py  –  Continuous write/read client for failover demonstration
==============================================================================
Continuously writes (INCR) and reads (GET) a counter key every 200 ms against
the Redis Cluster, printing timestamp, cluster node, value, and OK/ERROR.
Keeps running through failures with retry/reconnection logic (never crashes).
"""

import signal
import time
from datetime import datetime

from redis.backoff import NoBackoff
from redis.cluster import ClusterNode, RedisCluster
from redis.retry import Retry

# ── Configuration ─────────────────────────────────────────────────────────────
STARTUP_NODES = [ClusterNode(f"redis-{i}", 6379) for i in range(1, 7)]
KEY = "counter:failover"
INTERVAL = 0.200  # 200 ms between operations

running = True


def handle_signal(_sig, _frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


def ts() -> str:
    """Return current UTC time as HH:MM:SS.mmm."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def connect_cluster() -> RedisCluster:
    """Create a RedisCluster client with short timeouts so failures surface quickly."""
    while running:
        try:
            rc = RedisCluster(
                startup_nodes=STARTUP_NODES,
                decode_responses=True,
                socket_timeout=0.5,
                socket_connect_timeout=0.5,
                retry=Retry(NoBackoff(), 0),
                reinitialize_steps=1,
            )
            rc.ping()
            return rc
        except Exception as exc:
            print(f"[{ts()}] [ERROR] node=unknown          connect failed: {exc}", flush=True)
            time.sleep(INTERVAL)
    raise SystemExit(0)


def get_key_owner(rc: RedisCluster, key: str) -> str:
    """Return 'host:port' of the master node currently mapped for key."""
    try:
        node = rc.get_node_from_key(key, replica=False)
        return f"{node.host}:{node.port}" if node else "unknown"
    except Exception:
        return "unknown"


def main() -> None:
    print(f"[{ts()}] [START] Connecting to Redis Cluster...", flush=True)
    rc = connect_cluster()

    # Reset counter before starting the test
    try:
        rc.delete(KEY)
    except Exception:
        pass

    owner = get_key_owner(rc, KEY)
    print(
        f"[{ts()}] [START] Ready. key={KEY} owner={owner} interval=200ms",
        flush=True,
    )

    total_ops = 0
    ok_ops = 0
    err_ops = 0

    while running:
        t0 = time.perf_counter()
        total_ops += 1
        node_addr = get_key_owner(rc, KEY)

        try:
            # Write (increment) and read back the counter key
            written_val = rc.incr(KEY)
            read_val = int(rc.get(KEY))
            node_addr = get_key_owner(rc, KEY)
            ok_ops += 1
            print(
                f"[{ts()}] [OK]    node={node_addr:<18} write={written_val} read={read_val}",
                flush=True,
            )
        except Exception as exc:
            err_ops += 1
            msg = str(exc).splitlines()[0][:80]
            print(
                f"[{ts()}] [ERROR] node={node_addr:<18} {type(exc).__name__}: {msg}",
                flush=True,
            )
            # Reconnect / refresh topology so we pick up the newly promoted master
            try:
                rc = RedisCluster(
                    startup_nodes=STARTUP_NODES,
                    decode_responses=True,
                    socket_timeout=0.5,
                    socket_connect_timeout=0.5,
                    retry=Retry(NoBackoff(), 0),
                    reinitialize_steps=1,
                )
            except Exception:
                pass

        elapsed = time.perf_counter() - t0
        time.sleep(max(0.0, INTERVAL - elapsed))

    print(
        f"[{ts()}] [DONE]  total={total_ops} ok={ok_ops} errors={err_ops}",
        flush=True,
    )


if __name__ == "__main__":
    main()
