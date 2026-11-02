"""Think time (random +/- %), pacing and test-data mode, measured on real requests.

  1. Script structure: one JSR223 think timer per step, pacing start/end, no old ConstantTimer, checkpoint passes
  2. Think time waits really happen and vary inside +/- %; -Jthink_ms / -Jthink_dev_pct override them
  3. Pacing makes every iteration last at least pacing_ms; baked default and -J override both work
  4. Data mode: shared reuses CSV rows; unique uses each row once and stops the user at end of file
  5. The Groovy itself runs in real Groovy with JMeter bindings (when Java + Groovy are available)

    python tests/test_workload.py            (set GROOVY_CP=/path/to/groovy/lib/* to include step 5)
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mock_web_app as app  # noqa: E402
from engine.jmx_lint import lint  # noqa: E402
from engine.model import load_any  # noqa: E402
from engine.pipeline import Options, generate  # noqa: E402
from engine.validator import PyReplay  # noqa: E402

failed = False


def check(cond, msg):
    global failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    failed |= not cond


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def sample(name):
    p = os.path.join(ROOT, "samples", name)
    with open(p, "rb") as fh:
        return load_any(p, fh.read())


def replay(res, props, iterations, csv_text=None):
    pr = dict(BASE_PROPS)
    pr.update(props)
    return PyReplay(res.jmx, res.csv if csv_text is None else csv_text, pr).run(iterations)


def waits(r, kind):
    return [w["ms"] for w in r["waits"] if w["kind"] == kind]


def main():
    global BASE_PROPS
    port = free_port()
    srv = app.serve(port)
    try:
        r1, r2 = sample("shopweb_alice.har"), sample("shopweb_bob.har")
        res = generate(r1, r2, Options(app_name="ShopWeb", kind="web"))
        host_var = next(iter(res.analysis.hosts.values()))
        BASE_PROPS = {host_var + "_port": str(port)}  # point the recorded host at this run's server

        print("1. Script structure")
        n_steps = len(res.steps)
        check(res.jmx.count("<JSR223Timer ") == n_steps + 2, f"{n_steps} think timers + pacing start + pacing end")
        check("<ConstantTimer " not in res.jmx, "no fixed ConstantTimer left")
        check(res.jmx.count("mobperf:think") == n_steps and "mobperf:pacing_end" in res.jmx, "timer scripts present")
        check('<boolProp name="TransactionController.includeTimers">false</boolProp>' in res.jmx,
              "think time is excluded from transaction response time")
        check(lint(res.jmx).ok, "JMX checkpoint passes")
        import xml.etree.ElementTree as ET
        scripts = [e.text or "" for e in ET.fromstring(res.jmx).iter("stringProp") if e.get("name") == "script"]
        check(len(scripts) == n_steps + 2 and not any("${" in x for x in scripts), "no ${...} inside the Groovy (JMeter would expand it)")
        rec = [st.think_ms for st in res.steps]
        print(f"     recorded think times per step: {rec} ms")

        print("2. Think time")
        r = replay(res, {"think_ms": "300", "think_dev_pct": "0", "pacing_ms": "0"}, 1)
        t = waits(r, "think")
        check(r["passed"] and t == [300] * n_steps, f"-Jthink_ms=300 -Jthink_dev_pct=0 -> {t}")
        r = replay(res, {"think_ms": "400", "think_dev_pct": "50", "pacing_ms": "0"}, 3)
        t = waits(r, "think")
        check(all(200 <= x <= 600 for x in t) and len(set(t)) > 1, f"+/-50% -> {t} (random, within 200..600)")
        total = [w["ms"] for w in r["waits"] if w["kind"] == "iteration_total"]
        check(all(x >= min(t) * n_steps for x in total), f"iterations really waited: {total} ms")
        res_fixed = generate(r1, r2, Options(app_name="ShopWeb", kind="web", think_ms_fixed=250, think_dev_pct=0))
        check(f"<stringProp name=\"parameters\">250 0</stringProp>" in res_fixed.jmx, "fixed think 250 ms baked into every step")
        r = replay(res_fixed, {"pacing_ms": "0"}, 1)
        check(waits(r, "think") == [250] * n_steps, f"baked fixed think time used without -J: {waits(r, 'think')}")
        r = replay(res_fixed, {"think_ms": "0"}, 1)
        check(waits(r, "think") == [0] * n_steps, "-Jthink_ms=0 overrides baked value (smoke test mode)")

        print("3. Pacing")
        r = replay(res, {"think_ms": "0", "pacing_ms": "1500"}, 2)
        total = [w["ms"] for w in r["waits"] if w["kind"] == "iteration_total"]
        pw = waits(r, "pacing")
        check(r["passed"] and all(x >= 1450 for x in total), f"-Jpacing_ms=1500 -> iterations last {total} ms")
        check(all(x > 0 for x in pw), f"pacing waited only for the time left: {pw} ms")
        res_p = generate(r1, r2, Options(app_name="ShopWeb", kind="web", pacing_ms=1200))
        r = replay(res_p, {"think_ms": "0"}, 1)
        total = [w["ms"] for w in r["waits"] if w["kind"] == "iteration_total"]
        check(total and total[0] >= 1150, f"baked pacing 1200 ms -> iteration {total} ms")
        r = replay(res_p, {"think_ms": "0", "pacing_ms": "0"}, 1)
        check(waits(r, "pacing") == [0], "-Jpacing_ms=0 switches baked pacing off")
        r = replay(res, {"think_ms": "0", "pacing_ms": "1"}, 1)
        check(waits(r, "pacing") == [0], "iteration slower than pacing -> no extra wait")

        print("4. Test data mode")
        check('<boolProp name="recycle">true</boolProp>' in res.jmx and '<boolProp name="stopThread">false</boolProp>' in res.jmx,
              "shared (default): recycle=true, stopThread=false")
        r = replay(res, {"think_ms": "0"}, 3)
        check(r["iterations_run"] == 3 and r["passed"], "shared: 3 iterations with 2 CSV rows (rows reused)")
        res_u = generate(r1, r2, Options(app_name="ShopWeb", kind="web", data_mode="unique"))
        check('<boolProp name="recycle">false</boolProp>' in res_u.jmx and '<boolProp name="stopThread">true</boolProp>' in res_u.jmx,
              "unique: recycle=false, stopThread=true (JMeter stops the user at end of file)")
        r = replay(res_u, {"think_ms": "0"}, 3)
        check(r["iterations_run"] == 2 and r["data_exhausted"] and r["passed"],
              f"unique: 3 iterations asked, 2 rows -> ran {r['iterations_run']}, then stopped cleanly")
        check(lint(res_u.jmx).ok, "unique-mode JMX passes the checkpoint")
        users = [ln.split(",")[0] for ln in res_u.csv.splitlines()[1:]]
        check(len(users) == len(set(users)), f"CSV rows are distinct users: {users}")
    finally:
        srv.shutdown()

    print("5. Groovy timer scripts in real Groovy")
    cp = os.environ.get("GROOVY_CP", "")
    groovy = shutil.which("groovy")
    if not (cp and shutil.which("java")) and not groovy:
        print("  SKIP  Java + Groovy not found (set GROOVY_CP=<groovy>/lib/*). The Python mirror above was tested.")
    else:
        tmp = tempfile.mkdtemp()
        jmx = os.path.join(tmp, "w.jmx")
        with open(jmx, "w", encoding="utf-8") as fh:
            fh.write(generate(r1, r2, Options(app_name="ShopWeb", kind="web", think_ms_fixed=2000, pacing_ms=4000)).jmx)
        script = os.path.join(ROOT, "tests", "groovy", "timers_check.groovy")
        cmd = ([groovy, script, jmx] if groovy and not cp else ["java", "-cp", cp, "groovy.ui.GroovyMain", script, jmx])
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        print("\n".join("    " + ln for ln in out.stdout.splitlines()))
        check(out.returncode == 0, "Groovy timer scripts behave as specified" + ("" if out.returncode == 0 else " - " + out.stderr[-300:]))

    print("\nRESULT:", "ALL PASSED" if not failed else "FAILURES")
    return 1 if failed else 0


BASE_PROPS: dict = {}

if __name__ == "__main__":
    sys.exit(main())
