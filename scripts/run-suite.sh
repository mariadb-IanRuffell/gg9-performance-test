#!/usr/bin/env bash
# usage: scripts/run-suite.sh [profile ...]        (default: every non-smoke profile)
# For EVERY test: destroy the cluster, create a fresh one, load data, run the test, build the report.
# Ramp tests must end in a breakpoint (failures); if the thread ceiling is hit first, the test is rerun with
# threads x2 and rampup x2 (fresh cluster each time) up to MAX_ESCALATIONS times.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_NAME="${SUITE_ENV:-local}"; MAX_ESCALATIONS="${MAX_ESCALATIONS:-3}"; MAX_THREADS="${MAX_THREADS:-8000}"
[ -f "$ROOT/config/versions.env" ] && . "$ROOT/config/versions.env"
export GG_VERSION JMETER_HEAP="${JMETER_HEAP:-8g}"
DEFAULT=(oltp-steady analytical-steady ingest-steady oltp-ramp analytical-ramp ingest-ramp
         chaos-oltp-node-stop chaos-oltp-node-kill chaos-oltp-node-kill-timeouts chaos-oltp-node-pause
         chaos-oltp-rolling-restart chaos-oltp-quorum-loss chaos-ingest-node-kill)
PROFILES=("$@"); [ ${#PROFILES[@]} -eq 0 ] && PROFILES=("${DEFAULT[@]}")
SUITE="$ROOT/results/suite-$(date +%Y%m%d-%H%M%S)-gg$GG_VERSION"; mkdir -p "$SUITE"
TSV="$SUITE/summary.tsv"; printf 'profile\tattempt\tstatus\trun_dir\tnote\n' > "$TSV"
prop() { grep -E "^$1=" "$2" | tail -1 | cut -d= -f2-; }
log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$SUITE/suite.log"; }

fresh_cluster() {   # $1 = workload, $2 = profile (sizing)
  "$ROOT/scripts/cluster-down.sh" >/dev/null 2>&1
  "$ROOT/scripts/cluster-up.sh" >>"$SUITE/suite.log" 2>&1 || return 1
  "$ROOT/scripts/prepare-data.sh" "$1" "$ENV_NAME" "$2" >>"$SUITE/suite.log" 2>&1 || return 1
}

run_one() {   # $1 profile, $2 attempt, rest = overrides
  local prof="$1" attempt="$2"; shift 2
  local wl; wl=$(prop workload "$ROOT/profiles/$prof.properties")
  log "== $prof (attempt $attempt) $* : fresh cluster + data ($wl)"
  if ! fresh_cluster "$wl" "$prof"; then printf '%s\t%s\tSETUP_FAILED\t-\tcluster/data setup failed\n' "$prof" "$attempt" >> "$TSV"; return 1; fi
  "$ROOT/scripts/run-test.sh" "$prof" "$ENV_NAME" "$@" >"$SUITE/$prof.attempt$attempt.log" 2>&1
  local run; run=$(cat "$ROOT/results/.last_run" 2>/dev/null)
  if [ ! -f "$run/summary.json" ]; then printf '%s\t%s\tRUN_FAILED\t%s\tno summary\n' "$prof" "$attempt" "$run" >> "$TSV"; return 1; fi
  LAST_RUN="$run"
  return 0
}

for prof in "${PROFILES[@]}"; do
  mode=$(prop mode "$ROOT/profiles/$prof.properties")
  threads=$(prop threads "$ROOT/profiles/$prof.properties"); ramp=$(prop rampup "$ROOT/profiles/$prof.properties"); dur=$(prop duration "$ROOT/profiles/$prof.properties")
  attempt=1; overrides=()
  while :; do
    LAST_RUN=""
    if ! run_one "$prof" "$attempt" "${overrides[@]:-}"; then
      [ $attempt -ge 2 ] && { log "!! $prof failed twice, moving on"; break; }
      attempt=$((attempt+1)); continue          # one retry on a fresh cluster
    fi
    if [ "$mode" = "ramp" ]; then
      if [ -f "$LAST_RUN/breakpoint.json" ]; then
        note=$(python3 -c "import json;b=json.load(open('$LAST_RUN/breakpoint.json'));print('breakpoint:',b['reason'],'at',b['threads_at_breach'],'threads')")
        printf '%s\t%s\tOK\t%s\t%s\n' "$prof" "$attempt" "$LAST_RUN" "$note" >> "$TSV"; log "   $note"; break
      fi
      if [ $attempt -gt "$MAX_ESCALATIONS" ] || [ $((threads*2)) -gt "$MAX_THREADS" ]; then
        printf '%s\t%s\tNO_BREAKPOINT\t%s\tceiling %s threads reached without failures\n' "$prof" "$attempt" "$LAST_RUN" "$threads" >> "$TSV"
        log "!! $prof: no failures up to $threads threads"; break
      fi
      threads=$((threads*2)); ramp=$((ramp*2)); dur=$((ramp+60)); attempt=$((attempt+1))
      overrides=("threads=$threads" "rampup=$ramp" "duration=$dur")
      printf '%s\t%s\tNO_BREAKPOINT\t%s\tescalating to %s threads\n' "$prof" "$((attempt-1))" "$LAST_RUN" "$threads" >> "$TSV"
      log "   no failures yet -> escalating to $threads threads / ${ramp}s ramp"
    else
      printf '%s\t%s\tOK\t%s\t\n' "$prof" "$attempt" "$LAST_RUN" >> "$TSV"; break
    fi
  done
done
"$ROOT/scripts/cluster-down.sh" >/dev/null 2>&1
python3 "$ROOT/framework/suite_index.py" "$SUITE" >>"$SUITE/suite.log" 2>&1
log "suite finished: $SUITE/index.html"
