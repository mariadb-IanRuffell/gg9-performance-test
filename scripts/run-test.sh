#!/usr/bin/env bash
# usage: scripts/run-test.sh <profile> [env]     e.g. scripts/run-test.sh oltp-steady local
# Runs one measured test, then builds the report into results/<run>/report.html
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE_NAME="${1:?profile name (see profiles/)}"; ENV_NAME="${2:-local}"
JMETER_VERSION="${JMETER_VERSION:-5.6.3}"
JMETER="$ROOT/tools/apache-jmeter-$JMETER_VERSION/bin/jmeter"
PROFILE="$ROOT/profiles/$PROFILE_NAME.properties"; ENVF="$ROOT/config/env/$ENV_NAME.properties"
[ -f "$PROFILE" ] && [ -f "$ENVF" ] || { echo "missing profile or env file"; exit 1; }
prop() { grep -E "^$1=" "$2" | tail -1 | cut -d= -f2-; }
WORKLOAD=$(prop workload "$PROFILE"); MODE=$(prop mode "$PROFILE")

RUN_ID="$(date +%Y%m%d-%H%M%S)-$PROFILE_NAME-$ENV_NAME"
OUT="$ROOT/results/$RUN_ID"; mkdir -p "$OUT"
cp "$PROFILE" "$OUT/profile.properties"; cp "$ENVF" "$OUT/env.properties"
SALT=$(( $(date +%s) - 1735689600 ))
export JVM_ARGS="${JVM_ARGS:--Xms1g -Xmx${JMETER_HEAP:-4g} -XX:+UseG1GC}"

echo ">> $RUN_ID  workload=$WORKLOAD mode=$MODE"
"$JMETER" -n -t "$ROOT/plans/run-$WORKLOAD.jmx" \
  -q "$ROOT/config/jmeter.properties" -q "$ROOT/config/defaults.properties" -q "$ENVF" -q "$PROFILE" \
  -Jrun_salt="$SALT" -l "$OUT/results.jtl" -j "$OUT/jmeter.log" &
JPID=$!
START=$(date +%s)

BG=()
if [ "$(prop collector "$ENVF")" = "docker" ]; then
  "$ROOT/framework/collect_docker_stats.sh" "$OUT/resources.csv" "$JPID" "$(prop collector_containers "$ENVF")" & BG+=($!)
fi
if [ "$MODE" = "ramp" ]; then
  python3 "$ROOT/framework/breakpoint_watch.py" "$OUT/results.jtl" "$OUT/profile.properties" "$JPID" "$OUT" & BG+=($!)
fi
if grep -q "^chaos_events=" "$PROFILE"; then
  python3 "$ROOT/framework/chaos.py" "$OUT" "$OUT/profile.properties" "$OUT/env.properties" "$JPID" 2>&1 | tee "$OUT/chaos.log" & BG+=($!)
fi
trap 'kill $JPID 2>/dev/null || true' INT TERM
wait $JPID || echo "!! jmeter exited non-zero"
END=$(date +%s)
for b in "${BG[@]:-}"; do [ -n "$b" ] && wait "$b" 2>/dev/null || true; done

python3 - "$OUT" "$START" "$END" <<'PY'
import json,sys,platform
out,s,e=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
json.dump({"start_epoch":s,"end_epoch":e,"host":platform.node(),"loadgen":platform.platform()},open(out+"/run.json","w"))
PY
python3 "$ROOT/framework/report.py" "$OUT"
echo ">> report: $OUT/report.html"
