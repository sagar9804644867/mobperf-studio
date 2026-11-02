"""Script validation.

* `run_jmeter`  - runs the generated .jmx with a real JMeter install (non-GUI) when one is available.
* `PyReplay`    - a dependency-free replayer that interprets the JMeter elements MobPerf generates
                  (UDV, CSV Data Set, Cookie Manager, HTTP sampler, Header Manager, User Parameters,
                  JSON/Regex extractors, Response Assertion and the JMeter functions used).
                  Lets the MCP server / CI validate a script on machines without JMeter.
"""
from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import string
import subprocess
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus, unquote_plus


# =========================================================== JMeter (real)
def find_jmeter() -> str | None:
    home = os.environ.get("JMETER_HOME")
    cands = []
    if home:
        cands += [os.path.join(home, "bin", "jmeter"), os.path.join(home, "bin", "jmeter.bat")]
    for c in cands:
        if os.path.exists(c):
            return c
    return shutil.which("jmeter") or shutil.which("jmeter.bat")


def run_jmeter(jmx_path: str, iterations: int = 2, users: int = 1, extra_props: dict | None = None, timeout: int = 900):
    jm = find_jmeter()
    if not jm:
        return None
    out = tempfile.mkdtemp(prefix="mobperf_jm_")
    jtl = os.path.join(out, "results.jtl")
    cmd = [jm, "-n", "-t", jmx_path, "-l", jtl, f"-Jusers={users}", f"-Jloops={iterations}", "-Jthink_ms=0",
           "-Jjmeter.save.saveservice.output_format=csv", "-Jjmeter.save.saveservice.response_message=true",
           "-Jjmeter.save.saveservice.failure_message=true", "-j", os.path.join(out, "jmeter.log")]
    for k, v in (extra_props or {}).items():
        cmd.append(f"-J{k}={v}")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(os.path.abspath(jmx_path)))
    rows = []
    if os.path.exists(jtl):
        with open(jtl, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                rows.append({"sampler": r.get("label"), "status": r.get("responseCode"),
                             "ok": r.get("success") == "true", "ms": int(r.get("elapsed") or 0),
                             "error": r.get("failureMessage") or ""})
    return {"engine": "jmeter", "cmd": " ".join(cmd), "stdout": proc.stdout[-4000:], "results": rows,
            "passed": bool(rows) and all(r["ok"] for r in rows)}


# =========================================================== function evaluator
def _split_args(s: str):
    args, depth, cur, i = [], 0, "", 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) and s[i + 1] == ",":
            cur += ","
            i += 2
            continue
        if s.startswith("${", i):
            depth += 1
            cur += "${"
            i += 2
            continue
        if ch == "}" and depth:
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    args.append(cur)
    return args


def _java_fmt_to_py(fmt: str) -> str:
    out = fmt
    out = re.sub(r"'([^']*)'", lambda m: m.group(1).replace("%", "%%"), out)
    for j, p in (("yyyy", "%Y"), ("MM", "%m"), ("dd", "%d"), ("HH", "%H"), ("mm", "%M"), ("ss", "%S"), ("SSS", "{ms}")):
        out = out.replace(j, p)
    return out


class Evaluator:
    def __init__(self, props=None):
        self.props = props or {}
        self.vars = {}

    def ev(self, s: str) -> str:
        if s is None or "${" not in s:
            return s or ""
        out, i = "", 0
        while i < len(s):
            j = s.find("${", i)
            if j < 0:
                out += s[i:]
                break
            out += s[i:j]
            depth, k = 1, j + 2
            while k < len(s) and depth:
                if s.startswith("${", k):
                    depth += 1
                    k += 2
                    continue
                if s[k] == "}":
                    depth -= 1
                k += 1
            inner = s[j + 2:k - 1]
            out += self._one(inner, s[j:k])
            i = k
        return out

    def _one(self, inner: str, raw: str) -> str:
        if inner.startswith("__"):
            m = re.match(r"__([A-Za-z]+)(?:\((.*)\))?$", inner, re.S)
            if not m:
                return raw
            fn, argstr = m.group(1), m.group(2)
            args = [self.ev(a) for a in _split_args(argstr)] if argstr is not None else []
            return self._fn(fn, args, raw)
        return self.vars.get(inner, raw)

    def _fn(self, fn, a, raw):
        g = lambda n: a[n] if len(a) > n else ""
        if fn == "P":
            return str(self.props.get(g(0), g(1)))
        if fn == "UUID":
            return str(uuid.uuid4())
        if fn == "time":
            fmt = g(0)
            now = time.time()
            if fmt == "":
                r = str(int(now * 1000))
            elif fmt.startswith("/"):
                r = str(int(now * 1000) // int(fmt[1:]))
            else:
                dt = datetime.fromtimestamp(now, tz=timezone.utc)
                r = dt.strftime(_java_fmt_to_py(fmt)).replace("{ms}", f"{dt.microsecond // 1000:03d}")
            if g(1):
                self.vars[g(1)] = r
            return r
        if fn == "RandomString":
            r = "".join(random.choice(g(1) or string.ascii_letters) for _ in range(int(g(0) or 8)))
            if g(2):
                self.vars[g(2)] = r
            return r
        if fn == "changeCase":
            mode = (g(1) or "UPPER").upper()
            return g(0).upper() if mode == "UPPER" else g(0).lower()
        if fn == "urlencode":
            return quote_plus(g(0))
        if fn == "urldecode":
            return unquote_plus(g(0))
        return raw


# =========================================================== JSON path subset
def jsonpath_get(obj, expr: str):
    toks = re.findall(r"\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]|\['((?:[^'\\]|\\.)*)'\]", expr[1:])
    cur = obj
    for name, idx, quoted in toks:
        try:
            if name:
                cur = cur[name]
            elif idx:
                cur = cur[int(idx)]
            else:
                cur = cur[quoted.replace("\\'", "'")]
        except (KeyError, IndexError, TypeError):
            return None
    if isinstance(cur, bool):
        return "true" if cur else "false"
    if isinstance(cur, (dict, list)):
        return json.dumps(cur, separators=(",", ":"))
    if isinstance(cur, float) and cur.is_integer():
        return str(cur)
    return None if cur is None else str(cur)


# =========================================================== JMX parsing
def _props(el):
    p = {}
    for c in el:
        if c.tag in ("stringProp", "boolProp", "intProp", "longProp"):
            p[c.get("name")] = c.text or ""
    return p


def _pairs(tree_list):
    """hashTree children come as element, hashTree, element, hashTree..."""
    items = list(tree_list)
    out = []
    i = 0
    while i < len(items):
        el = items[i]
        ht = items[i + 1] if i + 1 < len(items) and items[i + 1].tag == "hashTree" else None
        out.append((el, ht))
        i += 2 if ht is not None else 1
    return out


class PyReplay:
    def __init__(self, jmx_text: str, csv_text: str = "", props: dict | None = None, base_dir: str | None = None,
                 proxies: dict | None = None, verify_tls: bool = True):
        self.root = ET.fromstring(jmx_text)
        self.csv_rows = []
        if csv_text:
            rdr = csv.reader(io.StringIO(csv_text))
            next(rdr, None)
            self.csv_rows = [r for r in rdr if r]
        self.props = props or {}
        self.proxies = proxies
        self.verify = verify_tls

    def run(self, iterations: int = 2):
        import requests
        plan_tree = self.root.find("hashTree")
        (_tp, plan_children), = _pairs(plan_tree)[:1]
        ev = Evaluator(self.props)
        csv_vars, recycle, stop_thread = [], True, False
        tg = None
        for el, ht in _pairs(plan_children):
            if el.get("enabled") == "false":
                continue
            if el.tag == "Arguments":
                for a in el.iter("elementProp"):
                    pp = _props(a)
                    if "Argument.name" in pp:
                        ev.vars[pp["Argument.name"]] = ev.ev(pp.get("Argument.value", ""))
            elif el.tag == "CSVDataSet":
                cp = _props(el)
                csv_vars = cp.get("variableNames", "").split(",")
                recycle = cp.get("recycle", "true") != "false"
                stop_thread = cp.get("stopThread", "false") == "true"
            elif el.tag == "ThreadGroup":
                tg = (el, ht)
        results = []
        self.waits = []
        self._iter_start = None
        if tg is None:
            return {"engine": "pyreplay", "results": [], "passed": False, "error": "no thread group"}
        done, exhausted = 0, False
        for it in range(iterations):
            sess = requests.Session()
            sess.trust_env = False
            if self.proxies:
                sess.proxies.update(self.proxies)
            if csv_vars and self.csv_rows:
                if it >= len(self.csv_rows) and not recycle:
                    if stop_thread:  # JMeter: CSV Data Set stopThread=true ends this user at end of file
                        exhausted = True
                        break
                    row = ["<EOF>"] * len(csv_vars)
                else:
                    row = self.csv_rows[it % len(self.csv_rows)]
                for n, v in zip(csv_vars, row):
                    ev.vars[n] = v
            t0 = time.time()
            self._walk(tg[1], ev, sess, results, it + 1, None)
            self.waits.append({"iteration": it + 1, "kind": "iteration_total", "ms": int((time.time() - t0) * 1000)})
            done += 1
        passed = bool(results) and all(r["ok"] for r in results)
        return {"engine": "pyreplay", "results": results, "passed": passed, "iterations_run": done,
                "data_exhausted": exhausted, "waits": self.waits}

    def _timer_ms(self, el, ev, it):
        """Python mirror of the timers MobPerf generates (same rules as the Groovy in jmx_builder)."""
        cp = _props(el)
        if el.tag == "ConstantTimer":
            try:
                return int(float(ev.ev(cp.get("ConstantTimer.delay", "0")) or 0)), "constant"
            except ValueError:
                return 0, "constant"
        if el.tag != "JSR223Timer":
            return 0, "other"
        script, params = cp.get("script", ""), ev.ev(cp.get("parameters", "")).strip()

        def prop(name):
            return str(self.props.get(name, "")).strip()
        if "mobperf:think" in script:
            a = params.split() or ["0"]
            rec, def_pct = int(a[0]), (int(a[1]) if len(a) > 1 else 20)
            base = int(prop("think_ms")) if prop("think_ms") else rec
            pct = max(0, min(int(prop("think_dev_pct")) if prop("think_dev_pct") else def_pct, 100))
            d = base * pct // 100
            return max(0, base - d + random.randint(0, 2 * d)), "think"
        if "mobperf:pacing_start" in script:
            self._iter_start = time.time()
            return 0, "pacing_start"
        if "mobperf:pacing_end" in script:
            pacing = int(prop("pacing_ms")) if prop("pacing_ms") else int(params or 0)
            if pacing <= 0 or self._iter_start is None:
                return 0, "pacing"
            return max(0, pacing - int((time.time() - self._iter_start) * 1000)), "pacing"
        return 0, "jsr223 (not run in Python replay)"

    def _walk(self, tree, ev, sess, results, it, txn):
        for el, ht in _pairs(tree):
            if el.get("enabled") == "false":
                continue
            if el.tag == "TransactionController":
                self._walk(ht, ev, sess, results, it, el.get("testname"))
            elif el.tag == "HTTPSamplerProxy":
                results.append(self._sample(el, ht, ev, sess, it, txn))
            elif el.tag == "TestAction" and ht is not None:
                for t, _ in _pairs(ht):
                    if t.get("enabled") == "false" or not t.tag.endswith("Timer"):
                        continue
                    ms, kind = self._timer_ms(t, ev, it)
                    if ms > 0:
                        time.sleep(ms / 1000.0)
                    if kind in ("think", "pacing", "constant"):
                        self.waits.append({"iteration": it, "kind": kind, "ms": ms, "step": txn or el.get("testname")})

    def _sample(self, el, ht, ev, sess, it, txn):
        import requests
        children = _pairs(ht) if ht is not None else []
        for c, _ in children:  # pre-processors
            if c.tag == "UserParameters":
                names = [s.text for s in c.find("collectionProp[@name='UserParameters.names']")]
                tv = c.find("collectionProp[@name='UserParameters.thread_values']")
                vals = [s.text for s in tv[0]]
                for n, v in zip(names, vals):
                    ev.vars[n] = ev.ev(v)
        p = _props(el)
        proto = ev.ev(p.get("HTTPSampler.protocol")) or "https"
        host = ev.ev(p.get("HTTPSampler.domain"))
        port = ev.ev(p.get("HTTPSampler.port"))
        path = ev.ev(p.get("HTTPSampler.path"))
        url = f"{proto}://{host}{(':' + port) if port else ''}{path}"
        method = p.get("HTTPSampler.method", "GET")
        body = None
        for a in el.iter("elementProp"):
            if a.get("elementType") == "HTTPArgument":
                body = ev.ev(_props(a).get("Argument.value", ""))
        headers = {}
        for c, _ in children:
            if c.tag == "HeaderManager":
                for h in c.iter("elementProp"):
                    hp = _props(h)
                    headers[hp.get("Header.name")] = ev.ev(hp.get("Header.value"))
        t0 = time.time()
        rec = {"iteration": it, "transaction": txn, "sampler": el.get("testname"), "method": method, "url": url}
        try:
            r = sess.request(method, url, data=body.encode("utf-8") if body else None, headers=headers,
                             allow_redirects=False, timeout=60, verify=self.verify)
        except requests.RequestException as ex:
            rec.update(status="ERR", ok=False, ms=int((time.time() - t0) * 1000), error=str(ex)[:300])
            return rec
        rec["ms"] = int((time.time() - t0) * 1000)
        rec["status"] = str(r.status_code)
        try:
            hpairs = list(r.raw.headers.items())
        except Exception:
            hpairs = list(r.headers.items())
        htext = f"HTTP/1.1 {r.status_code} {r.reason}\n" + "".join(f"{k}: {v}\n" for k, v in hpairs)
        text = r.text
        errors = []
        extracted = {}
        for c, _ in children:
            cp = _props(c)
            if c.tag == "JSONPostProcessor":
                var = cp["JSONPostProcessor.referenceNames"]
                try:
                    val = jsonpath_get(json.loads(text), cp["JSONPostProcessor.jsonPathExprs"])
                except Exception:
                    val = None
                ev.vars[var] = val if val is not None else cp.get("JSONPostProcessor.defaultValues", "")
                extracted[var] = ev.vars[var]
                if val is None:
                    errors.append(f"{var} not found")
            elif c.tag == "RegexExtractor":
                var = cp["RegexExtractor.refname"]
                src = htext if cp.get("RegexExtractor.useHeaders") == "true" else (url if cp.get("RegexExtractor.useHeaders") == "URL" else text)
                ms = [m.group(1) for m in re.finditer(cp["RegexExtractor.regex"], src, re.M)]
                n = int(cp.get("RegexExtractor.match_number") or 1)
                val = ms[n - 1] if 0 < n <= len(ms) else None
                ev.vars[var] = val if val is not None else cp.get("RegexExtractor.default", "")
                extracted[var] = ev.vars[var]
                if val is None:
                    errors.append(f"{var} not found")
            elif c.tag == "ResponseAssertion":
                strings = [s.text or "" for s in c.find("collectionProp[@name='Asserion.test_strings']") or []]
                if cp.get("Assertion.scope") == "variable":
                    vname = cp.get("Scope.variable", "")
                    target = str(ev.vars.get(vname, ""))
                    if bool(int(cp.get("Assertion.test_type") or 2) & 4) and any(s and s in target for s in strings):
                        errors.append(f"correlation {vname} not found")
                elif cp.get("Assertion.test_field") == "Assertion.response_data":
                    # JMeter: Substring (16) / Contains (2) with Not (4) fails when any string is found
                    hit = [s for s in strings if s and s in text]
                    negate = bool(int(cp.get("Assertion.test_type") or 2) & 4)
                    if negate and hit:
                        errors.append("response body contains " + ", ".join(repr(h) for h in hit))
                else:
                    if str(r.status_code) not in strings:
                        errors.append(f"expected HTTP {strings[0] if strings else '?'}, got {r.status_code}")
        rec["ok"] = not errors
        rec["error"] = "; ".join(errors)
        rec["extracted"] = extracted
        rec["response_snippet"] = text[:300]
        return rec


def validate(jmx_path: str, csv_path: str | None = None, iterations: int = 2, prefer_jmeter: bool = True, props=None,
             proxies=None, verify_tls=True):
    if prefer_jmeter and find_jmeter():
        res = run_jmeter(jmx_path, iterations, 1, props)
        if res is not None:
            return res
    with open(jmx_path, encoding="utf-8") as fh:
        jmx = fh.read()
    csv_text = ""
    if csv_path and os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8") as fh:
            csv_text = fh.read()
    pr = {"think_ms": "0", "pacing_ms": "0"}
    pr.update(props or {})
    return PyReplay(jmx, csv_text, pr, proxies=proxies, verify_tls=verify_tls).run(iterations)
