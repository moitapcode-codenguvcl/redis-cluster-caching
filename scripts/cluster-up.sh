#!/usr/bin/env bash
# scripts/cluster-up.sh
# Start the Redis Cluster via Docker Compose and wait until it is healthy.
# Usage: ./scripts/cluster-up.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> Starting Redis Cluster containers..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d

echo "==> Waiting for cluster-init to finish..."
# Poll until the init container exits (success or failure)
for i in $(seq 1 60); do
  STATUS=$(docker inspect --format '{{.State.Status}}' cluster-init 2>/dev/null || echo "missing")
  EXIT_CODE=$(docker inspect --format '{{.State.ExitCode}}' cluster-init 2>/dev/null || echo "1")

  if [ "$STATUS" = "exited" ] && [ "$EXIT_CODE" = "0" ]; then
    echo "    cluster-init finished successfully."
    break
  elif [ "$STATUS" = "exited" ] && [ "$EXIT_CODE" != "0" ]; then
    echo "ERROR: cluster-init exited with code $EXIT_CODE. Logs:"
    docker logs cluster-init
    exit 1
  fi

  echo "    cluster-init still running (attempt $i/60)..."
  sleep 3
done

echo "==> Waiting for cluster_state:ok on redis-1..."
for i in $(seq 1 30); do
  STATE=$(docker exec redis-1 redis-cli cluster info 2>/dev/null \
          | grep cluster_state | tr -d '[:space:]' || true)
  if [ "$STATE" = "cluster_state:ok" ]; then
    echo "    cluster_state:ok confirmed!"
    echo ""
    echo "==> Redis Cluster is UP and HEALTHY."
    exit 0
  fi
  echo "    Not ready yet (attempt $i/30): $STATE"
  sleep 2
done

echo "ERROR: Cluster did not reach cluster_state:ok in time."
exit 1
