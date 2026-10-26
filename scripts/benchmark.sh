#!/usr/bin/env bash
# scripts/benchmark.sh
# ──────────────────────────────────────────────────────────────────────────────
# Benchmark the Redis Cluster with redis-benchmark (from the redis:7 image):
#   - runs inside the cluster's Docker network (nothing is published to the host)
#   - cluster mode (--cluster): requests are spread over all master nodes
#   - SET and GET tests, 100000 requests, 50 parallel clients
#   - full output is saved to results/benchmark.txt, then a short summary of
#     requests per second is printed
#   - the key:* keys created by redis-benchmark are removed afterwards
#
# Usage: ./scripts/benchmark.sh
# Optional overrides: REQUESTS=200000 CLIENTS=100 ./scripts/benchmark.sh
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RESULTS_DIR="$PROJECT_DIR/results"
RESULT_FILE="$RESULTS_DIR/benchmark.txt"

REQUESTS="${REQUESTS:-100000}"
CLIENTS="${CLIENTS:-50}"
KEYSPACE="${KEYSPACE:-100000}"   # random keys per test, so all 3 masters get traffic
SEP="──────────────────────────────────────────────────────────────"

# ── Find the Docker network the cluster runs on (compose names it <project>_redis-net) ──
NETWORK=$(docker inspect redis-1 \
          --format '{{range $name, $_ := .NetworkSettings.Networks}}{{$name}}{{"\n"}}{{end}}' \
          2>/dev/null | head -n 1)
if [ -z "$NETWORK" ]; then
  echo "ERROR: container redis-1 not found. Start the cluster first: ./scripts/cluster-up.sh"
  exit 1
fi

mkdir -p "$RESULTS_DIR"

echo ""
echo "$SEP"
echo "  REDIS BENCHMARK  (cluster mode, network: $NETWORK)"
echo "$SEP"
echo "  Tests    : SET, GET"
echo "  Requests : $REQUESTS"
echo "  Clients  : $CLIENTS"
echo "  Keyspace : $KEYSPACE random keys"
echo ""
echo "==> Running redis-benchmark (this takes a few seconds)..."

# redis-benchmark redraws its progress line with carriage returns: turn them into
# newlines and drop the progress lines so the saved file stays readable.
{
  echo "# redis-benchmark – cluster mode – $(date '+%Y-%m-%d %H:%M:%S')"
  echo "# requests=$REQUESTS clients=$CLIENTS keyspace=$KEYSPACE tests=set,get"
  echo ""
  docker run --rm --network "$NETWORK" redis:7 \
    redis-benchmark -h redis-1 -p 6379 --cluster \
    -t set,get -n "$REQUESTS" -c "$CLIENTS" -r "$KEYSPACE" 2>&1 \
    | tr '\r' '\n' \
    | grep -Ev '^(SET|GET): (rps=|[0-9.]+( requests per second.*)?$)|^[[:space:]]+$' \
    || true
} > "$RESULT_FILE"

# ── Clean up: redis-benchmark leaves up to $KEYSPACE keys named key:<number> behind ──
echo "==> Removing benchmark keys (key:*) from every master..."
for addr in $(docker exec redis-1 redis-cli cluster nodes \
              | awk '$3 ~ /master/ { split($2, a, "@"); print a[1] }'); do
  host="${addr%:*}"
  port="${addr#*:}"
  # UNLINK one key per command (multi-key commands are rejected with CROSSSLOT)
  docker exec redis-1 redis-cli -h "$host" -p "$port" --scan --pattern 'key:*' \
    | sed 's/^/UNLINK /' \
    | docker exec -i redis-1 redis-cli -h "$host" -p "$port" --pipe > /dev/null
done

# ── Summary: requests per second for each test ──
# Each "====== NAME ======" block holds its own "requests completed in X seconds"
# and "throughput summary: N requests per second" lines; parse them per block.
# redis-benchmark measures time with ~1 ms resolution, so two 0.50 s runs can
# legitimately report the identical rate (100000 / 0.501 s = 199600.80).
SUMMARY=$(awk '
  /^====== / { name = $2 }
  /requests completed in/ { secs = $5 }
  /requests per second/ {
    for (i = 1; i < NF; i++)
      if ($i == "requests" && $(i + 1) == "per") { printf "  %-5s %12s requests/sec  (%s s)\n", name, $(i - 1), secs; break }
  }
' "$RESULT_FILE")

if [ -z "$SUMMARY" ]; then
  echo "ERROR: no results found. Output saved in $RESULT_FILE:"
  cat "$RESULT_FILE"
  exit 1
fi

echo ""
echo "$SEP"
echo "  SUMMARY"
echo "$SEP"
echo "$SUMMARY"
echo ""
echo "  Full output saved to: results/benchmark.txt"
echo "$SEP"
echo ""
