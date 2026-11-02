"""Command line for the same engine (no MCP client needed).

  python -m mcp_server.cli generate rec1.har [rec2.har] --app MyApp [--type web] [--out out_dir] [--validate]
        [--fail-pattern "token expired" --fail-pattern Unauthorized] [--no-body-checks]
        [--think-ms 3000] [--think-dev-pct 20] [--pacing-ms 60000] [--data-mode unique]
  python -m engine.web_recorder scenario.json recording.har      (record a web app in headless Chromium)
  python -m mcp_server.cli check script.jmx
  python -m mcp_server.cli validate script.jmx [--csv data.csv] [--iterations 2]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.jmx_lint import jmeter_load_check, lint  # noqa: E402
from engine.pipeline import Options, generate_files  # noqa: E402
from engine.validator import validate  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mobperf")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("recordings", nargs="+")
    g.add_argument("--app", default="MyApp")
    g.add_argument("--out", default="mobperf_out")
    g.add_argument("--exclude", default="")
    g.add_argument("--include", default="")
    g.add_argument("--validate", action="store_true")
    g.add_argument("--type", choices=["mobile", "web"], default="mobile", help="mobile -> MOB_ names, web -> WEB_ names")
    g.add_argument("--fail-pattern", action="append", default=None,
                   help="text that fails a request when found in its response body (repeatable; replaces rules/assertions.yaml)")
    g.add_argument("--no-body-checks", action="store_true", help="status-code assertions only")
    g.add_argument("--think-ms", type=int, default=None, help="fixed think time for every step (default: recorded per step)")
    g.add_argument("--think-dev-pct", type=int, default=20, help="random think-time variation +/- percent (0 = exact)")
    g.add_argument("--pacing-ms", type=int, default=0, help="minimum iteration duration in ms (0 = off)")
    g.add_argument("--data-mode", choices=["shared", "unique"], default="shared",
                   help="shared = reuse CSV rows; unique = each row once, user stops when rows run out")
    c = sub.add_parser("check")
    c.add_argument("jmx")
    v = sub.add_parser("validate")
    v.add_argument("jmx")
    v.add_argument("--csv", default="")
    v.add_argument("--iterations", type=int, default=2)
    a = ap.parse_args(argv)

    if a.cmd == "generate":
        opts = Options(app_name=a.app, extra_exclude=[x for x in a.exclude.split(",") if x],
                       include_hosts=[x for x in a.include.split(",") if x], kind=a.type,
                       fail_patterns=[] if a.no_body_checks else a.fail_pattern,
                       think_ms_fixed=a.think_ms, think_dev_pct=a.think_dev_pct, pacing_ms=a.pacing_ms, data_mode=a.data_mode)
        res, paths = generate_files(a.recordings[0], a.recordings[1] if len(a.recordings) > 1 else None, a.out, opts, validate=a.validate)
        s = res.summary
        cov = "n/a (single recording)" if s["coverage_pct"] is None else f"{s['coverage_pct']:.0f}%"
        print(f"Transactions {s['transactions']} | requests {s['requests']} (-{s['removed']} noise) | correlations {s['correlations']} "
              f"| params {s['parameters']} | generated {s['generated']} | dynamic resolved {cov}")
        print("JMX checkpoint:", "PASS" if s["jmx_check"]["ok"] else "FAIL " + "; ".join(s["jmx_check"]["errors"]))
        for f in res.analysis.flags:
            print(f"  {f.severity.upper()}: {f.message}")
        if res.validation:
            print("Validation:", res.validation["engine"], "PASS" if res.validation["passed"] else "FAIL")
        print(json.dumps(paths, indent=2))
        return 0 if s["jmx_check"]["ok"] else 1
    if a.cmd == "check":
        r = lint(open(a.jmx, encoding="utf-8", errors="replace").read())
        for code, desc, ok in r.checks:
            print(f"  {code} {'PASS' if ok else 'FAIL'}  {desc}")
        for e in r.errors:
            print("  ERROR", e)
        jl = jmeter_load_check(a.jmx)
        print("  JMeter load:", "not run (JMeter not found - set JMETER_HOME)" if jl is None else ("PASS" if jl["ok"] else "FAIL " + ", ".join(jl["errors"])))
        ok = r.ok and (jl is None or jl["ok"])
        print("READY for JMeter GUI" if ok else "NOT READY")
        return 0 if ok else 1
    res = validate(a.jmx, a.csv or None, a.iterations)
    for r in res["results"]:
        print(f"  it{r.get('iteration', '-')} {'OK ' if r['ok'] else 'ERR'} {r['status']} {r['sampler']} {r.get('error', '')}")
    print("PASSED" if res["passed"] else "FAILED", f"({res['engine']})")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
