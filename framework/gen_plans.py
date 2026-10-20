#!/usr/bin/env python3
"""Generates the JMeter plans in ./plans from framework/workloads.py.

  plans/schema.jmx         create zone + tables for every workload
  plans/drop.jmx           drop tables
  plans/load-<wl>.jmx      bulk-load seed data (oltp, analytical)
  plans/run-<wl>.jmx       the measured workload (steady-state AND ramp-to-failure; shaped by properties)
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import jmx  # noqa: E402
from workloads import WORKLOADS, POOL  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / "plans"
DRIVER = "org.apache.ignite.jdbc.IgniteJdbcDriver"
URL = "${__P(jdbc_url,jdbc:ignite:thin://localhost:10800\\,localhost:10801\\,localhost:10802)}"
LOAD_SCRIPT = """
import java.util.concurrent.ThreadLocalRandom
import org.apache.jmeter.protocol.jdbc.config.DataSourceElement
long total = Long.parseLong(props.getProperty('%(prop)s', '%(default)s'))
int batch = Integer.parseInt(props.getProperty('load_batch', '1000'))
long it = (vars.getObject('it') ?: 0L) + 1L
vars.putObject('it', it)
long n = (it - 1L) * Long.parseLong(props.getProperty("load_threads", "8")) + ctx.getThreadNum()   // deterministic, no shared counter
long start = n * batch + 1L
if (start > total) { SampleResult.setIgnore(); SampleResult.setStopThread(true); return 'done' }
long end = Math.min(total, start + batch - 1L)
def rnd = ThreadLocalRandom.current()
def c = DataSourceElement.getConnection('gg9')
try {
  def ps = c.prepareStatement('%(sql)s')
  for (long id = start; id <= end; id++) {
    def row = %(row)s
    for (int i = 0; i < row.size(); i++) { ps.setObject(i + 1, row[i]) }
    ps.addBatch()
  }
  ps.executeBatch()
} finally { c.close() }
return 'rows ' + (end - start + 1L)
"""


def write(name, plan):
    OUT.mkdir(exist_ok=True)
    (OUT / name).write_text(jmx.render_plan(plan))
    print("wrote", OUT / name)


def base_plan(title, variables=None):
    return jmx.test_plan(title, variables or {})


def pool(max_expr):
    return jmx.jdbc_config("GridGain JDBC pool", POOL, DRIVER, URL, max_expr)


def ddl_plan(title, statements):
    tp = base_plan(title)
    tp.add(pool("4"))
    tg = jmx.setup_thread_group("DDL")
    for i, stmt in enumerate(statements):
        tg.add(jmx.jdbc_sampler(f"ddl-{i:02d}", POOL, stmt, qtype="Update Statement"))
    tp.add(tg)
    return tp


def schema_and_drop():
    seen, create = set(), []
    for wl in WORKLOADS.values():
        for s in wl.ddl:
            if s not in seen:
                seen.add(s)
                create.append(s)
    write("schema.jmx", ddl_plan("GG9 schema", create))
    drops = [s for wl in WORKLOADS.values() for s in wl.drop]
    write("drop.jmx", ddl_plan("GG9 drop", drops))


def load_plan(wl):
    tp = base_plan(f"GG9 load {wl.key}")
    tp.add(pool("${__P(load_threads,8)}"))
    for ld in wl.loaders:
        tg = jmx.thread_group(f"load {ld.table}", "${__P(load_threads,8)}", "1", loops=-1)
        script = LOAD_SCRIPT % dict(prop=ld.total_prop, default=ld.total_default,
                                    sql=ld.insert_sql, row=ld.row_expr)
        tg.add(jmx.jsr223_sampler(f"load.{ld.table}", script))
        tp.add(tg)
    write(f"load-{wl.key}.jmx", tp)


def run_plan(wl):
    tp = base_plan(f"GG9 run {wl.key}")
    tp.add(pool("${__intSum(${__P(threads,32)},8)}"))
    # threads/rampup/duration come from properties: steady-state => ramp is short, threads fixed;
    # ramp-to-failure => threads = ceiling, rampup = long, a watcher stops the run at the breakpoint.
    tg = jmx.thread_group("workload", "${__P(threads,32)}", "${__P(rampup,60)}",
                          duration="${__P(duration,600)}", delay="${__P(startup_delay,0)}")
    tg.add(jmx.jsr223_sampler("pick-op", wl.pick_script))
    sw = jmx.switch_controller("op-mix", "${op}")
    for op in wl.ops:
        if op.kind == "jdbc":
            sw.add(jmx.jdbc_sampler(op.name, POOL, op.sql, qtype=op.qtype, args=op.args, types=op.types))
        else:
            sw.add(jmx.jsr223_sampler(op.name, op.script))
    tg.add(sw)
    tg.add(jmx.constant_timer("think-time", "${__P(think_ms,0)}"))
    tp.add(tg)
    write(f"run-{wl.key}.jmx", tp)


if __name__ == "__main__":
    schema_and_drop()
    for w in WORKLOADS.values():
        if w.loaders:
            load_plan(w)
        run_plan(w)
