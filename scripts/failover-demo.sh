#!/usr/bin/env bash
# scripts/failover-demo.sh
# ──────────────────────────────────────────────────────────────────────────────
# Automated Redis Cluster High Availability (Failover) Demonstration:
#   1. Starts failover_client.py in the background inside the app container.
#   2. Identifies the master node owning the counter key's hash slot.
#   3. Prints 'cluster nodes' BEFORE stopping the master.
#   4. Stops the master container with 'docker stop'.
#   5. Waits for the cluster to detect the failure and promote its replica to
#      master, then prints 'cluster nodes' AFTER promotion.
#   6. Measures how many seconds the client experienced errors.
#   7. Restarts the stopped container and shows it rejoining as a replica.
#   8. Stops the client and prints a full summary (downtime seconds, old master,
#      new master, and whether any data was lost).
#
# Uses an EXIT trap to ensure the cluster is always restored to a healthy state.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG=$(mktemp /tmp/failover_client.XXXXXX.log)
SEP="────────────────────────────────────────────────────────────────────────"

CLIENT_PID=""
VICTIM_CONTAINER=""

# ── Helper: pick any running Redis node other than the optional argument ─────
healthy_node() {
    local skip="${1:-__none__}"
    for i in 1 2 3 4 5 6; do
        local name="redis-$i"
        [ "$name" = "$skip" ] && continue
        local st
        st=$(docker inspect --format '{{.State.Status}}' "$name" 2>/dev/null || echo "")
        if [ "$st" = "running" ]; then
            echo "$name"
            return
        fi
    done
    echo "redis-1"
}

# ── Helper: get a container's IP on the redis-net network ────────────────────
container_ip() {
    docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$1" 2>/dev/null || echo ""
}

# ── Helper: map an IP address back to container name (redis-1 .. redis-6) ───
container_for_ip() {
    local target="$1"
    for i in 1 2 3 4 5 6; do
        local ip
        ip=$(container_ip "redis-$i")
        if [ "$ip" = "$target" ]; then
            echo "redis-$i"
            return
        fi
    done
    echo "unknown"
}

# ── Helper: find the master IP currently owning a given hash slot ────────────
slot_master_ip() {
    local query_node="$1"
    local slot="$2"
    docker exec "$query_node" redis-cli cluster nodes 2>/dev/null | awk -v slot="$slot" '
    /master/ && !/fail/ {
        for (i = 9; i <= NF; i++) {
            n = split($i, r, "-")
            if (n == 1 && slot+0 == r[1]+0) {
                split($2, a, "@"); split(a[1], hp, ":"); print hp[1]; exit
            } else if (n == 2 && slot+0 >= r[1]+0 && slot+0 <= r[2]+0) {
                split($2, a, "@"); split(a[1], hp, ":"); print hp[1]; exit
            }
        }
    }'
}

# ── Cleanup trap (always runs on exit) ───────────────────────────────────────
cleanup() {
    local exit_code=$?
    echo ""
    echo "$SEP"
    echo "  CLEANUP & CLUSTER HEALTH RESTORATION"
    echo "$SEP"

    # Stop background client if still running
    if [ -n "$CLIENT_PID" ]; then
        kill "$CLIENT_PID" 2>/dev/null || true
        docker exec app pkill -f failover_client.py 2>/dev/null || true
        wait "$CLIENT_PID" 2>/dev/null || true
        echo "  [OK] Background client stopped."
    fi

    # Ensure all 6 redis containers are running
    for i in 1 2 3 4 5 6; do
        local cname="redis-$i"
        local st
        st=$(docker inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo "missing")
        if [ "$st" != "running" ]; then
            echo "  [OK] Starting stopped container $cname..."
            docker start "$cname" >/dev/null
        fi
    done

    # Wait until cluster_state:ok
    local hnode
    hnode=$(healthy_node)
    for _ in $(seq 1 30); do
        local cstate
        cstate=$(docker exec "$hnode" redis-cli cluster info 2>/dev/null \
                 | grep cluster_state | tr -d '[:space:]' || echo "")
        if [ "$cstate" = "cluster_state:ok" ]; then
            echo "  [OK] Verified cluster_state:ok on all nodes."
            break
        fi
        sleep 1
    done

    rm -f "$LOG"
    echo "$SEP"
    exit "$exit_code"
}
trap cleanup EXIT

# ── 0. Pre-flight check ──────────────────────────────────────────────────────
echo ""
echo "$SEP"
echo "  PRE-FLIGHT CHECK"
echo "$SEP"
INIT_STATE=$(docker exec redis-1 redis-cli cluster info 2>/dev/null \
             | grep cluster_state | tr -d '[:space:]' || echo "cluster_state:fail")
if [ "$INIT_STATE" != "cluster_state:ok" ]; then
    echo "ERROR: Cluster is not in 'ok' state ($INIT_STATE). Run ./scripts/cluster-up.sh first."
    exit 1
fi
echo "  Cluster state is OK."

# Copy latest failover_client.py into app container so rebuild is not required
docker cp "$PROJECT_DIR/app/failover_client.py" app:/app/failover_client.py
docker exec app sed -i 's/\r//' /app/failover_client.py

# ── 1. Start failover_client.py in the background ────────────────────────────
echo ""
echo "$SEP"
echo "  STEP 1 – Start failover_client.py in the background"
echo "$SEP"
docker exec app pkill -f failover_client.py 2>/dev/null || true

docker exec -i app python -u /app/failover_client.py >"$LOG" 2>&1 &
CLIENT_PID=$!
echo "  Started failover_client.py (PID: $CLIENT_PID, log: $LOG)"
echo "  Letting client write & read for 3 seconds..."
sleep 3

echo ""
echo "  Recent client output (before failover):"
tail -n 6 "$LOG" | sed 's/^/    /'

# ── 2. Identify the master owning counter:failover ───────────────────────────
echo ""
echo "$SEP"
echo "  STEP 2 – Identify target master node"
echo "$SEP"
SLOT=$(docker exec redis-1 redis-cli cluster keyslot counter:failover | tr -d '[:space:]')
VICTIM_IP=$(slot_master_ip "redis-1" "$SLOT")
VICTIM_CONTAINER=$(container_for_ip "$VICTIM_IP")
HEALTH_NODE=$(healthy_node "$VICTIM_CONTAINER")

# Also find the replica of this victim master before we stop it
VICTIM_NODE_ID=$(docker exec "$HEALTH_NODE" redis-cli cluster nodes | awk -v ip="$VICTIM_IP:" '$2 ~ ip {print $1; exit}')
EXPECTED_REPLICA_IP=$(docker exec "$HEALTH_NODE" redis-cli cluster nodes | awk -v mid="$VICTIM_NODE_ID" '$3 ~ /slave/ && $4 == mid {split($2,a,"@"); split(a[1],hp,":"); print hp[1]; exit}')
EXPECTED_REPLICA_CONTAINER=$(container_for_ip "$EXPECTED_REPLICA_IP")

echo "  Key 'counter:failover' hashes to slot : $SLOT"
echo "  Current master for slot $SLOT         : $VICTIM_CONTAINER ($VICTIM_IP:6379)"
echo "  Its designated replica                : $EXPECTED_REPLICA_CONTAINER ($EXPECTED_REPLICA_IP:6379)"
echo "  Observer node for cluster commands    : $HEALTH_NODE"

# ── 3. Show cluster nodes BEFORE stopping master ─────────────────────────────
echo ""
echo "$SEP"
echo "  STEP 3 – Cluster nodes BEFORE stopping $VICTIM_CONTAINER"
echo "$SEP"
docker exec "$HEALTH_NODE" redis-cli cluster nodes | sed 's/^/  /'

# ── 4. Stop the master container ─────────────────────────────────────────────
echo ""
echo "$SEP"
echo "  STEP 4 – Stopping master container: $VICTIM_CONTAINER ($VICTIM_IP)"
echo "$SEP"
STOP_EPOCH=$(date +%s.%N)
docker stop "$VICTIM_CONTAINER" >/dev/null
echo "  [$(date '+%H:%M:%S')] '$VICTIM_CONTAINER' stopped."
echo "  Waiting for cluster to detect failure (cluster-node-timeout = 5000 ms)"
echo "  and promote replica $EXPECTED_REPLICA_CONTAINER ($EXPECTED_REPLICA_IP)..."

# ── 5. Wait until a NEW master owns the slot and cluster_state is ok ─────────
NEW_MASTER_IP=""
for i in $(seq 1 30); do
    sleep 1
    CSTATE=$(docker exec "$HEALTH_NODE" redis-cli cluster info 2>/dev/null \
             | grep cluster_state | tr -d '[:space:]' || echo "cluster_state:fail")
    CUR_OWNER=$(slot_master_ip "$HEALTH_NODE" "$SLOT")
    echo "    t+${i}s: $CSTATE | slot $SLOT master = ${CUR_OWNER:-none}"

    if [ -n "$CUR_OWNER" ] && [ "$CUR_OWNER" != "$VICTIM_IP" ] && [ "$CSTATE" = "cluster_state:ok" ]; then
        NEW_MASTER_IP="$CUR_OWNER"
        PROMOTE_EPOCH=$(date +%s.%N)
        break
    fi
done

if [ -z "$NEW_MASTER_IP" ]; then
    echo "ERROR: Replica promotion did not complete within 30 seconds."
    exit 1
fi

NEW_MASTER_CONTAINER=$(container_for_ip "$NEW_MASTER_IP")
FAILOVER_DURATION=$(awk "BEGIN {printf \"%.1f\", $PROMOTE_EPOCH - $STOP_EPOCH}")
echo ""
echo "  ==> Replica promoted! New master for slot $SLOT is $NEW_MASTER_CONTAINER ($NEW_MASTER_IP:6379) after ~${FAILOVER_DURATION}s."

# ── 6. Show cluster nodes AFTER replica promotion ────────────────────────────
echo ""
echo "$SEP"
echo "  STEP 5 – Cluster nodes AFTER replica promotion ($VICTIM_CONTAINER still down)"
echo "$SEP"
docker exec "$HEALTH_NODE" redis-cli cluster nodes | sed 's/^/  /'

echo ""
echo "  Letting client write to the new master ($NEW_MASTER_CONTAINER) for 3 seconds..."
sleep 3

echo ""
echo "  Recent client output (writing to promoted master):"
tail -n 6 "$LOG" | sed 's/^/    /'

# ── 7. Restart old master and watch it rejoin as a replica ───────────────────
echo ""
echo "$SEP"
echo "  STEP 6 – Restarting $VICTIM_CONTAINER and waiting for it to rejoin as replica"
echo "$SEP"
docker start "$VICTIM_CONTAINER" >/dev/null
echo "  [$(date '+%H:%M:%S')] '$VICTIM_CONTAINER' started."

REJOINED_ROLE="unknown"
for i in $(seq 1 20); do
    sleep 1
    FLAGS=$(docker exec "$HEALTH_NODE" redis-cli cluster nodes 2>/dev/null \
            | awk -v ip="$VICTIM_IP:" '$2 ~ ip {print $3; exit}')
    echo "    t+${i}s: $VICTIM_CONTAINER ($VICTIM_IP) flags = ${FLAGS:-unknown}"
    if echo "$FLAGS" | grep -q "slave" && ! echo "$FLAGS" | grep -q "fail"; then
        REJOINED_ROLE="replica (slave of $NEW_MASTER_CONTAINER)"
        break
    fi
done

echo ""
echo "$SEP"
echo "  STEP 7 – Cluster nodes AFTER $VICTIM_CONTAINER rejoined"
echo "$SEP"
docker exec "$HEALTH_NODE" redis-cli cluster nodes | sed 's/^/  /'

# ── 8. Stop the client and compute metrics ───────────────────────────────────
echo ""
echo "$SEP"
echo "  STEP 8 – Stopping client and analysing results"
echo "$SEP"
docker exec app pkill -TERM -f failover_client.py 2>/dev/null || true
sleep 1
kill "$CLIENT_PID" 2>/dev/null || true
wait "$CLIENT_PID" 2>/dev/null || true
CLIENT_PID=""

OK_COUNT=$(grep -c '\[OK\]' "$LOG" 2>/dev/null || true)
ERR_COUNT=$(grep -c '\[ERROR\]' "$LOG" 2>/dev/null || true)
TOTAL_COUNT=$((OK_COUNT + ERR_COUNT))

# Read final counter value stored in the cluster
FINAL_COUNTER=$(docker exec "$HEALTH_NODE" redis-cli -c get counter:failover | tr -d '[:space:]')

# Calculate client error window duration from first [ERROR] to first [OK] after errors
FIRST_ERR_TS=$(grep '\[ERROR\]' "$LOG" 2>/dev/null | head -n 1 | sed -n 's/^\[\([0-9:.]*\)\].*/\1/p' || true)
LAST_ERR_TS=$(grep '\[ERROR\]' "$LOG" 2>/dev/null | tail -n 1 | sed -n 's/^\[\([0-9:.]*\)\].*/\1/p' || true)
RECOVERY_OK_TS=""
if [ -n "$LAST_ERR_TS" ]; then
    RECOVERY_OK_TS=$(awk '/\[ERROR\]/{found=1} found && /\[OK\]/{print; exit}' "$LOG" \
                     | sed -n 's/^\[\([0-9:.]*\)\].*/\1/p' || true)
fi

to_seconds() {
    echo "$1" | awk -F'[:.]' '{printf "%.3f", ($1 * 3600) + ($2 * 60) + $3 + ($4 / 1000)}'
}

if [ -n "$FIRST_ERR_TS" ] && [ -n "$RECOVERY_OK_TS" ]; then
    T_START=$(to_seconds "$FIRST_ERR_TS")
    T_END=$(to_seconds "$RECOVERY_OK_TS")
    CLIENT_DOWNTIME=$(awk "BEGIN {printf \"%.2f s (from %s to %s)\", $T_END - $T_START, \"$FIRST_ERR_TS\", \"$RECOVERY_OK_TS\"}")
elif [ -n "$FIRST_ERR_TS" ] && [ -n "$LAST_ERR_TS" ]; then
    T_START=$(to_seconds "$FIRST_ERR_TS")
    T_END=$(to_seconds "$LAST_ERR_TS")
    CLIENT_DOWNTIME=$(awk "BEGIN {printf \"%.2f s (from %s to %s)\", ($T_END - $T_START) + 0.2, \"$FIRST_ERR_TS\", \"$LAST_ERR_TS\"}")
else
    CLIENT_DOWNTIME="0.00 s"
fi

if [ "$ERR_COUNT" -gt 0 ]; then
    echo "  Sample client log around failover window:"
    grep -C 2 '\[ERROR\]' "$LOG" | head -n 20 | sed 's/^/    /'
fi

# Check whether any acknowledged writes were lost
LOST_WRITES=$((OK_COUNT - FINAL_COUNTER))
if [ "$LOST_WRITES" -le 0 ]; then
    DATA_LOSS_MSG="No (0 acknowledged writes lost; $OK_COUNT OK writes == final counter $FINAL_COUNTER)"
else
    DATA_LOSS_MSG="Yes ($LOST_WRITES write(s) lost due to async replication; $OK_COUNT OK writes vs final counter $FINAL_COUNTER)"
fi

echo ""
echo "$SEP"
echo "  FAILOVER SUMMARY"
echo "$SEP"
printf "  %-28s : %s\n" "Hash slot for key"       "$SLOT (key: counter:failover)"
printf "  %-28s : %s\n" "Old master (stopped)"    "$VICTIM_CONTAINER ($VICTIM_IP:6379)"
printf "  %-28s : %s\n" "New master (promoted)"   "$NEW_MASTER_CONTAINER ($NEW_MASTER_IP:6379)"
printf "  %-28s : %s\n" "Old master rejoined as"  "$REJOINED_ROLE"
printf "  %-28s : %s\n" "Client downtime"         "$CLIENT_DOWNTIME"
printf "  %-28s : %s\n" "Total client operations" "$TOTAL_COUNT (OK: $OK_COUNT, ERROR: $ERR_COUNT)"
printf "  %-28s : %s\n" "Final counter value"     "$FINAL_COUNTER"
printf "  %-28s : %s\n" "Data lost?"              "$DATA_LOSS_MSG"
echo "$SEP"
