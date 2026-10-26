# Distributed Caching and In-Memory Data Store with Redis Cluster

A university Linux course project that demonstrates **distributed caching, sharding and
high availability** with a 6-node Redis Cluster (3 masters + 3 replicas) orchestrated by
Docker Compose, plus a Python client application that exercises it.

## Table of contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Project structure](#project-structure)
4. [Prerequisites](#prerequisites)
5. [Quick start](#quick-start)
6. [Running each step](#running-each-step)
7. [What each script shows (and expected output)](#what-each-script-shows-and-expected-output)
8. [Troubleshooting](#troubleshooting)

---

## Overview

| Topic | What the project shows |
|---|---|
| Sharding | 16384 hash slots split across 3 masters; every key lives on exactly one master |
| Replication / HA | Each master has a replica; if a master dies, its replica is promoted automatically |
| Caching | The *cache-aside* pattern in front of a slow (simulated) database |
| Cache problems | Stampede, avalanche and penetration, each with a problem run and a fixed run |
| Eviction | `allkeys-lru` vs `allkeys-lfu` under memory pressure |
| Performance | `redis-benchmark` throughput in cluster mode |

## Architecture

All containers share one Docker bridge network (`redis-net`). No Redis port is published to
the host; everything is reached from inside the network (via the `app` container or
`docker exec`).

```text
                         Docker network: redis-net
 ┌──────────────────────────────────────────────────────────────────────┐
 │                                                                      │
 │   ┌───────────────────────────┐                                      │
 │   │  app  (python:3.12-slim)  │   cluster-aware client (redis-py)    │
 │   │  demo_cache.py            │──────────────┐                       │
 │   │  failover_client.py       │              │ MOVED/ASK redirects   │
 │   │  cache_problems.py        │              │ handled by the client │
 │   │  eviction_demo.py         │              ▼                       │
 │   └───────────────────────────┘                                      │
 │                                                                      │
 │   slots 0-5460       slots 5461-10922      slots 10923-16383         │
 │   ┌────────────┐     ┌────────────┐        ┌────────────┐            │
 │   │  MASTER    │     │  MASTER    │        │  MASTER    │            │
 │   │  redis-1   │     │  redis-2   │        │  redis-3   │            │
 │   └─────┬──────┘     └─────┬──────┘        └─────┬──────┘            │
 │         │ replication      │ replication         │ replication       │
 │   ┌─────▼──────┐     ┌─────▼──────┐        ┌─────▼──────┐            │
 │   │  REPLICA   │     │  REPLICA   │        │  REPLICA   │            │
 │   │  redis-5   │     │  redis-6   │        │  redis-4   │            │
 │   └────────────┘     └────────────┘        └────────────┘            │
 │                                                                      │
 │   (nodes talk to each other on the cluster bus, port 16379)          │
 └──────────────────────────────────────────────────────────────────────┘

 cluster-init : one-shot container that runs `redis-cli --cluster create` and exits.
```

> The exact master/replica pairing is chosen by `redis-cli --cluster create`, so it can
> differ from the picture. Run `./scripts/cluster-status.sh` to see the real layout.

## Project structure

```text
redis-cluster-caching/
├── app/
│   ├── Dockerfile            # python:3.12-slim image, idles with "sleep infinity"
│   ├── requirements.txt      # redis[hiredis]
│   ├── demo_cache.py         # cache-aside demo (HIT/MISS, sharding)
│   ├── failover_client.py    # continuous writer used by the failover demo
│   ├── cache_problems.py     # stampede / avalanche / penetration + fixes
│   └── eviction_demo.py      # allkeys-lru vs allkeys-lfu
├── scripts/
│   ├── cluster-up.sh         # start the cluster and wait until healthy
│   ├── cluster-down.sh       # stop it (--volumes also deletes data)
│   ├── cluster-status.sh     # cluster info, nodes, slot distribution
│   ├── failover-demo.sh      # kill a master, watch the replica take over
│   ├── benchmark.sh          # redis-benchmark SET/GET in cluster mode
│   └── run-all.sh            # run every stage and print a PASS/FAIL table
├── results/                  # benchmark.txt and run-all logs (generated)
├── docker-compose.yml        # 6 Redis nodes + cluster-init + app
├── README.md                 # this file
└── HUONG_DAN.md              # Vietnamese step-by-step guide
```

## Prerequisites

- Linux, or Windows with **WSL 2** (this project was developed on Ubuntu in WSL).
- **Docker Engine** with the **Compose v2** plugin (`docker compose ...`).
  Docker Desktop with WSL integration enabled also works.
- Internet access the first time (to pull `redis:7` and `python:3.12-slim`).
- About 1 GB of free disk space and 1 GB of free RAM.
- Shell scripts must use **LF** line endings (they fail with CRLF).

Check your setup:

```bash
docker --version
docker compose version
docker info > /dev/null && echo "Docker is running"
```

## Quick start

```bash
cd ~/redis-cluster-caching
./scripts/run-all.sh --clean     # build + run every stage from zero, print PASS/FAIL table
```

This takes a few minutes. Without `--clean` the existing cluster and data are reused.
Per-stage logs are saved in `results/run-all/`.

## Running each step

Run everything from the project root.

| # | Step | Command |
|---|---|---|
| 1 | Start the cluster | `./scripts/cluster-up.sh` |
| 2 | Show cluster status | `./scripts/cluster-status.sh` |
| 3 | Cache-aside demo | `docker compose exec app python demo_cache.py` |
| 4 | Failover demo | `./scripts/failover-demo.sh` |
| 5 | Cache problems | `docker compose exec app python cache_problems.py` |
| 5a | …a single problem | `docker compose exec app python cache_problems.py stampede` (or `avalanche`, `penetration`) |
| 6 | Eviction policies | `docker compose exec app python eviction_demo.py` |
| 7 | Benchmark | `./scripts/benchmark.sh` |
| – | Stop (keep data) | `./scripts/cluster-down.sh` |
| – | Stop and delete data | `./scripts/cluster-down.sh --volumes` |

> The `app` image copies the Python files at build time. After editing anything in `app/`,
> rebuild it: `docker compose build app && docker compose up -d --no-deps app`
> (`run-all.sh` does this automatically).

## What each script shows (and expected output)

### `cluster-up.sh` and `cluster-status.sh`
Starts the 6 nodes, waits for `cluster-init` to build the cluster, and checks
`cluster_state:ok`. The status script then prints:

```text
Masters  : 3
Replicas : 3
Slots assigned : 16384 / 16384
cluster_state:ok
```

### `demo_cache.py` – cache-aside
Reads 20 products twice. The first pass misses and takes about 200 ms per product
(simulated database); the second pass hits the cache and takes about 1 ms. A table shows
which slot and which master holds each key, so you can see sharding in action.

### `failover-demo.sh` – high availability
Runs a client that increments a counter every 200 ms, then stops the master that owns the
counter's slot. Expected: a short burst of `[ERROR]` lines (roughly 5-10 seconds), then a replica
is promoted and the client continues with `[OK]`. The stopped node is restarted and
rejoins as a replica. The summary reports the downtime and whether writes were lost
(`No` in a normal run).

### `cache_problems.py` – three classic problems

| Problem | Without fix | With fix | Typical result |
|---|---|---|---|
| Stampede (50 threads, 1 hot key) | every thread queries the DB | distributed lock (`SET NX EX`), one rebuilds | 50 → **1** DB calls |
| Avalanche (200 keys) | identical TTL, all expire in the same second | TTL + random jitter 0–30 s | 200 keys/sec → about **12** keys/sec |
| Penetration (100 requests, 10 missing ids) | every request hits the DB | cache a null marker for 30 s | 100 → **10** DB calls |

Each section ends with `Result: PASS`, and the final summary lists all three.

### `eviction_demo.py` – LRU vs LFU
Temporarily sets a small `maxmemory` on one master, reads a "hot" key 500 times, leaves it
idle, then writes thousands of one-off keys. Expected:

```text
allkeys-lru    EVICTED   (only recency counts)
allkeys-lfu    KEPT      (frequently used keys survive)
```

The original `maxmemory` and policy are always restored (`maxmemory=0`, `noeviction`).
While the limit is small, other keys on that node (such as `product:*`) can be evicted too.

### `benchmark.sh` – throughput
Runs `redis-benchmark --cluster` (SET and GET, 100000 requests, 50 clients) in a throw-away
`redis:7` container on `redis-net`, saves the full output to `results/benchmark.txt` and
prints requests per second, for example `SET ≈ 130k–200k` and `GET ≈ 200k` requests/sec.
Numbers depend on your machine. Two tests that finish in the same time can print the same
value, because the tool measures time with about 1 ms resolution. The `key:*` keys the
tool creates are deleted afterwards.

### `run-all.sh` – everything
Runs all stages above in order with a banner for each and finishes with a table:

```text
  #    Stage              Result   Time
  1    cluster up         PASS     ...
  ...
  OVERALL: PASS  (7/7 stages, ...)
```

Use `--clean` to delete containers and volumes first. The exit code is 0 only if every
stage passed.

## Troubleshooting

**`Cannot connect to the Docker daemon` / `docker info` fails**
Docker is not running. Start the Docker service (`sudo service docker start` on WSL
without Docker Desktop) or start Docker Desktop and enable WSL integration for your distro.
If you get `permission denied`, add yourself to the group: `sudo usermod -aG docker $USER`,
then open a new shell.

**Scripts fail with `bad interpreter` or `\r: command not found`**
The files have Windows (CRLF) line endings. Convert them: `sed -i 's/\r$//' scripts/*.sh`.

**Port or name conflicts (`container name "/redis-1" is already in use`, network overlap)**
Remove old containers of this project: `./scripts/cluster-down.sh`. No Redis ports are
published to the host, so host port conflicts are unlikely; if another project uses the
same container names, stop or remove it first (`docker ps -a`).

**Stale volumes / strange cluster state after changing the setup**
Old data volumes keep the previous `nodes.conf`. Start clean:
`./scripts/cluster-down.sh --volumes && ./scripts/cluster-up.sh`
(or `./scripts/run-all.sh --clean`).

**`cluster_state` is not `ok`**
1. Look at the details: `./scripts/cluster-status.sh` and `docker logs cluster-init`.
2. If nodes are marked `fail`, `disconnected` or `noaddr`, container IPs probably changed
   after a Docker/host restart and the nodes still remember the old addresses. Make every
   node re-learn the current IPs by running `CLUSTER MEET <ip> 6379` against each other
   node (`docker inspect redis-N` shows the IP), or simply rebuild with
   `./scripts/cluster-down.sh --volumes && ./scripts/cluster-up.sh`.
3. Right after a failover, wait up to about 15 seconds; the cluster converges on its own.

**`CROSSSLOT Keys in request don't hash to the same slot`**
Multi-key commands (`DEL a b`, `MGET ...`) only work when all keys share a slot. Use one
command per key or hash tags (`{tag}key`). The demos already do this.

**`docker compose exec app ...` says the container is not running**
Run `./scripts/cluster-up.sh` (it starts `app` too), then retry.

**The eviction demo left `maxmemory` set**
It restores the configuration automatically. To check or reset by hand:
`docker exec redis-1 redis-cli config set maxmemory 0` and
`docker exec redis-1 redis-cli config set maxmemory-policy noeviction`.
