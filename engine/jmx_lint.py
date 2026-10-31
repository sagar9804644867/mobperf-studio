"""JMX structure checkpoint - "will this load in the JMeter GUI?"

JMeter (GUI and non-GUI) loads a .jmx through SaveService/XStream. Most "Problem loading XML"
/ CannotResolveClassException / ConversionException errors come from a short list of mistakes.
This checker looks for every one of them before the file ever reaches JMeter:

  L01 well-formed XML, and no characters that XML 1.0 forbids (common in recorded bodies)
  L02 root <jmeterTestPlan> with version/properties/jmeter attributes, one TestPlan
  L03 hashTree pairing - every test element is followed by exactly one <hashTree>
  L04 tag == testclass, and guiclass matches the GUI class JMeter expects for that element
  L05 element placement (Thread Group under Test Plan, samplers/controllers inside a Thread Group)
  L06 property syntax: name attributes, boolProp true/false, intProp/longProp numeric
  L07 required properties present for every element type
  L08 regular expressions compile, JSONPath expressions are valid
  L09 every ${variable} is defined before it is used; every __function is a real JMeter function
  L10 ${...} braces balanced

When a JMeter install is found, `jmeter_load_check` also asks JMeter itself to load the plan
(non-GUI mode, 0 users, so no traffic is sent) - it uses the same loader as the GUI.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# testclass -> expected guiclass (JMeter 5.x)
GUI = {
    "TestPlan": {"TestPlanGui"},
    "Arguments": {"ArgumentsPanel"},
    "ConfigTestElement": {"HttpDefaultsGui"},
    "CookieManager": {"CookiePanel"},
    "CacheManager": {"CacheManagerGui"},
    "HeaderManager": {"HeaderPanel"},
    "CSVDataSet": {"TestBeanGUI"},
    "ThreadGroup": {"ThreadGroupGui"},
    "LoopController": {"LoopControlPanel"},
    "TransactionController": {"TransactionControllerGui"},
    "HTTPSamplerProxy": {"HttpTestSampleGui"},
    "JSONPostProcessor": {"JSONPostProcessorGui"},
    "RegexExtractor": {"RegexExtractorGui"},
    "BoundaryExtractor": {"BoundaryExtractorGui"},
    "UserParameters": {"UserParametersGui"},
    "ResponseAssertion": {"AssertionGui"},
    "TestAction": {"TestActionGui"},
    "ConstantTimer": {"ConstantTimerGui"},
    "UniformRandomTimer": {"UniformRandomTimerGui"},
    "ResultCollector": {"ViewResultsFullVisualizer", "SummaryReport", "StatVisualizer", "TableVisualizer"},
    "JSR223PreProcessor": {"TestBeanGUI"},
    "JSR223PostProcessor": {"TestBeanGUI"},
    "JSR223Sampler": {"TestBeanGUI"},
}
SAMPLERS = {"HTTPSamplerProxy", "JSR223Sampler", "TestAction"}
CONTROLLERS = {"TransactionController", "LoopController"}
THREAD_ONLY = SAMPLERS | CONTROLLERS
REQUIRED = {
    "HTTPSamplerProxy": ["HTTPSampler.domain", "HTTPSampler.path", "HTTPSampler.method"],
    "JSONPostProcessor": ["JSONPostProcessor.referenceNames", "JSONPostProcessor.jsonPathExprs", "JSONPostProcessor.match_numbers"],
    "RegexExtractor": ["RegexExtractor.refname", "RegexExtractor.regex", "RegexExtractor.template", "RegexExtractor.match_number"],
    "CSVDataSet": ["filename", "variableNames", "delimiter"],
    "ThreadGroup": ["ThreadGroup.num_threads", "ThreadGroup.ramp_time"],
    "ResponseAssertion": ["Assertion.test_field", "Assertion.test_type"],
    "ConstantTimer": ["ConstantTimer.delay"],
    "HeaderManager": [],
}
FUNCTIONS = {"P", "property", "UUID", "time", "RandomString", "Random", "changeCase", "urlencode", "urldecode", "V", "javaScript",
             "groovy", "jexl3", "threadNum", "counter", "intSum", "longSum", "machineName", "machineIP", "base64Encode",
             "base64Decode", "digest", "dateTimeConvert", "timeShift", "split", "escapeHtml", "unescapeHtml", "escapeXml",
             "unescape", "evalVar", "eval", "isVarDefined", "log", "logn", "samplerName", "TestPlanName", "iterationNum",
             "env", "RandomDate", "RandomFromMultipleVars", "char", "regexFunction", "StringFromFile", "FileToString",
             "CSVRead", "XPath", "setProperty", "StringToFile"}
JMETER_BUILTIN_VARS = {"__jm__", "JMeterThread.last_sample_ok", "START.MS", "START.YMD", "START.HMS", "TESTSTART.MS", "__jmeter.USER_TOKEN__"}
ILLEGAL_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


@dataclass
class LintResult:
    errors: list = field(default_factory=list)    # will not load / will break at runtime
    warnings: list = field(default_factory=list)
    checks: list = field(default_factory=list)    # (code, description, passed)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self):
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "checks": [{"code": c, "check": d, "passed": p} for c, d, p in self.checks]}


def _name(el):
    return el.get("testname") or el.tag


def lint(jmx_text: str) -> LintResult:
    r = LintResult()
    err = lambda code, msg: r.errors.append(f"{code} {msg}")

    # L01 ---------------------------------------------------------------
    bad = ILLEGAL_XML.search(jmx_text)
    if bad:
        line = jmx_text.count("\n", 0, bad.start()) + 1
        err("L01", f"illegal XML character U+{ord(bad.group()):04X} at line {line} - JMeter will refuse to open the file")
    try:
        root = ET.fromstring(jmx_text.encode("utf-8"))
    except ET.ParseError as ex:
        err("L01", f"XML is not well-formed: {ex}")
        r.checks.append(("L01", "Well-formed XML", False))
        return r
    r.checks.append(("L01", "Well-formed XML, no forbidden characters", not bad))

    # L02 ---------------------------------------------------------------
    n_err = len(r.errors)
    if root.tag != "jmeterTestPlan":
        err("L02", f"root element is <{root.tag}>, expected <jmeterTestPlan>")
    for a in ("version", "properties", "jmeter"):
        if not root.get(a):
            err("L02", f"<jmeterTestPlan> is missing the '{a}' attribute")
    top = list(root)
    if len(top) != 1 or top[0].tag != "hashTree":
        err("L02", "<jmeterTestPlan> must contain exactly one <hashTree>")
        r.checks.append(("L02", "Test plan root", False))
        return r
    plans = [c for c in top[0] if c.tag == "TestPlan"]
    if len(plans) != 1:
        err("L02", f"expected exactly 1 TestPlan, found {len(plans)}")
    r.checks.append(("L02", "Root <jmeterTestPlan> and single Test Plan", len(r.errors) == n_err))

    # L03-L07 walk -------------------------------------------------------
    pair_err = guiclass_err = place_err = prop_err = req_err = 0
    ordered = []  # test elements in execution (document) order

    def walk(tree, depth, in_thread):
        nonlocal pair_err, guiclass_err, place_err, req_err
        kids = list(tree)
        if (tree.text or "").strip():
            err("L03", f"text content found directly inside <hashTree> at depth {depth}")
            pair_err += 1
        i = 0
        while i < len(kids):
            el = kids[i]
            if el.tag == "hashTree":
                err("L03", f"<hashTree> without a preceding test element (depth {depth}, after '{_name(kids[i - 1]) if i else 'start'}')")
                pair_err += 1
                i += 1
                continue
            nxt = kids[i + 1] if i + 1 < len(kids) else None
            if nxt is None or nxt.tag != "hashTree":
                err("L03", f"'{_name(el)}' ({el.tag}) is not followed by its <hashTree>")
                pair_err += 1
            tc, gc = el.get("testclass"), el.get("guiclass")
            if tc is None:
                err("L04", f"<{el.tag}> '{_name(el)}' has no testclass attribute")
                guiclass_err += 1
            elif tc != el.tag:
                err("L04", f"<{el.tag}> declares testclass='{tc}' - tag and testclass must match")
                guiclass_err += 1
            elif tc in GUI and gc not in GUI[tc]:
                err("L04", f"{tc} '{_name(el)}' has guiclass='{gc}', JMeter GUI expects {sorted(GUI[tc])[0]} - the GUI would fail to open this element")
                guiclass_err += 1
            elif tc not in GUI:
                r.warnings.append(f"L04 element type {tc} is not in MobPerf's known list - check it opens in the GUI")
            if el.tag == "ThreadGroup" and depth != 1:
                err("L05", f"Thread Group '{_name(el)}' must sit directly under the Test Plan")
                place_err += 1
            if el.tag in THREAD_ONLY and not in_thread:
                err("L05", f"{el.tag} '{_name(el)}' is outside any Thread Group - it will never run")
                place_err += 1
            if el.tag == "TestPlan" and depth != 0:
                err("L05", "Test Plan nested inside another element")
                place_err += 1
            check_props(el)
            for p in REQUIRED.get(el.tag, []):
                if el.find(f".//*[@name='{p}']") is None:
                    err("L07", f"{el.tag} '{_name(el)}' is missing required property '{p}'")
                    req_err += 1
            if el.tag == "ThreadGroup" and el.find("elementProp[@name='ThreadGroup.main_controller']") is None:
                err("L07", f"Thread Group '{_name(el)}' has no main_controller (LoopController)")
                req_err += 1
            if el.tag == "ResponseAssertion" and el.find("collectionProp[@name='Asserion.test_strings']") is None:
                err("L07", f"Response Assertion '{_name(el)}' has no 'Asserion.test_strings' collection (JMeter's own spelling)")
                req_err += 1
            ordered.append(el)
            if nxt is not None and nxt.tag == "hashTree":
                walk(nxt, depth + 1, in_thread or el.tag == "ThreadGroup")
                i += 2
            else:
                i += 1

    def check_props(el):
        for p in el.iter():
            if p.tag in ("stringProp", "boolProp", "intProp", "longProp", "doubleProp", "floatProp", "collectionProp", "elementProp"):
                if p.get("name") is None:
                    err("L06", f"<{p.tag}> without a name attribute inside '{_name(el)}'")
                txt = (p.text or "").strip()
                if p.tag == "boolProp" and txt not in ("true", "false"):
                    err("L06", f"boolProp '{p.get('name')}' in '{_name(el)}' has value '{txt}' (must be true/false)")
                if p.tag in ("intProp", "longProp") and not re.fullmatch(r"-?\d+", txt):
                    err("L06", f"{p.tag} '{p.get('name')}' in '{_name(el)}' has non-numeric value '{txt}'")
                if p.tag == "elementProp" and not p.get("elementType"):
                    err("L06", f"elementProp '{p.get('name')}' in '{_name(el)}' has no elementType")

    walk(top[0], 0, False)
    by_code = lambda c: sum(1 for e in r.errors if e.startswith(c))
    r.checks.append(("L03", "hashTree pairing (element + hashTree)", by_code("L03") == 0))
    r.checks.append(("L04", "testclass / guiclass match what the GUI expects", by_code("L04") == 0))
    r.checks.append(("L05", "Element placement in the tree", by_code("L05") == 0))
    r.checks.append(("L06", "Property syntax (names, booleans, numbers)", by_code("L06") == 0))
    r.checks.append(("L07", "Required properties per element", by_code("L07") == 0))

    # L08 regex / jsonpath ------------------------------------------------
    for el in ordered:
        if el.tag == "RegexExtractor":
            rx = el.findtext("stringProp[@name='RegexExtractor.regex']") or ""
            if "(?P<" in rx:
                err("L08", f"'{_name(el)}' uses Python-only (?P<..>) group syntax - Java regex will reject it")
            try:
                re.compile(rx)
            except re.error as ex:
                err("L08", f"'{_name(el)}' regex does not compile: {ex}")
            if "(" not in rx:
                err("L08", f"'{_name(el)}' regex has no capture group but template is $1$")
        if el.tag == "JSONPostProcessor":
            for ex in (el.findtext("stringProp[@name='JSONPostProcessor.jsonPathExprs']") or "").split(";"):
                if not re.fullmatch(r"\$(\.\.?[A-Za-z_*][A-Za-z0-9_\-*]*|\[\d+\]|\[\*\]|\['(?:[^'\\]|\\.)*'\]|\[\?\(.*\)\])*", ex.strip()):
                    err("L08", f"'{_name(el)}' JSONPath '{ex}' is not valid")
    r.checks.append(("L08", "Regex / JSONPath expressions valid", by_code("L08") == 0))

    # L09/L10 variables ---------------------------------------------------
    defined = set(JMETER_BUILTIN_VARS)
    used_before = []
    for el in ordered:
        if el.tag == "Arguments" or el.tag == "TestPlan":
            for a in el.iter("elementProp"):
                n = a.findtext("stringProp[@name='Argument.name']")
                if n:
                    defined.add(n)
    for el in ordered:
        # defs created by this element become visible to LATER elements (extractors) or itself (pre-processors)
        if el.tag == "CSVDataSet":
            defined.update(v.strip() for v in (el.findtext("stringProp[@name='variableNames']") or "").split(",") if v.strip())
        if el.tag == "UserParameters":
            defined.update(s.text for s in el.findall("collectionProp[@name='UserParameters.names']/stringProp") if s.text)
    def refs(text):
        out, i = [], 0
        while True:
            j = text.find("${", i)
            if j < 0:
                return out
            depth, k = 1, j + 2
            while k < len(text) and depth:
                if text.startswith("${", k):
                    depth += 1
                    k += 2
                    continue
                if text[k] == "}":
                    depth -= 1
                k += 1
            if depth:
                err("L10", f"unbalanced ${{ in '{text[j:j + 60]}'")
                return out
            out.append(text[j + 2:k - 1])
            i = j + 2
    produced = set()
    for el in ordered:
        own_texts = []
        for p in el.iter():
            if p.tag == "stringProp" and p.text and "${" in p.text:
                own_texts.append(p.text)
        for t in own_texts:
            for ref in refs(t):
                if ref.startswith("__"):
                    fn = re.match(r"__([A-Za-z0-9]+)", ref).group(1)
                    if fn not in FUNCTIONS:
                        err("L09", f"unknown JMeter function __{fn} in '{_name(el)}'")
                    for v in re.findall(r"__time\([^,)]*,([A-Za-z_][\w.]*)\)", "${" + ref + "}") + re.findall(r"__RandomString\([^,]*,[^,]*,([A-Za-z_][\w.]*)\)", "${" + ref + "}"):
                        defined.add(v)
                    continue
                if "${" in ref:
                    continue
                if ref not in defined and ref not in produced:
                    used_before.append((ref, _name(el)))
        if el.tag == "JSONPostProcessor":
            produced.update(x.strip() for x in (el.findtext("stringProp[@name='JSONPostProcessor.referenceNames']") or "").split(";"))
        if el.tag == "RegexExtractor":
            produced.add(el.findtext("stringProp[@name='RegexExtractor.refname']") or "")
    for ref, where in used_before:
        err("L09", f"${{{ref}}} used in '{where}' before any element defines it (it would be sent literally)")
    r.checks.append(("L09", "Every ${variable} defined before use, functions exist", by_code("L09") == 0))
    r.checks.append(("L10", "${...} braces balanced", by_code("L10") == 0))
    return r


def jmeter_load_check(jmx_path: str, timeout: int = 180):
    """Ask a real JMeter install to load the plan (0 users => nothing is sent).
    Returns None when JMeter is not installed."""
    from .validator import find_jmeter
    jm = find_jmeter()
    if not jm:
        return None
    out = tempfile.mkdtemp(prefix="mobperf_load_")
    log = os.path.join(out, "jmeter.log")
    cmd = [jm, "-n", "-t", jmx_path, "-l", os.path.join(out, "x.jtl"), "-j", log, "-Jusers=0", "-Jloops=1"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(os.path.abspath(jmx_path)))
    text = proc.stdout + proc.stderr
    try:
        text += open(log, encoding="utf-8", errors="replace").read()
    except OSError:
        pass
    markers = ["Error in NonGUIDriver", "Problem loading XML", "CannotResolveClassException", "ConversionException",
               "XStreamException", "Error loading test plan", "IllegalUserActionException", "SAXParseException"]
    hits = [m for m in markers if m in text]
    return {"ok": not hits and proc.returncode == 0, "errors": hits, "cmd": " ".join(cmd), "output": text[-3000:]}
