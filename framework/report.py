#!/usr/bin/env python3
"""Builds a self-contained HTML report (+ summary.json) from a run directory.

usage: report.py results/<run>

Inputs : results.jtl (required), profile.properties, env.properties, run.json,
         breakpoint.json (ramp mode), resources.csv (local docker collector)
Outputs: report.html, summary.json
Latency percentiles come from log-scale histograms (2% bin width) so multi-hour runs stay O(buckets).
"""
import csv, json, math, os, sys, collections, datetime, html
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from props import read_props

GROWTH = math.log(1.02)


def bin_of(ms):
    return int(math.log(max(ms, 1)) / GROWTH)


def val_of(b):
    return math.exp((b + 0.5) * GROWTH)


def hist_pct(h, q):
    n = sum(h.values())
    if not n:
        return None
    target, acc = q * n, 0
    for b in sorted(h):
        acc += h[b]
        if acc >= target:
            return round(val_of(b), 1)
    return round(val_of(max(h)), 1)


class Agg:
    __slots__ = ("n", "err", "sum", "max", "hist", "bytes")

    def __init__(self):
        self.n = self.err = self.sum = self.max = self.bytes = 0
        self.hist = collections.Counter()

    def add(self, e, ok, nbytes):
        self.n += 1
        self.err += 0 if ok else 1
        self.sum += e
        self.bytes += nbytes
        if e > self.max:
            self.max = e
        self.hist[bin_of(e)] += 1

    def merge(self, o):
        self.n += o.n; self.err += o.err; self.sum += o.sum; self.bytes += o.bytes
        self.max = max(self.max, o.max); self.hist.update(o.hist)
        return self

    def stats(self, secs):
        return dict(samples=self.n, errors=self.err,
                    error_pct=round(100.0 * self.err / self.n, 3) if self.n else 0,
                    tps=round(self.n / secs, 2) if secs > 0 else 0,
                    avg_ms=round(self.sum / self.n, 1) if self.n else None,
                    p50_ms=hist_pct(self.hist, .50), p90_ms=hist_pct(self.hist, .90),
                    p95_ms=hist_pct(self.hist, .95), p99_ms=hist_pct(self.hist, .99), max_ms=self.max)


def load(run):
    per_sec = collections.defaultdict(lambda: collections.defaultdict(Agg))  # sec -> label -> Agg
    threads = collections.defaultdict(int)
    errors = collections.Counter()
    t0 = None
    path = os.path.join(run, "results.jtl")
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        rows = []
        for r in rd:
            try:
                if r["label"].startswith("teardown."):
                    continue
                rows.append((int(r["timeStamp"]), r["label"], int(r["elapsed"]), r["success"] == "true",
                             int(r.get("allThreads") or 0), int(r.get("bytes") or 0),
                             r.get("responseMessage", ""), r.get("failureMessage", "")))
            except (ValueError, KeyError):
                continue  # truncated final line
    if not rows:
        raise SystemExit("no samples in results.jtl")
    t0 = min(r[0] for r in rows)
    for ts, label, e, ok, th, nb, msg, fmsg in rows:
        s = (ts - t0) // 1000
        per_sec[s][label].add(e, ok, nb)
        if th > threads[s]:
            threads[s] = th
        if not ok:
            errors[(label, (fmsg or msg or "unknown")[:160])] += 1
    return per_sec, threads, errors, t0


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def chaos_analysis(run, per_sec, dur, t0_ms, warm=0):
    """Phase stats (baseline / disruption / recovery) and availability metrics around injected faults."""
    path = os.path.join(run, "chaos.json")
    if not os.path.exists(path):
        return None
    raw = json.load(open(path))["events"]
    t0 = t0_ms / 1000.0
    ev = [dict(e, t=round(e["t_epoch"] - t0, 1)) for e in raw]
    unplanned = [e for e in ev if e["action"] == "unplanned-exit"]
    bad = [e for e in ev if e["action"] in ("stop", "kill", "pause") and e["ok"]]
    good = [e for e in ev if e["action"] in ("start", "unpause") and e["ok"]]
    ready = [e for e in ev if e["action"] == "ready" and e["ok"]]
    if not bad:
        return dict(events=ev, phases=[], unplanned=unplanned, note="no disruptive event succeeded")
    first_bad = int(min(e["t"] for e in bad))
    last_back = int(max([e["t"] for e in ready] or [e["t"] for e in good] or [dur]))

    def window(a, b):
        agg = Agg()
        for sec in range(max(0, a), min(dur, b)):
            for x in per_sec.get(sec, {}).values():
                agg.merge(x)
        return agg, max(1, min(dur, b) - max(0, a))

    start = warm if warm < first_bad - 20 else first_bad // 2  # skip warm-up in the baseline
    phases = []
    for name, a, b in (("baseline (before first fault)", start, first_bad),
                       ("disruption (fault -> node ready)", first_bad, last_back),
                       ("after recovery", last_back, dur)):
        agg, secs = window(a, b)
        if agg.n:
            phases.append(dict(phase=name, from_s=a, to_s=min(b, dur), **agg.stats(secs)))
    base = phases[0] if phases and phases[0]["phase"].startswith("baseline") else None
    # availability: seconds in the disruption window with no successful sample
    dead, longest, cur = 0, 0, 0
    for sec in range(first_bad, min(dur, last_back)):
        ok = sum(a.n - a.err for a in per_sec.get(sec, {}).values())
        if ok == 0:
            dead += 1; cur += 1; longest = max(longest, cur)
        else:
            cur = 0
    # time to recover: first sec after last_back where 5 consecutive 5s windows hold >=90% of baseline tps, err <= 1%
    recover = None
    if base:
        need = 0.9 * base["tps"]

        def healthy(sec):
            agg, secs = window(sec, sec + 5)
            st = agg.stats(secs)
            return agg.n > 0 and st["tps"] >= need and st["error_pct"] <= 1.0
        # recovered = every 5s window from some point to the end of the run is healthy; report when that point was.
        flags = [healthy(sec) for sec in range(last_back, dur - 4, 5)]
        if flags and flags[-1]:
            last_bad = max([i for i, f in enumerate(flags) if not f], default=-1)
            recover = (last_bad + 1) * 5
        # else: still unhealthy at the end of the run -> None ("not recovered")
    return dict(events=ev, phases=phases, unplanned=unplanned, first_fault_s=first_bad, last_ready_s=last_back,
                zero_success_seconds=dead, longest_outage_s=longest, recovery_time_s=recover,
                rejoin_s=[dict(node=e["node"], note=e["note"]) for e in ready])


def probe_analysis(run, t0_ms, dur, B, nb):
    """Independent probe (fresh connection per probe, hard timeouts) -> per-target availability and gaps."""
    rows = read_csv(os.path.join(run, "probe.csv"))
    if not rows:
        return None
    t0 = t0_ms / 1000.0
    by = collections.defaultdict(list)  # (target, op) -> [(t, ok, ms, err)]
    for r in rows:
        try:
            by[(r["target"], r["op"])].append((int(r["ts_ms"]) / 1000.0 - t0, r["ok"] == "true", int(r["latency_ms"]), r["error"]))
        except (ValueError, KeyError):
            pass
    targets = sorted({k[0] for k in by})
    table, avail, maxlat = [], {}, {}
    for tg in targets:
        a, m = [[0, 0] for _ in range(nb)], [None] * nb
        for op in ("read", "write"):
            pts = sorted(by.get((tg, op), []))
            if not pts:
                continue
            oks = [t for t, ok, _, _ in pts if ok]
            edges = [0.0] + oks + [float(dur)]
            gap = max(b - a_ for a_, b in zip(edges, edges[1:]))
            errs = collections.Counter(e[:90] for _, ok, _, e in pts if not ok)
            lat = sorted(ms for _, ok, ms, _ in pts if ok)
            table.append(dict(target=tg, op=op, probes=len(pts), ok_pct=round(100.0 * len(oks) / len(pts), 2),
                              longest_gap_s=round(gap, 1), p50_ms=lat[len(lat) // 2] if lat else None,
                              p99_ms=lat[min(len(lat) - 1, int(len(lat) * .99))] if lat else None,
                              top_error=errs.most_common(1)[0][0] if errs else ""))
            for t, ok, ms, _ in pts:
                i = min(nb - 1, max(0, int(t // B)))
                a[i][0] += 1 if ok else 0
                a[i][1] += 1
                m[i] = max(m[i] or 0, ms)
        avail[tg] = [round(100.0 * x / y, 1) if y else None for x, y in a]
        maxlat[tg] = m
    return dict(table=table, avail=avail, maxlat=maxlat)


def build(run):
    prof = read_props(os.path.join(run, "profile.properties")) if os.path.exists(os.path.join(run, "profile.properties")) else {}
    env = read_props(os.path.join(run, "env.properties")) if os.path.exists(os.path.join(run, "env.properties")) else {}
    meta = json.load(open(os.path.join(run, "run.json"))) if os.path.exists(os.path.join(run, "run.json")) else {}
    bp = json.load(open(os.path.join(run, "breakpoint.json"))) if os.path.exists(os.path.join(run, "breakpoint.json")) else None
    per_sec, threads, errors, t0 = load(run)
    dur = max(per_sec) + 1
    mode = prof.get("mode", "steady")
    warm = int(float(prof.get("warmup_sec", prof.get("rampup", 0)))) if mode == "steady" else 0
    warm = min(warm, max(0, dur - 10))
    labels = sorted({l for s in per_sec.values() for l in s})

    # ---- overall / steady-window per-label stats
    steady, overall = collections.defaultdict(Agg), collections.defaultdict(Agg)
    for s, d in per_sec.items():
        for l, a in d.items():
            overall[l].merge(a)
            if s >= warm:
                steady[l].merge(a)
    steady_secs = dur - warm
    all_steady, all_overall = Agg(), Agg()
    for a in steady.values():
        all_steady.merge(a)
    for a in overall.values():
        all_overall.merge(a)
    label_rows = {l: steady[l].stats(steady_secs) for l in labels if steady[l].n}

    # ---- time series, merged into <= ~240 display buckets
    B = max(1, math.ceil(dur / 240))
    series = collections.defaultdict(list)
    xs = []
    per_label_p95 = {l: [] for l in labels}
    for b0 in range(0, dur, B):
        tot, lab = Agg(), collections.defaultdict(Agg)
        th = 0
        for s in range(b0, min(dur, b0 + B)):
            th = max(th, threads.get(s, 0))
            for l, a in per_sec.get(s, {}).items():
                tot.merge(a); lab[l].merge(a)
        span = min(B, dur - b0)
        xs.append(b0 + span / 2.0)
        st = tot.stats(span)
        series["tps"].append(st["tps"]); series["err_pct"].append(st["error_pct"])
        for k in ("p50_ms", "p95_ms", "p99_ms"):
            series[k].append(st[k])
        series["threads"].append(th)
        for l in labels:
            per_label_p95[l].append(hist_pct(lab[l].hist, .95) if lab[l].n else None)

    # ---- saturation curve (ramp): metrics grouped by active-thread level
    sat = []
    if mode == "ramp":
        levels = collections.defaultdict(lambda: [Agg(), 0])
        step = max(1, int(max(threads.values()) / 40) or 1)
        for s, d in per_sec.items():
            key = (threads.get(s, 0) // step) * step
            for a in d.values():
                levels[key][0].merge(a)
            levels[key][1] += 1
        for k in sorted(levels):
            a, secs = levels[k]
            if secs and a.n:
                st = a.stats(secs)
                sat.append(dict(threads=k, tps=st["tps"], p95_ms=st["p95_ms"], err_pct=st["error_pct"]))

    # ---- resources
    res = read_csv(os.path.join(run, "resources.csv"))
    resources = {}
    if res:
        for r in res:
            try:
                t = int(r["ts"]) - (meta.get("start_epoch") or t0 // 1000)
                resources.setdefault(r["container"], []).append((t, float(r["cpu_pct"]), float(r["mem_mib"])))
            except (ValueError, KeyError):
                pass

    chaos = chaos_analysis(run, per_sec, dur, t0, warm)
    probe = probe_analysis(run, t0, dur, B, len(xs))
    top_err = [dict(label=l, message=m, count=c) for (l, m), c in errors.most_common(15)]
    peak = max(series["tps"]) if series["tps"] else 0
    peak_at = series["threads"][series["tps"].index(peak)] if peak else 0
    ts_all = sorted({t for v in resources.values() for t, _, _ in v})
    aligned = {"x": ts_all}
    for k, v in resources.items():
        m = {t: (c, mem) for t, c, mem in v}
        aligned[k] = [[m[t][0] if t in m else None, m[t][1] if t in m else None] for t in ts_all]
    summary = dict(
        run=os.path.basename(os.path.abspath(run)), workload=prof.get("workload"), mode=mode,
        env=env.get("env_name"), duration_s=dur, steady_window_s=steady_secs,
        overall=all_overall.stats(dur), steady=all_steady.stats(steady_secs),
        peak_tps=peak, threads_at_peak_tps=peak_at, breakpoint=bp, labels=label_rows,
        top_errors=top_err, chaos=chaos, probe=probe["table"] if probe else None)
    if "ingest.batch" in label_rows:
        summary["ingest_rows_per_sec"] = round(label_rows["ingest.batch"]["tps"] * int(prof.get("ingest_batch", 500)), 1)
    data = dict(summary=summary, profile=prof, env=env, meta=meta, bucket_s=B, x=xs, series=series,
                label_p95=per_label_p95, labels=labels, saturation=sat,
                resources=aligned, chaos=chaos, probe=probe)
    return summary, data


# --------------------------------------------------------------------------- HTML
CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--panel:#ffffff;--ink:#0b0b0b;--ink2:#52514e;--muted:#6b6a66;--line:#e4e3df;
--grid:#ecebe7;--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#6250d6;--s8:#e34948;
--good:#0a7a3b;--bad:#b3261e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--bg:#1a1a19;--panel:#222221;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#9a998f;--line:#3a3a37;--grid:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;
--s6:#008300;--s7:#9085e9;--s8:#e66767;--good:#4cc27f;--bad:#f2766d}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--panel:#222221;--ink:#f0efec;--ink2:#c3c2b7;--muted:#9a998f;
--line:#3a3a37;--grid:#2c2c2a;--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;
--s8:#e66767;--good:#4cc27f;--bad:#f2766d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:32px 0 12px}
.sub{color:var(--ink2);margin:0 0 16px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.kpi b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.kpi span{color:var(--muted);font-size:12px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px;margin-bottom:16px}
.card h3{margin:0 0 2px;font-size:14px}.card p{margin:0 0 8px;color:var(--muted);font-size:12px}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:4px 0;font-size:12px;color:var(--ink2)}
.legend i{display:inline-block;width:14px;height:0;border-top:2px solid;margin-right:6px;vertical-align:middle}
.chart{position:relative}svg{display:block;width:100%}.tip{position:absolute;pointer-events:none;background:var(--panel);
border:1px solid var(--line);border-radius:6px;padding:6px 8px;font-size:12px;display:none;white-space:nowrap;
box-shadow:0 2px 8px rgba(0,0,0,.15);z-index:2}
table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}.scroll{overflow-x:auto}
.banner{border-left:4px solid;padding:10px 14px;border-radius:4px;background:var(--panel);border-color:var(--line);margin:12px 0}
.banner.bad{border-color:var(--bad)}.banner.good{border-color:var(--good)}.banner strong{display:inline-block;margin-right:6px}
details{margin-top:8px;color:var(--ink2)}details summary{cursor:pointer;font-size:12px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:2px 16px;font-size:13px}.kv dt{color:var(--muted)}.kv dd{margin:0}
text{fill:var(--muted);font-size:11px}.gridline{stroke:var(--grid)}.axis{stroke:var(--line)}
"""

JS = r"""
const D=JSON.parse(document.getElementById('data').textContent);
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt=v=>v==null?'–':(Math.abs(v)>=1000?v.toLocaleString(undefined,{maximumFractionDigits:0}):(+v.toFixed(2)).toString());
function nice(max){if(max<=0)return 1;const p=Math.pow(10,Math.floor(Math.log10(max)));const n=max/p;return(n<=1?1:n<=2?2:n<=5?5:10)*p}
const charts=[];
function chart(id,spec){charts.push([id,spec]);draw(id,spec)}
function draw(id,spec){
 const host=document.getElementById(id);host.innerHTML='';
 const W=host.clientWidth||800,H=250,m={l:54,r:28,t:10,b:30};
 const xs=spec.x,all=spec.series.flatMap(s=>s.values.filter(v=>v!=null));
 if(!xs.length||!all.length){host.textContent='no data';return}
 const x0=Math.min(...xs),x1=Math.max(...xs)||1,ymax=nice(Math.max(...all)*1.05);
 const X=v=>m.l+(v-x0)/((x1-x0)||1)*(W-m.l-m.r),Y=v=>H-m.b-v/ymax*(H-m.t-m.b);
 let g='';
 for(let i=0;i<=4;i++){const v=ymax*i/4,y=Y(v);g+=`<line class="gridline" x1="${m.l}" x2="${W-m.r}" y1="${y}" y2="${y}"/><text x="${m.l-6}" y="${y+4}" text-anchor="end">${fmt(v)}</text>`}
 for(let i=0;i<=5;i++){const v=x0+(x1-x0)*i/5;g+=`<text x="${X(v)}" y="${H-10}" text-anchor="middle">${spec.xfmt?spec.xfmt(v):fmt(v)}</text>`}
 g+=`<line class="axis" x1="${m.l}" x2="${W-m.r}" y1="${H-m.b}" y2="${H-m.b}"/>`;
 spec.series.forEach((s,k)=>{let d='',pen=false;s.values.forEach((v,i)=>{if(v==null){pen=false;return}d+=(pen?'L':'M')+X(xs[i]).toFixed(1)+' '+Y(v).toFixed(1);pen=true});
  g+=`<path d="${d}" fill="none" stroke="${css('--s'+(k+1))}" stroke-width="2" stroke-linejoin="round"/>`});
 (spec.marks||[]).forEach((mk,i)=>{if(mk.t<x0||mk.t>x1)return;const mx=X(mk.t),c=mk.kind==='bad'?css('--s8'):mk.kind==='good'?css('--s3'):css('--muted');
  g+=`<line x1="${mx}" x2="${mx}" y1="${m.t}" y2="${H-m.b}" stroke="${c}" stroke-width="1.5" stroke-dasharray="4 3"/><circle cx="${mx}" cy="${m.t+7}" r="8" fill="${c}"/><text x="${mx}" y="${m.t+11}" text-anchor="middle" style="fill:#fff;font-weight:700">${mk.n}</text>`});
 g+=`<line id="${id}-x" y1="${m.t}" y2="${H-m.b}" stroke="${css('--muted')}" stroke-dasharray="3 3" style="display:none"/>`;
 host.insertAdjacentHTML('beforeend',`<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${spec.title}">${g}</svg><div class="tip"></div>`);
 const svg=host.querySelector('svg'),tip=host.querySelector('.tip'),vl=svg.querySelector('line[id$=-x]');
 svg.addEventListener('mousemove',e=>{const r=svg.getBoundingClientRect(),px=(e.clientX-r.left)*W/r.width;
  const xv=x0+(px-m.l)/(W-m.l-m.r)*(x1-x0);let bi=0,bd=1e18;xs.forEach((v,i)=>{const dd=Math.abs(v-xv);if(dd<bd){bd=dd;bi=i}});
  vl.setAttribute('x1',X(xs[bi]));vl.setAttribute('x2',X(xs[bi]));vl.style.display='';
  tip.innerHTML=`<b>${spec.xlabel} ${spec.xfmt?spec.xfmt(xs[bi]):fmt(xs[bi])}</b><br>`+spec.series.map((s,k)=>`<span style="color:${css('--s'+(k+1))}">━</span> ${s.name}: ${fmt(s.values[bi])}${spec.unit||''}`).join('<br>');
  tip.style.display='block';tip.style.left=Math.min(r.width-170,Math.max(0,X(xs[bi])*r.width/W+12))+'px';tip.style.top='8px'});
 svg.addEventListener('mouseleave',()=>{tip.style.display='none';vl.style.display='none'});
}
const CH=(D.chaos&&D.chaos.events||[]).filter(e=>e.action!=='ready-timeout'||true).map((e,i)=>({t:e.t,n:i+1,kind:['stop','kill','pause','unplanned-exit'].includes(e.action)?'bad':(e.action==='ready'||e.action==='start'||e.action==='unpause')?'good':'x',label:e.action+' '+e.node}));
function card(parent,id,spec,note){
 if(spec.xfmt===tfmt&&CH.length)spec.marks=CH;
 const el=document.createElement('section');el.className='card';
 const leg=spec.series.length>1?`<div class="legend">${spec.series.map((s,k)=>`<span><i style="border-color:${css('--s'+(k+1))}"></i>${s.name}</span>`).join('')}</div>`:'';
 const rows=spec.x.map((x,i)=>`<tr><td>${fmt(x)}</td>${spec.series.map(s=>`<td>${fmt(s.values[i])}</td>`).join('')}</tr>`).join('');
 el.innerHTML=`<h3>${spec.title}</h3><p>${note||''}</p>${leg}<div class="chart" id="${id}"></div>
 <details><summary>Data table</summary><div class="scroll" style="max-height:240px;overflow:auto"><table><tr><th>${spec.xlabel}</th>${spec.series.map(s=>`<th>${s.name}</th>`).join('')}</tr>${rows}</table></div></details>`;
 document.getElementById(parent).appendChild(el);chart(id,spec)}
const tfmt=v=>{v=Math.round(v);const m=Math.floor(v/60);return m+'m'+String(v%60).padStart(2,'0')+'s'};
const S=D.series,X=D.x;
card('charts','c-tps',{title:'Throughput',xlabel:'t',xfmt:tfmt,unit:' ops/s',x:X,series:[{name:'ops/s (all operations)',values:S.tps}]},'Completed samples per second, '+D.bucket_s+'s buckets.');
card('charts','c-lat',{title:'Latency percentiles',xlabel:'t',xfmt:tfmt,unit:' ms',x:X,series:[{name:'p50',values:S.p50_ms},{name:'p95',values:S.p95_ms},{name:'p99',values:S.p99_ms}]},'Client-observed response time per bucket (all operations).');
card('charts','c-err',{title:'Error rate',xlabel:'t',xfmt:tfmt,unit:'%',x:X,series:[{name:'errors %',values:S.err_pct}]},'Failed samples as % of samples.');
card('charts','c-thr',{title:'Active virtual users',xlabel:'t',xfmt:tfmt,x:X,series:[{name:'threads',values:S.threads}]},'Concurrent JMeter threads.');
if(D.labels.length>1)card('charts','c-lab',{title:'p95 latency by operation',xlabel:'t',xfmt:tfmt,unit:' ms',x:X,series:D.labels.slice(0,8).map(l=>({name:l,values:D.label_p95[l]}))},'Which operation degrades first.');
if(D.saturation.length){const sx=D.saturation.map(r=>r.threads);
 card('charts','c-sat',{title:'Throughput vs concurrency',xlabel:'threads',x:sx,unit:' ops/s',series:[{name:'ops/s',values:D.saturation.map(r=>r.tps)}]},'Saturation curve: the knee is where more users stop adding throughput.');
 card('charts','c-satl',{title:'p95 latency vs concurrency',xlabel:'threads',x:sx,unit:' ms',series:[{name:'p95',values:D.saturation.map(r=>r.p95_ms)}]},'Latency growth as concurrency rises.')}
if(D.probe){const pk=Object.keys(D.probe.avail);
 card('charts','c-pav',{title:'Probe: availability by endpoint',xlabel:'t',xfmt:tfmt,unit:'%',x:X,series:pk.map(k=>({name:k,values:D.probe.avail[k]}))},'Independent client, fresh connection per probe, hard timeout. Gaps = probes still hanging (no result recorded yet).');
 card('charts','c-pla',{title:'Probe: worst latency by endpoint',xlabel:'t',xfmt:tfmt,unit:' ms',x:X,series:pk.map(k=>({name:k,values:D.probe.maxlat[k]}))},'Max probe latency per bucket (a timeout shows as the timeout value).')}
const rk=Object.keys(D.resources).filter(k=>k!=='x');
if(rk.length){const t=D.resources.x;
 card('charts','c-cpu',{title:'Node CPU',xlabel:'t',xfmt:tfmt,unit:'%',x:t,series:rk.slice(0,8).map(k=>({name:k,values:D.resources[k].map(r=>r[0])}))},'docker stats, % of one core (200% = 2 cores). Gaps = node down.');
 card('charts','c-mem',{title:'Node memory',xlabel:'t',xfmt:tfmt,unit:' MiB',x:t,series:rk.slice(0,8).map(k=>({name:k,values:D.resources[k].map(r=>r[1])}))},'Resident container memory.')}
let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>charts.forEach(([i,s])=>draw(i,s)),120)});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>charts.forEach(([i,s])=>draw(i,s)));
"""


def esc(x):
    return html.escape(str(x))


def render(summary, data):
    s, st = summary, summary["steady"]
    prof = data["profile"]
    kp = [(f'{st["tps"]:,.0f}', "avg ops/s (steady window)"), (f'{summary["peak_tps"]:,.0f}', "peak ops/s (bucket)"),
          (f'{st["error_pct"]}%', "error rate"), (f'{st["p50_ms"]} ms', "p50 latency"),
          (f'{st["p95_ms"]} ms', "p95 latency"), (f'{st["p99_ms"]} ms', "p99 latency"),
          (f'{st["samples"]:,}', "samples")]
    if "ingest_rows_per_sec" in s:
        kp.append((f'{s["ingest_rows_per_sec"]:,.0f}', "rows/s ingested (batches)"))
    kpis = "".join(f'<div class="kpi"><b>{esc(a)}</b><span>{esc(b)}</span></div>' for a, b in kp)

    banner = ""
    bp = s.get("breakpoint")
    if s["mode"] == "ramp":
        if bp:
            lg = bp.get("last_good") or {}
            banner = (f'<div class="banner bad"><strong>✕ Breakpoint reached</strong> {esc(bp["reason"])} — detected at '
                      f'{esc(bp["threads_at_breach"])} threads after {esc(bp["detected_at_s"])}s. '
                      f'Last healthy window: {esc(lg.get("threads", "?"))} threads, {esc(lg.get("tps", "?"))} ops/s, '
                      f'p95 {esc(lg.get("p95_ms", "?"))} ms. Peak throughput {s["peak_tps"]:,.0f} ops/s at ~{s["threads_at_peak_tps"]} threads.</div>')
        else:
            banner = ('<div class="banner good"><strong>✓ No breakpoint</strong> SLO held up to the thread ceiling — '
                      'raise <code>threads</code> or relax the ramp to find the limit.</div>')
    lab_rows = "".join(
        f'<tr><td>{esc(l)}</td><td>{r["samples"]:,}</td><td>{r["tps"]}</td><td>{r["error_pct"]}%</td><td>{r["avg_ms"]}</td>'
        f'<td>{r["p50_ms"]}</td><td>{r["p90_ms"]}</td><td>{r["p95_ms"]}</td><td>{r["p99_ms"]}</td><td>{r["max_ms"]}</td></tr>'
        for l, r in s["labels"].items())
    err_rows = "".join(f'<tr><td>{esc(e["label"])}</td><td style="text-align:left;white-space:normal">{esc(e["message"])}</td>'
                       f'<td>{e["count"]:,}</td></tr>' for e in s["top_errors"]) or '<tr><td colspan="3">none</td></tr>'
    kv = lambda d: "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in d.items())
    chaos_html = ""
    ch = s.get("chaos")
    if ch:
        ev_rows = "".join(f'<tr><td>{i + 1}</td><td>{esc(e["t"])}s</td><td style="text-align:left">{esc(e["action"])} {esc(e["node"])}</td>'
                          f'<td style="text-align:left">{"✓" if e["ok"] else "✕"} {esc(e["note"])[:90]}</td></tr>' for i, e in enumerate(ch["events"]))
        ph_rows = "".join(f'<tr><td>{esc(p["phase"])}</td><td>{p["from_s"]}–{p["to_s"]}s</td><td>{p["tps"]}</td><td>{p["error_pct"]}%</td>'
                          f'<td>{p["p95_ms"]}</td><td>{p["p99_ms"]}</td><td>{p["errors"]:,}</td></tr>' for p in ch["phases"])
        rec = ch.get("recovery_time_s")
        kp2 = [(f'{ch.get("longest_outage_s", 0)}s', "longest full outage (no successful op)"),
               (f'{ch.get("zero_success_seconds", 0)}s', "seconds with zero successful ops"),
               ("not recovered" if rec is None else f"{rec}s", "until stable at >=90% of baseline (after node ready)")]
        un = ch.get("unplanned") or []
        banner_un = "".join(f'<div class="banner bad"><strong>⚠ Unplanned node failure</strong> {esc(u["node"])} left the cluster at {esc(u["t"])}s without an injected fault. '
                            'This run is no longer a single-fault experiment - check that node\'s logs.</div>' for u in un)
        chaos_html = (banner_un + '<h2>Fault injection</h2><div class="kpis">' + "".join(f'<div class="kpi"><b>{esc(a)}</b><span>{esc(b)}</span></div>' for a, b in kp2) +
                      '</div><div class="card scroll" style="margin-top:12px"><table><tr><th>#</th><th>at</th><th style="text-align:left">event</th>'
                      f'<th style="text-align:left">result</th></tr>{ev_rows}</table><p style="margin-top:8px">Numbered markers on the charts below show these events '
                      '(red = fault, green = recovery).</p></div><div class="card scroll"><table><tr><th>phase</th><th>window</th><th>ops/s</th><th>errors</th>'
                      f'<th>p95 ms</th><th>p99 ms</th><th>failed ops</th></tr>{ph_rows}</table></div>')
    probe_html = ""
    if s.get("probe"):
        pr = "".join(f'<tr><td>{esc(r["target"])}</td><td>{esc(r["op"])}</td><td>{r["probes"]:,}</td><td>{r["ok_pct"]}%</td><td>{r["longest_gap_s"]}s</td>'
                     f'<td>{r["p50_ms"]}</td><td>{r["p99_ms"]}</td><td style="text-align:left;white-space:normal">{esc(r["top_error"])}</td></tr>' for r in s["probe"])
        probe_html = ('<h2>Availability probe</h2><div class="card scroll"><p>Independent Java client, new connection per probe, 5s hard timeout. '
                      '"Longest gap" = longest time with no successful probe of that kind.</p><table><tr><th>endpoint</th><th>op</th><th>probes</th><th>success</th>'
                      f'<th>longest gap</th><th>p50 ms</th><th>p99 ms</th><th style="text-align:left">most common error</th></tr>{pr}</table></div>')
    when = ""
    if data["meta"].get("start_epoch"):
        when = datetime.datetime.fromtimestamp(data["meta"]["start_epoch"]).strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GG9 Perf Report</title><style>{CSS}</style></head><body><main>
<h1>GridGain 9 performance report</h1>
<p class="sub">{esc(s["workload"])} · {esc(s["mode"])} · env <b>{esc(s["env"])}</b> · GridGain {esc(data["meta"].get("gridgain_version", "?"))} · {esc(when)} · run {esc(s["run"])}</p>
{banner}<div class="kpis">{kpis}</div>
<p class="sub" style="margin-top:8px">Stats cover the steady window ({s["steady_window_s"]}s of {s["duration_s"]}s; warm-up excluded in steady mode).</p>
<h2>Operations</h2><div class="card scroll"><table><tr><th>operation</th><th>samples</th><th>ops/s</th><th>errors</th><th>avg ms</th>
<th>p50</th><th>p90</th><th>p95</th><th>p99</th><th>max</th></tr>{lab_rows}</table></div>
{chaos_html}{probe_html}<h2>Over time</h2><div id="charts"></div>
<h2>Errors</h2><div class="card scroll"><table><tr><th>operation</th><th style="text-align:left">message</th><th>count</th></tr>{err_rows}</table></div>
<h2>Configuration</h2><div class="card"><dl class="kv">{kv(data["env"])}</dl></div>
<div class="card"><dl class="kv">{kv(prof)}</dl></div>
</main><script id="data" type="application/json">{payload}</script><script>{JS}</script></body></html>"""


def main(run):
    summary, data = build(run)
    json.dump(summary, open(os.path.join(run, "summary.json"), "w"), indent=2)
    open(os.path.join(run, "report.html"), "w").write(render(summary, data))
    st = summary["steady"]
    print(f'{summary["workload"]}/{summary["mode"]}: {st["tps"]} ops/s, err {st["error_pct"]}%, p95 {st["p95_ms"]} ms, p99 {st["p99_ms"]} ms')


if __name__ == "__main__":
    main(sys.argv[1])
