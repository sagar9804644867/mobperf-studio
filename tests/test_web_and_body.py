"""Web / API section + body-level assertions, end to end.

  1. Record a WEB app twice in real headless Chromium (Playwright) with step markers (alice, bob)
  2. Generate the script with the same engine used for mobile
  3. Replay it 2 iterations against the strict server (one-time CSRF, cookie session, Bearer token in HTML meta)
  4. Replay as carol, a user who was never recorded
  5. Server returns HTTP 200 + {"error": "token expired"}  ->  the script MUST fail (body check)
  6. Correlated-value assertion fails when the page no longer carries the token
  7. A word that is part of a normal recorded response ('Unauthorized' in a note) is NOT turned into a check
  7. A body check can be switched off (fail_patterns=[]) and the JMX checkpoint still passes

    python tests/test_web_and_body.py
"""
from __future__ import annotations

import json
import os
import socket
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
from engine.web_recorder import record_web, validate_scenario  # noqa: E402

failed = False


def check(cond, msg):
    global failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    failed |= not cond


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def scenario(base, user, pwd):
    return [{"step": "Login"}, {"goto": base + "/login"},
            {"fill": ["input[name=user]", user]}, {"fill": ["input[name=pass]", pwd]},
            {"click": "button[type=submit]"},
            {"step": "View orders"}, {"goto": base + "/home"}, {"wait_ms": 300}]


def replay(res, csv_text, iterations=2):
    return PyReplay(res.jmx, csv_text, {"think_ms": "0"}).run(iterations)


def main():
    port = free_port()
    srv = app.serve(port)
    base = f"http://127.0.0.1:{port}"
    tmp = tempfile.mkdtemp(prefix="mobperf_web_")
    try:
        print("1. Record the web app twice in headless Chromium")
        recs = []
        for user in ("alice", "bob"):
            har = os.path.join(tmp, f"shopweb_{user}.har")
            out = record_web(scenario(base, user, app.USERS[user]), har)
            check(out["requests"] >= 4, f"{user}: {out['requests']} requests recorded")
            with open(har, "rb") as fh:
                rec = load_any(har, fh.read())
            steps = {e.page for e in rec.entries}
            check(steps >= {"Login", "View orders"}, f"{user}: step markers on requests {sorted(s for s in steps if s)}")
            recs.append(rec)

        print("2. Generate with the same engine (default body checks)")
        res = generate(recs[0], recs[1], Options(app_name="ShopWeb"))
        s = res.summary
        print(f"     transactions={s['transactions']} requests={s['requests']} correlations={s['correlations']} "
              f"params={s['parameters']} dynamic={s['coverage_pct']}%")
        check(s["jmx_check"]["ok"], "JMX checkpoint passes")
        check(s["transactions"] == 2, "2 transactions (Login, View orders)")
        names = [v.name for v in res.analysis.variables if v.kind == "correlation"]
        check(any("csrf" in n.lower() for n in names), f"CSRF token correlated from HTML ({names})")
        check(any("token" in n.lower() and "csrf" not in n.lower() for n in names) or len(names) >= 2,
              "API token correlated from the HTML <meta> tag")
        check(s["coverage_pct"] == 100.0, "100% of dynamic values resolved")
        check('<intProp name="Assertion.test_type">20</intProp>' in res.jmx and '<intProp name="Assertion.test_type">6</intProp>' not in res.jmx,
              "body assertion uses JMeter Substring+Not (type 20, literal text - '\"errors\":[' is not a broken regex)")
        check("token expired" in res.jmx and "Error text found in the response body" in res.jmx, "body check patterns in JMX")
        orders = [r for r in res.samplers if "orders" in r["url"]]
        check(bool(orders) and "Unauthorized" not in orders[0]["body_check"],
              "'Unauthorized' is in the recorded orders response, so it is NOT checked on that request")
        check(bool(orders) and "token expired" in orders[0]["body_check"], "'token expired' IS checked on the orders request")

        print("3. Replay 2 iterations (alice, bob)")
        app.S["expire"] = False
        r = replay(res, res.csv)
        bad = [x for x in r["results"] if not x["ok"]]
        check(r["passed"], f"replay passes ({len(r['results'])} samples, {len(bad)} failed)")
        for x in bad[:5]:
            print("       ", x["sampler"], x["status"], x.get("error"))

        print("4. Unrecorded user carol")
        hdr = res.csv.splitlines()[0]
        cols = hdr.split(",")
        row = ["carol" if "user" in c.lower() else ("Carol@123" if "pass" in c.lower() else "") for c in cols]
        r = replay(res, hdr + "\n" + ",".join(row) + "\n", 1)
        check(r["passed"], "carol passes (data comes from CSV, not the recording)")

        print("5. Negative control: HTTP 200 with an error body")
        app.S["expire"] = True
        r = replay(res, res.csv, 1)
        hit = [x for x in r["results"] if not x["ok"]]
        check(not r["passed"], "replay FAILS when the server says token expired with status 200")
        check(any(x["status"] == "200" and "token expired" in (x.get("error") or "") for x in hit),
              f"failure names the text found: {[x.get('error') for x in hit][:2]}")

        print("6. Same response, body checks switched off -> the error slips through (shows why the check matters)")
        res_off = generate(recs[0], recs[1], Options(app_name="ShopWeb", fail_patterns=[]))
        r = replay(res_off, res_off.csv, 1)
        check(r["passed"], "status-only script passes a 200 error body (the gap Rohit pointed out)")
        check(lint(res_off.jmx).ok and "Error text found in the response body" not in res_off.jmx, "no body assertions, still valid JMX")
        app.S["expire"] = False

        print("6b. Correlated-value assertions")
        corr = [v.name for v in res.analysis.variables if v.kind == "correlation"]
        check(all(f'<stringProp name="Scope.variable">{n}</stringProp>' in res.jmx for n in corr),
              f"every correlated variable has a JMeter variable-scope assertion ({len(corr)})")
        app.S["drop_token"] = True
        r = replay(res, res.csv, 1)
        app.S["drop_token"] = False
        errs = " | ".join(x.get("error") or "" for x in r["results"] if not x["ok"])
        check(not r["passed"] and "correlation c_" in errs, f"page without the token fails on the correlation ({errs[:120]})")

        print("7. Custom pattern + scenario validation")
        res_c = generate(recs[0], recs[1], Options(app_name="ShopWeb", fail_patterns=["SessionTimeout"]))
        check("SessionTimeout" in res_c.jmx and "token expired" not in res_c.jmx, "custom pattern list replaces defaults")
        try:
            validate_scenario([{"fill": "#x"}])
            check(False, "bad scenario rejected")
        except ValueError as ex:
            check(True, f"bad scenario rejected ({ex})")
        with open(os.path.join(tmp, "ShopWeb.jmx"), "w") as fh:
            fh.write(res.jmx)
    finally:
        srv.shutdown()

    print("\nRESULT:", "ALL PASSED" if not failed else "FAILURES")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
