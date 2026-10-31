"""Builds a JMeter 5.x test plan (.jmx) from the analysis."""
from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
import zlib

from .correlate import Analysis, substitute
from .naming import short_name

JMETER_VERSION = "5.6.3"


def _h(x: str) -> str:
    return str(zlib.crc32(x.encode("utf-8")))


# ------------------------------------------------------------------ xml helpers
def _el(tag, testclass=None, guiclass=None, testname=None, enabled=True, **attrs):
    a = {}
    if guiclass:
        a["guiclass"] = guiclass
    if testclass:
        a["testclass"] = testclass
    if testname is not None:
        a["testname"] = testname
    if testclass:
        a["enabled"] = "true" if enabled else "false"
    a.update(attrs)
    return ET.Element(tag, a)


_ILLEGAL = __import__("re").compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def sp(parent, name, value):
    e = ET.SubElement(parent, "stringProp", {"name": name})
    # strip characters XML 1.0 forbids (they make the JMeter GUI refuse the whole file)
    e.text = "" if value is None else _ILLEGAL.sub("", str(value))
    return e


def bp(parent, name, value: bool):
    e = ET.SubElement(parent, "boolProp", {"name": name})
    e.text = "true" if value else "false"
    return e


def ip(parent, name, value: int):
    e = ET.SubElement(parent, "intProp", {"name": name})
    e.text = str(value)
    return e


def add(tree, element):
    """Append element + its hashTree to a parent hashTree; returns the child hashTree."""
    tree.append(element)
    ht = ET.SubElement(tree, "hashTree")
    return ht


def arguments(parent, name, pairs, element_type="Argument"):
    ep = ET.SubElement(parent, "elementProp", {"name": name, "elementType": "Arguments",
                                               "guiclass": "ArgumentsPanel", "testclass": "Arguments",
                                               "testname": "User Defined Variables", "enabled": "true"})
    coll = ET.SubElement(ep, "collectionProp", {"name": "Arguments.arguments"})
    for k, v in pairs:
        a = ET.SubElement(coll, "elementProp", {"name": k, "elementType": element_type})
        sp(a, "Argument.name", k)
        sp(a, "Argument.value", v)
        sp(a, "Argument.metadata", "=")
    return ep


def comment(el, text):
    sp(el, "TestElement.comments", text)


# ------------------------------------------------------------------ builders
def udv(pairs, testname="Environment & Settings"):
    el = _el("Arguments", "Arguments", "ArgumentsPanel", testname)
    coll = ET.SubElement(el, "collectionProp", {"name": "Arguments.arguments"})
    for k, v, desc in pairs:
        a = ET.SubElement(coll, "elementProp", {"name": k, "elementType": "Argument"})
        sp(a, "Argument.name", k)
        sp(a, "Argument.value", v)
        sp(a, "Argument.metadata", "=")
        if desc:
            sp(a, "Argument.desc", desc)
    return el


def header_manager(headers, testname="HTTP Header Manager"):
    el = _el("HeaderManager", "HeaderManager", "HeaderPanel", testname)
    coll = ET.SubElement(el, "collectionProp", {"name": "HeaderManager.headers"})
    for k, v in headers:
        h = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "Header"})
        sp(h, "Header.name", k)
        sp(h, "Header.value", v)
    return el


def json_extractor(var, expr, match=1):
    el = _el("JSONPostProcessor", "JSONPostProcessor", "JSONPostProcessorGui", f"JSON Extractor - {var}")
    sp(el, "JSONPostProcessor.referenceNames", var)
    sp(el, "JSONPostProcessor.jsonPathExprs", expr)
    sp(el, "JSONPostProcessor.match_numbers", str(match))
    sp(el, "JSONPostProcessor.defaultValues", f"{var}_NOT_FOUND")
    return el


def regex_extractor(var, regex, match=1, where="body"):
    el = _el("RegexExtractor", "RegexExtractor", "RegexExtractorGui", f"Regex Extractor - {var}")
    sp(el, "RegexExtractor.useHeaders", {"body": "false", "headers": "true", "url": "URL"}[where])
    sp(el, "RegexExtractor.refname", var)
    sp(el, "RegexExtractor.regex", regex)
    sp(el, "RegexExtractor.template", "$1$")
    sp(el, "RegexExtractor.default", f"{var}_NOT_FOUND")
    bp(el, "RegexExtractor.default_empty_value", False)
    sp(el, "RegexExtractor.match_number", str(match))
    return el


def user_parameters(names_values, testname):
    el = _el("UserParameters", "UserParameters", "UserParametersGui", testname)
    names = ET.SubElement(el, "collectionProp", {"name": "UserParameters.names"})
    for n, _ in names_values:
        sp(names, _h(n), n)
    tv = ET.SubElement(el, "collectionProp", {"name": "UserParameters.thread_values"})
    col = ET.SubElement(tv, "collectionProp", {"name": "user_1"})
    for n, v in names_values:
        sp(col, _h(n + v), v)
    bp(el, "UserParameters.per_iteration", False)
    return el


def code_assertion(code):
    el = _el("ResponseAssertion", "ResponseAssertion", "AssertionGui", f"Assert HTTP {code}")
    coll = ET.SubElement(el, "collectionProp", {"name": "Asserion.test_strings"})
    sp(coll, _h(str(code)), str(code))
    sp(el, "Assertion.custom_message", f"Expected HTTP {code} as recorded")
    sp(el, "Assertion.test_field", "Assertion.response_code")
    bp(el, "Assertion.assume_success", False)
    ip(el, "Assertion.test_type", 8)
    return el


def http_sampler(name, host_var, method, path, body, recorded):
    el = _el("HTTPSamplerProxy", "HTTPSamplerProxy", "HttpTestSampleGui", name)
    sp(el, "HTTPSampler.domain", "${" + host_var + "}")
    sp(el, "HTTPSampler.port", "${" + host_var + "_port}")
    sp(el, "HTTPSampler.protocol", "${" + host_var + "_protocol}")
    sp(el, "HTTPSampler.path", path)
    sp(el, "HTTPSampler.method", method)
    sp(el, "HTTPSampler.contentEncoding", "UTF-8")
    bp(el, "HTTPSampler.follow_redirects", False)
    bp(el, "HTTPSampler.auto_redirects", False)
    bp(el, "HTTPSampler.use_keepalive", True)
    bp(el, "HTTPSampler.DO_MULTIPART_POST", False)
    args = ET.SubElement(el, "elementProp", {"name": "HTTPsampler.Arguments", "elementType": "Arguments"})
    coll = ET.SubElement(args, "collectionProp", {"name": "Arguments.arguments"})
    if body:
        bp(el, "HTTPSampler.postBodyRaw", True)
        a = ET.SubElement(coll, "elementProp", {"name": "", "elementType": "HTTPArgument"})
        bp(a, "HTTPArgument.always_encode", False)
        sp(a, "Argument.value", body)
        sp(a, "Argument.metadata", "=")
    comment(el, recorded)
    return el


# ------------------------------------------------------------------ main
def build(analysis: Analysis, steps, app: str, naming: dict, settings: dict | None = None):
    settings = settings or {}
    e = analysis.entries
    variables = analysis.variables
    by_producer = {}
    for v in variables:
        if v.kind == "correlation":
            by_producer.setdefault(v.producer, []).append(v)
    gen_at = {}
    for v in variables:
        if v.kind == "generated":
            gen_at.setdefault(v.first_use, []).append(v)
    params = [v for v in variables if v.kind == "parameter"]

    root = ET.Element("jmeterTestPlan", {"version": "1.2", "properties": "5.0", "jmeter": JMETER_VERSION})
    top = ET.SubElement(root, "hashTree")
    tp = _el("TestPlan", "TestPlan", "TestPlanGui", f"MobPerf - {app}")
    comment(tp, "Generated by MobPerf Studio. Correlated, parameterised and named automatically from a mobile emulator recording. "
                "Override any setting from the command line, e.g. -Jusers=50 -Jrampup=300 -Jloops=10 -Jthink_ms=3000")
    bp(tp, "TestPlan.functional_mode", False)
    bp(tp, "TestPlan.tearDown_on_shutdown", True)
    bp(tp, "TestPlan.serialize_threadgroups", False)
    arguments(tp, "TestPlan.user_defined_variables", [])
    sp(tp, "TestPlan.user_define_classpath", "")
    plan = add(top, tp)

    # environment variables (every value overridable with -J)
    env = []
    for (scheme, host, port), var in analysis.hosts.items():
        env.append((var, "${__P(" + var + "," + host + ")}", "recorded host"))
        env.append((var + "_protocol", "${__P(" + var + "_protocol," + scheme + ")}", ""))
        env.append((var + "_port", "${__P(" + var + "_port," + (str(port) if port else "") + ")}", "empty = default port"))
    add(plan, udv(env))

    defaults = _el("ConfigTestElement", "ConfigTestElement", "HttpDefaultsGui", "HTTP Request Defaults")
    ep = ET.SubElement(defaults, "elementProp", {"name": "HTTPsampler.Arguments", "elementType": "Arguments",
                                                 "guiclass": "HTTPArgumentsPanel", "testclass": "Arguments",
                                                 "testname": "User Defined Variables", "enabled": "true"})
    ET.SubElement(ep, "collectionProp", {"name": "Arguments.arguments"})
    sp(defaults, "HTTPSampler.implementation", "HttpClient4")
    sp(defaults, "HTTPSampler.connect_timeout", "${__P(connect_timeout,10000)}")
    sp(defaults, "HTTPSampler.response_timeout", "${__P(response_timeout,60000)}")
    add(plan, defaults)

    ck = _el("CookieManager", "CookieManager", "CookiePanel", "HTTP Cookie Manager")
    ET.SubElement(ck, "collectionProp", {"name": "CookieManager.cookies"})
    bp(ck, "CookieManager.clearEachIteration", True)
    bp(ck, "CookieManager.controlledByThreadGroup", False)
    add(plan, ck)

    csv_name = settings.get("csv_name", "test_data.csv")
    if params:
        cs = _el("CSVDataSet", "CSVDataSet", "TestBeanGUI", "CSV Data Set - test data")
        sp(cs, "delimiter", ",")
        sp(cs, "fileEncoding", "UTF-8")
        sp(cs, "filename", "${__P(data_file," + csv_name + ")}")
        bp(cs, "ignoreFirstLine", True)
        bp(cs, "quotedData", True)
        bp(cs, "recycle", True)
        sp(cs, "shareMode", "shareMode.all")
        bp(cs, "stopThread", False)
        sp(cs, "variableNames", ",".join(v.name for v in params))
        comment(cs, "One row per virtual user / iteration. Add more rows with real test accounts.")
        add(plan, cs)

    tg = _el("ThreadGroup", "ThreadGroup", "ThreadGroupGui", f"TG_{app}_Mobile_Journey")
    sp(tg, "ThreadGroup.on_sample_error", "startnextloop")
    lc = ET.SubElement(tg, "elementProp", {"name": "ThreadGroup.main_controller", "elementType": "LoopController",
                                           "guiclass": "LoopControlPanel", "testclass": "LoopController",
                                           "testname": "Loop Controller", "enabled": "true"})
    bp(lc, "LoopController.continue_forever", False)
    sp(lc, "LoopController.loops", "${__P(loops,2)}")
    sp(tg, "ThreadGroup.num_threads", "${__P(users,1)}")
    sp(tg, "ThreadGroup.ramp_time", "${__P(rampup,1)}")
    bp(tg, "ThreadGroup.scheduler", False)
    sp(tg, "ThreadGroup.duration", "")
    sp(tg, "ThreadGroup.delay", "")
    bp(tg, "ThreadGroup.same_user_on_next_iteration", True)
    tgt = add(plan, tg)

    sampler_rows = []
    pattern = naming.get("transaction_pattern", "{prefix}_{app}_{nn:02d}_{step}")
    spattern = naming.get("sampler_pattern", "{nn:02d}_{sub:02d}_{method}_{short}")
    prefix = naming.get("transaction_prefix", "MOB")
    for st in steps:
        tname = st.label(prefix, app, pattern)
        tc = _el("TransactionController", "TransactionController", "TransactionControllerGui", tname)
        bp(tc, "TransactionController.includeTimers", False)
        bp(tc, "TransactionController.parent", False)
        tct = add(tgt, tc)
        for sub, pos in enumerate(st.positions, start=1):
            en = e[pos]
            sname = spattern.format(nn=st.nn, sub=sub, method=en.method, short=short_name(en))
            host_var = analysis.hosts[(en.scheme, en.host, en.port)]
            path = en.path + (("?" + en.query) if en.query else "")
            path = substitute(path, pos, variables, "url")
            ctx = "json" if en.req_body.strip()[:1] in ("{", "[") else ("form" if "urlencoded" in en.req_mime else "raw")
            body = substitute(en.req_body, pos, variables, ctx) if en.req_body else ""
            s = http_sampler(sname, host_var, en.method, path, body,
                             f"Recorded #{pos + 1}: {en.method} {en.url[:300]} -> HTTP {en.status}")
            st_tree = add(tct, s)
            # headers
            hdrs = []
            for k, v in en.req_headers:
                lk = k.lower()
                if lk in ("host", "content-length", "connection", "cookie", "accept-encoding", "proxy-connection", "te", "priority", "keep-alive", "transfer-encoding"):
                    continue
                hdrs.append((k, substitute(v, pos, variables, "raw")))
            if pos in analysis.client_cookies:
                hdrs.append(("Cookie", substitute(analysis.client_cookies[pos], pos, variables, "raw")))
            if hdrs:
                add(st_tree, header_manager(hdrs))
            # generated values for this request
            if pos in gen_at:
                add(st_tree, user_parameters([(v.name, v.generator) for v in gen_at[pos]],
                                             "Generate " + ", ".join(v.name for v in gen_at[pos])))
            # extractors on this response
            for v in by_producer.get(pos, []):
                x = v.extractor
                if x["type"] == "json":
                    add(st_tree, json_extractor(v.name, x["expr"], x.get("match", 1)))
                else:
                    add(st_tree, regex_extractor(v.name, x["expr"], x.get("match", 1), x.get("where", "body")))
            if 100 <= en.status < 400 or settings.get("assert_errors"):
                add(st_tree, code_assertion(en.status))
            sampler_rows.append({"transaction": tname, "sampler": sname, "method": en.method,
                                 "url": en.url, "status": en.status, "position": pos})
        # think time at the end of every step
        ta = _el("TestAction", "TestAction", "TestActionGui", f"Think Time - {st.name}")
        ip(ta, "ActionProcessor.action", 1)
        ip(ta, "ActionProcessor.target", 0)
        sp(ta, "ActionProcessor.duration", "0")
        tat = add(tct, ta)
        timer = _el("ConstantTimer", "ConstantTimer", "ConstantTimerGui", "Think Time")
        sp(timer, "ConstantTimer.delay", "${__P(think_ms," + str(st.think_ms) + ")}")
        add(tat, timer)

    for gui, tn in (("ViewResultsFullVisualizer", "View Results Tree"), ("SummaryReport", "Summary Report")):
        rc = _el("ResultCollector", "ResultCollector", gui, tn)
        bp(rc, "ResultCollector.error_logging", False)
        sp(rc, "filename", "")
        add(plan, rc)

    ET.indent(root, space="  ")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode")
    return xml, sampler_rows


def build_csv(analysis: Analysis) -> str:
    params = [v for v in analysis.variables if v.kind == "parameter"]
    if not params:
        return ""
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    w.writerow([v.name for v in params])
    rows = max(len(v.csv_values) for v in params)
    for r in range(rows):
        w.writerow([v.csv_values[r] if r < len(v.csv_values) else v.csv_values[0] for v in params])
    return buf.getvalue()
