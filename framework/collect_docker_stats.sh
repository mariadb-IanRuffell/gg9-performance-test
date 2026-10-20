#!/usr/bin/env bash
# usage: collect_docker_stats.sh <out.csv> <pid-to-follow> <container,container,...>   (1 sample / 5 s)
OUT="$1"; PID="$2"; CONTAINERS="${3//,/ }"
echo "ts,container,cpu_pct,mem_mib,net_in_mb,net_out_mb" > "$OUT"
while kill -0 "$PID" 2>/dev/null; do
  TS=$(date +%s)
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.NetIO}}' $CONTAINERS 2>/dev/null | python3 -c '
import sys,re
ts=sys.argv[1]
def mb(s):
    m=re.match(r"([\d.]+)\s*([kKMGT]?i?B)",s.strip())
    if not m: return 0
    v=float(m.group(1)); u=m.group(2).upper()
    return v*{"B":1e-6,"KB":1e-3,"KIB":1.024e-3,"MB":1,"MIB":1.048576,"GB":1e3,"GIB":1073.7,"TB":1e6}.get(u,1)
for l in sys.stdin:
    n,c,m,net=l.strip().split(",",3)
    used=m.split("/")[0]; i,o=net.split("/")
    print(",".join([ts,n,c.rstrip("%"),"%.1f"%(mb(used)/1.048576),"%.2f"%mb(i),"%.2f"%mb(o)]))
' "$TS" >> "$OUT"
  sleep 3
done
