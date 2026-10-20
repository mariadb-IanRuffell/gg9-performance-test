#!/usr/bin/env python3
"""Builds <suite>/index.html: one row per test with headline numbers and a link to its report."""
import csv, html, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from report import CSS


def main(suite):
    rows = list(csv.DictReader(open(os.path.join(suite, "summary.tsv")), delimiter="\t"))
    body = ""
    for r in rows:
        run, s = r["run_dir"], None
        sj = os.path.join(run, "summary.json")
        if run not in ("-", "") and os.path.exists(sj):
            s = json.load(open(sj))
        st = s["steady"] if s else {}
        bp = (s or {}).get("breakpoint") or {}
        ch = (s or {}).get("chaos") or {}
        extra = f'breakpoint @ {bp["threads_at_breach"]} threads' if bp else ""
        if ch:
            extra = f'longest outage {ch.get("longest_outage_s", "?")}s' + (f', {len(ch["unplanned"])} unplanned node exit(s)' if ch.get("unplanned") else "")
        link = f'<a href="../{html.escape(os.path.basename(run))}/report.html">report</a>' if s else ""
        ok = r["status"] == "OK"
        body += (f'<tr><td>{html.escape(r["profile"])}</td><td>{r["attempt"]}</td><td>{"✓" if ok else "✕"} {html.escape(r["status"])}</td>'
                 f'<td>{st.get("tps", "")}</td><td>{st.get("error_pct", "")}</td><td>{st.get("p95_ms", "")}</td><td>{st.get("p99_ms", "")}</td>'
                 f'<td>{(s or {}).get("peak_tps", "")}</td><td style="text-align:left;white-space:normal">{html.escape(extra or r["note"])}</td><td>{link}</td></tr>')
    open(os.path.join(suite, "index.html"), "w").write(f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GG9 suite</title><style>{CSS}</style></head><body><main><h1>Suite {html.escape(os.path.basename(suite))}</h1>
<div class="card scroll"><table><tr><th>profile</th><th>attempt</th><th>status</th><th>ops/s</th><th>err %</th><th>p95 ms</th><th>p99 ms</th><th>peak ops/s</th>
<th style="text-align:left">notes</th><th></th></tr>{body}</table></div></main></body></html>""")


if __name__ == "__main__":
    main(sys.argv[1])
