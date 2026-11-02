"""One call from recording(s) to a ready-to-run JMeter bundle."""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

from . import rules
from .correlate import Analysis, Correlator
from .jmx_builder import build, build_csv
from .jmx_lint import lint
from .model import Recording, load_any, slug
from .naming import detect_steps
from .report import build_report


@dataclass
class Options:
    app_name: str = "MyApp"
    extra_exclude: list = field(default_factory=list)
    include_hosts: list = field(default_factory=list)
    keep_static: bool = False
    step_gap_s: float | None = None
    naming_overrides: dict = field(default_factory=dict)
    step_renames: dict = field(default_factory=dict)  # old step name -> new name
    # Body text that fails a request when present (None = use rules/assertions.yaml defaults; [] = none)
    fail_patterns: list | None = None
    # Workload realism (all can still be overridden at run time with -J)
    think_ms_fixed: int | None = None  # None = recorded think time per step
    think_dev_pct: int = 20            # random variation +/- %, 0 = exact
    pacing_ms: int = 0                 # minimum iteration duration, 0 = off
    data_mode: str = "shared"          # "shared" = recycle rows, "unique" = each row once, then the user stops
    kind: str = "mobile"  # "mobile" -> MOB_ names, "web" -> WEB_ names (same engine)


@dataclass
class Result:
    app: str
    jmx: str
    csv: str
    report_html: str
    analysis: Analysis
    steps: list
    removed: list
    samplers: list
    summary: dict
    validation: dict | None = None
    prefix: str = "MOB"

    @property
    def base(self) -> str:
        return f"{self.prefix}_{self.app}"

    def file_names(self):
        return f"{self.base}.jmx", f"{self.base}_data.csv", f"{self.base}_report.html"

    def bundle(self) -> bytes:
        jmx, csvn, rep = self.file_names()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(jmx, self.jmx)
            if self.csv:
                z.writestr(csvn, self.csv)
            z.writestr(rep, build_report(self))
            z.writestr("HOW_TO_RUN.txt", how_to_run(self))
        return buf.getvalue()


def how_to_run(res: Result) -> str:
    jmx, csvn, _ = res.file_names()
    s = res.summary
    hosts = "\n".join(f"  -J{v}=<host>   (recorded: {h})" for (sc, h, p), v in res.analysis.hosts.items())
    data = ("UNIQUE - each CSV row is used once across all users; a user stops when the rows run out.\n"
            "   Provide at least users x loops rows." if s.get("data_mode") == "unique" else
            "SHARED - rows are handed out in turn to all users and reused from the top when the file ends.")
    pacing = f"{s.get('pacing_ms')} ms" if s.get("pacing_ms") else "off"
    return f"""MobPerf Studio - {res.app}

1. Keep {jmx} and {csvn} in the same folder.
2. Add real test accounts to {csvn} (one row per virtual user; header row stays).
   Data mode: {data}
3. Smoke test (1 user x 2 iterations, no waits):
     jmeter -n -t {jmx} -l smoke.jtl -Jusers=1 -Jloops=2 -Jthink_ms=0 -Jpacing_ms=0
4. Load test example:
     jmeter -n -t {jmx} -l run.jtl -e -o report -Jusers=50 -Jrampup=300 -Jloops=10 -Jpacing_ms=60000
5. Point at another environment without editing the script:
{hosts}

Workload settings baked into this script (override any with -J):
  think time   : {s.get('think', 'recorded per step')} +/- {s.get('think_dev_pct', 20)}% random   (-Jthink_ms=, -Jthink_dev_pct=)
  pacing       : {pacing}   (-Jpacing_ms=60000 makes every iteration last at least 60 s; 0 = off)
  users/rampup/loops : -Jusers= -Jrampup= -Jloops=
  timeouts     : -Jconnect_timeout= -Jresponse_timeout=   data file: -Jdata_file=

Think time is excluded from transaction response times. Pacing waits only for the time left in the
iteration, so throughput per user = 1 iteration per pacing interval (if the iteration is faster than it).
"""


def generate(rec1: Recording, rec2: Recording | None = None, opts: Options | None = None) -> Result:
    opts = opts or Options()
    naming = dict(rules.naming_rules())
    if opts.kind == "web":
        naming["transaction_prefix"] = "WEB"
    naming.update(opts.naming_overrides or {})
    prefix = re.sub(r"[^A-Za-z0-9]+", "", str(naming.get("transaction_prefix") or "MOB")) or "MOB"
    naming["transaction_prefix"] = prefix
    app = slug(opts.app_name, "") or "MyApp"
    app = app[0].upper() + app[1:]

    kept, removed = rules.filter_noise(rec1.entries, opts.extra_exclude, opts.include_hosts, opts.keep_static)
    kept2 = []
    if rec2 is not None:
        kept2, _ = rules.filter_noise(rec2.entries, opts.extra_exclude, opts.include_hosts, opts.keep_static)

    analysis = Correlator(kept, kept2, naming).run()
    gap = opts.step_gap_s if opts.step_gap_s is not None else float(naming.get("step_gap_seconds", 2.0))
    steps = detect_steps(kept, gap, int(naming.get("default_think_time_ms", 3000)))
    for st in steps:
        if st.name in opts.step_renames and opts.step_renames[st.name]:
            st.name = re.sub(r"[^A-Za-z0-9]+", "", opts.step_renames[st.name]) or st.name

    fail_pats = opts.fail_patterns if opts.fail_patterns is not None else rules.assertion_rules().get("fail_patterns", [])
    fail_pats = [p for p in (fail_pats or []) if isinstance(p, str) and p]
    jmx, samplers = build(analysis, steps, app, naming,
                          {"csv_name": f"{prefix}_{app}_data.csv", "fail_patterns": fail_pats,
                           "think_ms_fixed": opts.think_ms_fixed, "think_dev_pct": max(0, min(int(opts.think_dev_pct), 100)),
                           "pacing_ms": max(0, int(opts.pacing_ms or 0)),
                           "data_mode": "unique" if opts.data_mode == "unique" else "shared"})
    csv_text = build_csv(analysis)
    kinds = [v.kind for v in analysis.variables]
    cov = None
    if rec2 is not None:
        cov = 100.0 * analysis.dynamic_resolved / analysis.dynamic_total if analysis.dynamic_total else 100.0
    summary = {
        "app": app, "kind": opts.kind, "prefix": prefix, "source": rec1.source + (f" + {rec2.source}" if rec2 is not None else ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "transactions": len(steps), "requests": len(kept), "removed": len(removed),
        "correlations": kinds.count("correlation"), "parameters": kinds.count("parameter"),
        "generated": kinds.count("generated"), "coverage_pct": cov,
        "dynamic_total": analysis.dynamic_total, "dynamic_resolved": analysis.dynamic_resolved,
        "blockers": sum(1 for f in analysis.flags if f.severity == "blocker"),
        "warnings": sum(1 for f in analysis.flags if f.severity == "warning"),
        "hosts": [h for (_, h, _) in analysis.hosts],
        "fail_patterns": fail_pats,
        "think": "recorded per step" if opts.think_ms_fixed is None else f"{int(opts.think_ms_fixed)} ms",
        "think_dev_pct": max(0, min(int(opts.think_dev_pct), 100)), "pacing_ms": max(0, int(opts.pacing_ms or 0)),
        "data_mode": "unique" if opts.data_mode == "unique" else "shared",
        "body_checks": sum(1 for s in samplers if s.get("body_check")) if samplers and isinstance(samplers[0], dict) else 0,
    }
    lr = lint(jmx)
    summary["jmx_check"] = lr.as_dict()
    res = Result(app, jmx, csv_text, "", analysis, steps, removed, samplers, summary, prefix=prefix)
    res.report_html = build_report(res)
    return res


def generate_files(rec1_path: str, rec2_path: str | None, out_dir: str, opts: Options | None = None,
                   validate: bool = False, **validate_kw) -> tuple[Result, dict]:
    with open(rec1_path, "rb") as fh:
        r1 = load_any(rec1_path, fh.read())
    r1.source = os.path.basename(rec1_path)
    r2 = None
    if rec2_path:
        with open(rec2_path, "rb") as fh:
            r2 = load_any(rec2_path, fh.read())
        r2.source = os.path.basename(rec2_path)
    res = generate(r1, r2, opts)
    os.makedirs(out_dir, exist_ok=True)
    jmx_n, csv_n, rep_n = res.file_names()
    paths = {"jmx": os.path.join(out_dir, jmx_n), "report": os.path.join(out_dir, rep_n)}
    with open(paths["jmx"], "w", encoding="utf-8") as fh:
        fh.write(res.jmx)
    if res.csv:
        paths["csv"] = os.path.join(out_dir, csv_n)
        with open(paths["csv"], "w", encoding="utf-8") as fh:
            fh.write(res.csv)
    from .jmx_lint import jmeter_load_check
    res.summary["jmeter_load"] = jmeter_load_check(paths["jmx"])
    if validate:
        from .validator import validate as _v
        res.validation = _v(paths["jmx"], paths.get("csv"), **validate_kw)
    res.report_html = build_report(res)
    with open(paths["report"], "w", encoding="utf-8") as fh:
        fh.write(res.report_html)
    with open(os.path.join(out_dir, "HOW_TO_RUN.txt"), "w", encoding="utf-8") as fh:
        fh.write(how_to_run(res))
    return res, paths
