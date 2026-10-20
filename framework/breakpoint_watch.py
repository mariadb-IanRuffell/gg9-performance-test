#!/usr/bin/env python3
"""Ramp-to-failure watchdog.

Tails the JMeter .jtl, evaluates fixed windows and, when the SLO is breached for N consecutive
windows, asks JMeter to shut down gracefully (UDP 'Shutdown' on the non-GUI control port) and writes
breakpoint.json. Exits when JMeter exits (pid) or the breakpoint fires.

usage: breakpoint_watch.py <results.jtl> <profile.props> <jmeter_pid> <out_dir>
"""
import csv, json, os, socket, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from props import read_props


def pct(vals, q):
    if not vals:
        return 0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def main(jtl, profile, pid, out_dir):
    p = read_props(profile)
    win = float(p.get("window_sec", 10))
    err_lim = float(p.get("stop_error_pct", 2))
    p95_lim = float(p.get("stop_p95_ms", 1000))
    need = int(p.get("stop_consecutive", 3))
    min_samples = int(p.get("stop_min_samples", 20))
    port = int(p.get("jmeterengine.nongui.port", 4445))
    stall = float(p.get("stop_stall_sec", 90))   # no completed samples at all for this long => the system (or client) is wedged
    grace = float(p.get("stop_grace_sec", 30))  # ignore the first seconds (pool/JIT warm-up)

    while not os.path.exists(jtl) and alive(pid):
        time.sleep(1)
    f = open(jtl, newline="")
    header, buf, t0 = None, [], None
    breaches, last_good, history = 0, None, []
    next_eval = time.time() + win
    partial = ""
    last_sample = time.time()
    started = time.time()
    while alive(pid):
        chunk = f.read()
        if chunk:
            last_sample = time.time()
            partial += chunk
            *lines, partial = partial.split("\n")
            for row in csv.reader(lines):
                if not row:
                    continue
                if header is None:
                    header = {k: i for i, k in enumerate(row)}
                    continue
                try:
                    ts = int(row[header["timeStamp"]]) / 1000.0
                    buf.append((ts, int(row[header["elapsed"]]), row[header["success"]] == "true",
                                int(row[header["allThreads"]])))
                except (ValueError, IndexError, KeyError):
                    pass
        if time.time() - last_sample > stall and time.time() - started > grace:
            threads = history[-1]["threads"] if history else 0
            bp = dict(detected_at_s=round(time.time() - started, 1), threads_at_breach=threads, kind="stall",
                      reason="no completed operations for %d s (stall)" % stall, last_good=last_good, windows=history[-6:],
                      criteria=dict(stop_stall_sec=stall))
            with open(os.path.join(out_dir, "breakpoint.json"), "w") as o:
                json.dump(bp, o, indent=2)
            print("BREAKPOINT (stall) at", threads, "threads", flush=True)
            for _ in range(3):
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"StopTestNow", ("127.0.0.1", port))
                time.sleep(5)
            return
        if time.time() >= next_eval and buf:
            next_eval = time.time() + win
            if t0 is None:
                t0 = buf[0][0]
            w, buf = buf, []
            n = len(w)
            errs = sum(1 for s in w if not s[2])
            p95 = pct([s[1] for s in w], 0.95)
            threads = max(s[3] for s in w)
            elapsed = w[-1][0] - t0
            rec = dict(t=round(elapsed, 1), threads=threads, samples=n,
                       tps=round(n / win, 1), err_pct=round(100.0 * errs / n, 2), p95_ms=p95)
            history.append(rec)
            if elapsed < grace or n < min_samples:
                continue
            bad = rec["err_pct"] >= err_lim or (p95_lim > 0 and p95 >= p95_lim)
            if bad:
                breaches += 1
                if breaches >= need:
                    reason = ("error rate %.2f%% >= %.2f%%" % (rec["err_pct"], err_lim)
                              if rec["err_pct"] >= err_lim else "p95 %d ms >= %d ms" % (p95, p95_lim))
                    bp = dict(detected_at_s=rec["t"], threads_at_breach=threads, reason=reason,
                              last_good=last_good, windows=history[-need - 3:], criteria=dict(
                                  stop_error_pct=err_lim, stop_p95_ms=p95_lim, consecutive=need, window_sec=win))
                    with open(os.path.join(out_dir, "breakpoint.json"), "w") as o:
                        json.dump(bp, o, indent=2)
                    print("BREAKPOINT:", reason, "at", threads, "threads", flush=True)
                    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"Shutdown", ("127.0.0.1", port))
                    return
            else:
                breaches, last_good = 0, rec
        time.sleep(1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
