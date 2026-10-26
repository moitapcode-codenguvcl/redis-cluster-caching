#!/usr/bin/env bash
# scripts/cluster-down.sh
# Stop the Redis Cluster. Optionally delete all data volumes.
# Usage:
#   ./scripts/cluster-down.sh           # stop only (data preserved)
#   ./scripts/cluster-down.sh --volumes # stop AND delete all named volumes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DELETE_VOLUMES=false
for arg in "$@"; do
  case "$arg" in
    --volumes|-v)
      DELETE_VOLUMES=true
      ;;
    *)
      echo "Unknown flag: $arg"
      echo "Usage: $0 [--volumes]"
      exit 1
      ;;
  esac
done

if [ "$DELETE_VOLUMES" = "true" ]; then
  echo "==> Stopping containers AND removing volumes..."
  docker compose -f "$PROJECT_DIR/docker-compose.yml" down -v
  echo "==> All containers and volumes removed."
else
  echo "==> Stopping containers (data volumes preserved)..."
  docker compose -f "$PROJECT_DIR/docker-compose.yml" down
  echo "==> Containers stopped. To also delete volumes, run: $0 --volumes"
fi
