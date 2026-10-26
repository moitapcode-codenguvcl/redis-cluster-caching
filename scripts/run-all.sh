#!/usr/bin/env bash
# scripts/run-all.sh
# ──────────────────────────────────────────────────────────────────────────────
# Run the whole project from scratch, stage by stage, and print a PASS/FAIL table:
#   1. cluster up      scripts/cluster-up.sh
#   2. cluster status  scripts/cluster-status.sh
#   3. cache demo      app/demo_cache.py        (cache-aside, HIT/MISS, sharding)
#   4. failover demo   scripts/failover-demo.sh (stop a master, replica takes over)
#   5. cache problems  app/cache_problems.py    (stampede, avalanche, penetration)
#   6. eviction demo   app/eviction_demo.py     (allkeys-lru vs allkeys-lfu)
#   7. benchmark       scripts/benchmark.sh     (redis-benchmark SET/GET)
#
# A failing stage does not stop the run; every stage is attempted and the exit
# code is 0 only if all stages pass.  The output of each stage is also saved to
# results/run-all/<stage>.log.
#
# Usage:
#   ./scripts/run-all.sh            # run everything on the current cluster
#   ./scripts/run-all.sh --clean    # delete containers AND volumes first (from zero)
# ──────────────────────────────────────────────────────────────────────────────
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/results/run-all"
COMPOSE=(docker compose -f "$PROJECT_DIR/docker-compose.yml")
SEP="════════════════════════════════════════════════════════════════════════"

CLEAN=false
for arg in "$@"; do
  case "$arg" in
    --clean)
      CLEAN=true
      ;;
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg"
      echo "Usage: $0 [--clean]"
      exit 1
      ;;
  esac
done

cd "$PROJECT_DIR"
mkdir -p "$LOG_DIR"
rm -f "$LOG_DIR"/*.log

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running (or this user cannot access it). Start Docker first."
  exit 1
fi

STAGE_NAMES=()
STAGE_RESULTS=()
STAGE_TIMES=()
STAGE_NO=0

# ── run_stage <name> <command...> : banner, run, record PASS/FAIL ────────────
run_stage() {
  local name="$1"; shift
  STAGE_NO=$((STAGE_NO + 1))
  echo ""
  echo "$SEP"
  echo "  STAGE $STAGE_NO: $name"
  echo "$SEP"

  local logfile start rc
  logfile="$LOG_DIR/$(printf '%02d' "$STAGE_NO")-${name// /-}.log"
  start=$(date +%s)
  "$@" 2>&1 | tee "$logfile"
  rc=${PIPESTATUS[0]}

  STAGE_NAMES+=("$name")
  STAGE_TIMES+=("$(( $(date +%s) - start ))")
  if [ "$rc" -eq 0 ]; then
    STAGE_RESULTS+=("PASS")
  else
    STAGE_RESULTS+=("FAIL")
    echo ""
    echo "  [FAIL] Stage '$name' exited with code $rc (log: results/run-all/$(basename "$logfile"))"
  fi
}

# ── Stage helpers ────────────────────────────────────────────────────────────
app_exec() { "${COMPOSE[@]}" exec -T app python "$@"; }

wait_for_app() {
  # The app container is started by compose; make sure it is running before exec.
  for _ in $(seq 1 30); do
    [ "$(docker inspect --format '{{.State.Status}}' app 2>/dev/null)" = "running" ] && return 0
    sleep 1
  done
  echo "ERROR: app container is not running"
  return 1
}

stage_up() {
  # Rebuild the app image so edits in app/ are baked in (cached layers make this fast).
  "${COMPOSE[@]}" build app || return 1
  "$SCRIPT_DIR/cluster-up.sh" || return 1
  wait_for_app
}

# ── Optional clean start ─────────────────────────────────────────────────────
if [ "$CLEAN" = "true" ]; then
  echo "$SEP"
  echo "  CLEAN START: removing containers and volumes"
  echo "$SEP"
  "$SCRIPT_DIR/cluster-down.sh" --volumes
fi

TOTAL_START=$(date +%s)

run_stage "cluster up"      stage_up
run_stage "cluster status"  "$SCRIPT_DIR/cluster-status.sh"
run_stage "cache demo"      app_exec demo_cache.py
run_stage "failover demo"   "$SCRIPT_DIR/failover-demo.sh"
run_stage "cache problems"  app_exec cache_problems.py
run_stage "eviction demo"   app_exec eviction_demo.py
run_stage "benchmark"       "$SCRIPT_DIR/benchmark.sh"

# ── Final summary ────────────────────────────────────────────────────────────
FAILED=0
echo ""
echo "$SEP"
echo "  FINAL SUMMARY"
echo "$SEP"
printf "  %-4s %-18s %-8s %s\n" "#" "Stage" "Result" "Time"
printf "  %-4s %-18s %-8s %s\n" "--" "------------------" "------" "------"
for i in "${!STAGE_NAMES[@]}"; do
  printf "  %-4s %-18s %-8s %ss\n" "$((i + 1))" "${STAGE_NAMES[$i]}" "${STAGE_RESULTS[$i]}" "${STAGE_TIMES[$i]}"
  [ "${STAGE_RESULTS[$i]}" = "PASS" ] || FAILED=$((FAILED + 1))
done
echo "  ------------------------------------------------"
if [ "$FAILED" -eq 0 ]; then
  echo "  OVERALL: PASS  (${#STAGE_NAMES[@]}/${#STAGE_NAMES[@]} stages, $(( $(date +%s) - TOTAL_START ))s)"
else
  echo "  OVERALL: FAIL  ($FAILED of ${#STAGE_NAMES[@]} stages failed; logs in results/run-all/)"
fi
echo "$SEP"
echo ""

[ "$FAILED" -eq 0 ]
