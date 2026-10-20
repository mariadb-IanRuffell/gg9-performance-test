"""Minimal JMX (JMeter test plan) writer. Stdlib only."""
from xml.sax.saxutils import escape

JMETER_VERSION = "5.6.3"


def _p(kind, name, value):
    if isinstance(value, bool):
        value = str(value).lower()
    return f'<{kind} name="{escape(name)}">{escape(str(value))}</{kind}>'


class Node:
    def __init__(self, xml_open, xml_close="", children=None):
        self.open = xml_open
        self.close = xml_close
        self.children = children or []

    def add(self, *nodes):
        self.children.extend(nodes)
        return self

    def render(self):
        inner = "".join(c.render() for c in self.children)
        return f"{self.open}{self.close}<hashTree>{inner}</hashTree>"


def _el(tag, gui, test, name, props, enabled=True):
    body = "".join(props)
    return (f'<{tag} guiclass="{gui}" testclass="{test}" testname="{escape(name)}" '
            f'enabled="{str(enabled).lower()}">{body}</{tag}>')


def test_plan(name, variables):
    args = "".join(
        f'<elementProp name="{escape(k)}" elementType="Argument">'
        f'{_p("stringProp", "Argument.name", k)}{_p("stringProp", "Argument.value", v)}'
        f'{_p("stringProp", "Argument.metadata", "=")}</elementProp>'
        for k, v in variables.items())
    udv = ('<elementProp name="TestPlan.user_defined_variables" elementType="Arguments" '
           'guiclass="ArgumentsPanel" testclass="Arguments" testname="User Defined Variables" enabled="true">'
           f'<collectionProp name="Arguments.arguments">{args}</collectionProp></elementProp>')
    xml = _el("TestPlan", "TestPlanGui", "TestPlan", name, [
        _p("boolProp", "TestPlan.functional_mode", False),
        _p("boolProp", "TestPlan.serialize_threadgroups", True),
        udv])
    return Node(xml)


def jdbc_config(name, pool_ref, driver, url, pool_max, user="", password=""):
    return Node(_el("JDBCDataSource", "TestBeanGUI", "JDBCDataSource", name, [
        _p("boolProp", "autocommit", True), _p("stringProp", "checkQuery", "${__P(pool_check_query,)}"),
        _p("stringProp", "connectionAge", "5000"), _p("stringProp", "connectionProperties", ""),
        _p("stringProp", "dataSource", pool_ref), _p("stringProp", "driver", driver),
        _p("stringProp", "initQuery", ""), _p("boolProp", "keepAlive", True),
        _p("stringProp", "password", password), _p("stringProp", "poolMax", pool_max),
        _p("stringProp", "timeout", "30000"), _p("stringProp", "transactionIsolation", "DEFAULT"),
        _p("stringProp", "trimInterval", "60000"), _p("stringProp", "dbUrl", url),
        _p("stringProp", "username", user)]))


def thread_group(name, threads, ramp, duration=None, loops=None, on_error="continue", delay=""):
    """Duration-based when `duration` is given (scheduler), else fixed `loops`."""
    sched = duration is not None
    loop_count = "-1" if sched else str(loops if loops is not None else 1)
    ctrl = ('<elementProp name="ThreadGroup.main_controller" elementType="LoopController" '
            'guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">'
            f'{_p("boolProp", "LoopController.continue_forever", False)}'
            f'{_p("stringProp", "LoopController.loops", loop_count)}</elementProp>')
    props = [_p("stringProp", "ThreadGroup.on_sample_error", on_error), ctrl,
             _p("stringProp", "ThreadGroup.num_threads", threads),
             _p("stringProp", "ThreadGroup.ramp_time", ramp),
             _p("boolProp", "ThreadGroup.scheduler", sched),
             _p("stringProp", "ThreadGroup.duration", duration if sched else ""),
             _p("stringProp", "ThreadGroup.delay", delay),
             _p("boolProp", "ThreadGroup.same_user_on_next_iteration", True)]
    return Node(_el("ThreadGroup", "ThreadGroupGui", "ThreadGroup", name, props))


def setup_thread_group(name):
    ctrl = ('<elementProp name="ThreadGroup.main_controller" elementType="LoopController" '
            'guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">'
            f'{_p("boolProp", "LoopController.continue_forever", False)}'
            f'{_p("stringProp", "LoopController.loops", "1")}</elementProp>')
    return Node(_el("SetupThreadGroup", "SetupThreadGroupGui", "SetupThreadGroup", name, [
        _p("stringProp", "ThreadGroup.on_sample_error", "continue"), ctrl,
        _p("stringProp", "ThreadGroup.num_threads", "1"), _p("stringProp", "ThreadGroup.ramp_time", "1"),
        _p("boolProp", "ThreadGroup.scheduler", False), _p("stringProp", "ThreadGroup.duration", ""),
        _p("stringProp", "ThreadGroup.delay", "")]))


def jdbc_sampler(name, pool_ref, query, qtype="Prepared Select Statement", args="", types="",
                 result_handler="Count Records"):
    return Node(_el("JDBCSampler", "TestBeanGUI", "JDBCSampler", name, [
        _p("stringProp", "dataSource", pool_ref), _p("stringProp", "queryType", qtype),
        _p("stringProp", "query", query), _p("stringProp", "queryArguments", args),
        _p("stringProp", "queryArgumentsTypes", types), _p("stringProp", "variableNames", ""),
        _p("stringProp", "resultVariable", ""), _p("stringProp", "queryTimeout", ""),
        _p("stringProp", "resultSetMaxRows", ""), _p("stringProp", "resultSetHandler", result_handler)]))


def _jsr223(tag, gui, test, name, script, params="", extra=()):
    return Node(_el(tag, "TestBeanGUI", test, name, [
        _p("stringProp", "scriptLanguage", "groovy"), _p("stringProp", "parameters", params),
        _p("stringProp", "filename", ""), _p("stringProp", "cacheKey", "true"),
        _p("stringProp", "script", script), *extra]))


def jsr223_sampler(name, script, params=""):
    return _jsr223("JSR223Sampler", "TestBeanGUI", "JSR223Sampler", name, script, params)


def teardown_thread_group(name):
    ctrl = ('<elementProp name="ThreadGroup.main_controller" elementType="LoopController" '
            'guiclass="LoopControlPanel" testclass="LoopController" testname="Loop Controller" enabled="true">'
            f'{_p("boolProp", "LoopController.continue_forever", False)}'
            f'{_p("stringProp", "LoopController.loops", "1")}</elementProp>')
    return Node(_el("PostThreadGroup", "PostThreadGroupGui", "PostThreadGroup", name, [
        _p("stringProp", "ThreadGroup.on_sample_error", "continue"), ctrl,
        _p("stringProp", "ThreadGroup.num_threads", "1"), _p("stringProp", "ThreadGroup.ramp_time", "1"),
        _p("boolProp", "ThreadGroup.scheduler", False), _p("stringProp", "ThreadGroup.duration", ""),
        _p("stringProp", "ThreadGroup.delay", "")]))


def switch_controller(name, value):
    return Node(_el("SwitchController", "SwitchControllerGui", "SwitchController", name,
                    [_p("stringProp", "SwitchController.value", value)]))


def simple_controller(name):
    return Node(_el("GenericController", "LogicControllerGui", "GenericController", name, []))


def constant_timer(name, delay_expr):
    return Node(_el("ConstantTimer", "ConstantTimerGui", "ConstantTimer", name,
                    [_p("stringProp", "ConstantTimer.delay", delay_expr)]))


def render_plan(plan):
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<jmeterTestPlan version="1.2" properties="5.0" jmeter="{JMETER_VERSION}">'
            f'<hashTree>{plan.render()}</hashTree></jmeterTestPlan>\n')
