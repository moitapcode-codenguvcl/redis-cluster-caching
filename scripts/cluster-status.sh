#!/usr/bin/env bash
# scripts/cluster-status.sh
# Display a human-readable overview of the Redis Cluster:
#   1. cluster info    – global state, epoch, slots assigned, etc.
#   2. cluster nodes   – raw node list (id, role, host:port, slots)
#   3. Slot distribution – which master owns which hash-slot ranges
# Usage: ./scripts/cluster-status.sh
set -euo pipefail

SEPARATOR="──────────────────────────────────────────────────────────────"

# ── 1. cluster info ─────────────────────────────────────────────────────────
echo ""
echo "$SEPARATOR"
echo "  CLUSTER INFO  (from redis-1)"
echo "$SEPARATOR"
docker exec redis-1 redis-cli cluster info

# ── 2. cluster nodes ────────────────────────────────────────────────────────
echo ""
echo "$SEPARATOR"
echo "  CLUSTER NODES  (id | addr | flags | master-id | slots)"
echo "$SEPARATOR"
docker exec redis-1 redis-cli cluster nodes

# ── 3. Slot distribution per master ─────────────────────────────────────────
echo ""
echo "$SEPARATOR"
echo "  HASH SLOT DISTRIBUTION  (master nodes only)"
echo "$SEPARATOR"

# Parse cluster nodes output: columns are
# <id> <ip:port@cport> <flags> <master-id> <ping> <pong> <epoch> <state> [slots...]
docker exec redis-1 redis-cli cluster nodes | awk '
/master/ && !/slave/ {
  # Extract container addr (field 2 is ip:port@cport, take the part before @)
  split($2, addr, "@")
  split(addr[1], hp, ":")
  host = hp[1]

  # Collect all slot ranges (fields 9 onwards)
  slots = ""
  for (i = 9; i <= NF; i++) {
    slots = slots " " $i
  }

  printf "  %-20s  Slots:%s\n", host, slots
}
'

# ── 4. Summary counts ────────────────────────────────────────────────────────
echo ""
echo "$SEPARATOR"
echo "  SUMMARY"
echo "$SEPARATOR"

NODES_OUTPUT=$(docker exec redis-1 redis-cli cluster nodes)

MASTERS=$(echo "$NODES_OUTPUT" | grep -c "master" || true)
REPLICAS=$(echo "$NODES_OUTPUT" | grep -c "slave"  || true)
CLUSTER_STATE=$(docker exec redis-1 redis-cli cluster info \
                | grep cluster_state | tr -d '[:space:]')
SLOTS_ASSIGNED=$(docker exec redis-1 redis-cli cluster info \
                 | grep cluster_slots_assigned | awk -F: '{print $2}' | tr -d '[:space:]')

echo "  Masters  : $MASTERS"
echo "  Replicas : $REPLICAS"
echo "  Slots assigned : $SLOTS_ASSIGNED / 16384"
echo "  $CLUSTER_STATE"
echo "$SEPARATOR"
echo ""
