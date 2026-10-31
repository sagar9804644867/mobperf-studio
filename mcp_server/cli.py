"""Command line for the same engine (no MCP client needed).

  python -m mcp_server.cli generate rec1.har [rec2.har] --app MyApp [--out out_dir] [--validate]
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
    c = sub.add_parser("check")
    c.add_argument("jmx")
    v = sub.add_parser("validate")
    v.add_argument("jmx")
    v.add_argument("--csv", default="")
    v.add_argument("--iterations", type=int, default=2)
    a = ap.parse_args(argv)

    if a.cmd == "generate":
        opts = Options(app_name=a.app, extra_exclude=[x for x in a.exclude.split(",") if x],
                       include_hosts=[x for x in a.include.split(",") if x])
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
