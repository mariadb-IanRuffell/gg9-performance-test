#!/usr/bin/env bash
# Start the local 3-node cluster and initialise it. License: GG_LICENSE_FILE (default ./gridgain-license.json).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/config/versions.env" ] && set -a && . "$ROOT/config/versions.env" && set +a
[ -f "$ROOT/config/local.env" ] && set -a && . "$ROOT/config/local.env" && set +a
LICENSE_FILE="${GG_LICENSE_FILE:-$ROOT/gridgain-license.json}"
REST="http://localhost:10300"

docker compose -f "$ROOT/cluster/local/docker-compose.yml" up -d
echo -n ">> waiting for REST"
until curl -sf "$REST/management/v1/cluster/state" >/dev/null 2>&1 || curl -s -o /dev/null -w '%{http_code}' "$REST/management/v1/cluster/state" | grep -qE '^(200|404|409)$'; do echo -n .; sleep 2; done; echo

if curl -sf "$REST/management/v1/cluster/state" >/dev/null 2>&1; then
  echo ">> cluster already initialised"; exit 0
fi

BODY='{"metaStorageNodes":["node1","node2","node3"],"cmgNodes":["node1","node2","node3"],"clusterName":"perf-cluster"'
if [ -f "$LICENSE_FILE" ]; then
  BODY="$BODY,\"license\":$(python3 -c 'import json,sys;print(json.dumps(open(sys.argv[1]).read()))' "$LICENSE_FILE")"
else
  echo "!! no license file at $LICENSE_FILE - initialising without one"
fi
curl -sf -X POST "$REST/management/v1/cluster/init" -H 'Content-Type: application/json' -d "$BODY}" && echo ">> init sent"
until curl -sf "$REST/management/v1/cluster/state" >/dev/null 2>&1; do sleep 2; done
for port in 10300 10301 10302; do
  until curl -s "http://localhost:$port/management/v1/node/state" | grep -q STARTED; do sleep 2; done
done
echo ">> cluster ready (GridGain $GG_VERSION): $(curl -s $REST/management/v1/cluster/state)"
