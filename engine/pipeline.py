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

    @property
    def base(self) -> str:
        return f"MOB_{self.app}"

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
    hosts = "\n".join(f"  -J{v}=<host>   (recorded: {h})" for (s, h, p), v in res.analysis.hosts.items())
    return f"""MobPerf Studio - {res.app}

1. Keep {jmx} and {csvn} in the same folder.
2. Add real test accounts to {csvn} (one row per virtual user; header row stays).
3. Smoke test (1 user x 2 iterations):
     jmeter -n -t {jmx} -l smoke.jtl -Jusers=1 -Jloops=2
4. Load test example:
     jmeter -n -t {jmx} -l run.jtl -e -o report -Jusers=50 -Jrampup=300 -Jloops=10 -Jthink_ms=3000
5. Point at another environment without editing the script:
{hosts}

Properties: users, rampup, loops, think_ms (blank = recorded think time per step),
connect_timeout, response_timeout, data_file.
"""


def generate(rec1: Recording, rec2: Recording | None = None, opts: Options | None = None) -> Result:
    opts = opts or Options()
    naming = dict(rules.naming_rules())
    naming.update(opts.naming_overrides or {})
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

    jmx, samplers = build(analysis, steps, app, naming, {"csv_name": f"MOB_{app}_data.csv"})
    csv_text = build_csv(analysis)
    kinds = [v.kind for v in analysis.variables]
    cov = None
    if rec2 is not None:
        cov = 100.0 * analysis.dynamic_resolved / analysis.dynamic_total if analysis.dynamic_total else 100.0
    summary = {
        "app": app, "source": rec1.source + (f" + {rec2.source}" if rec2 is not None else ""),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "transactions": len(steps), "requests": len(kept), "removed": len(removed),
        "correlations": kinds.count("correlation"), "parameters": kinds.count("parameter"),
        "generated": kinds.count("generated"), "coverage_pct": cov,
        "dynamic_total": analysis.dynamic_total, "dynamic_resolved": analysis.dynamic_resolved,
        "blockers": sum(1 for f in analysis.flags if f.severity == "blocker"),
        "warnings": sum(1 for f in analysis.flags if f.severity == "warning"),
        "hosts": [h for (_, h, _) in analysis.hosts],
    }
    lr = lint(jmx)
    summary["jmx_check"] = lr.as_dict()
    res = Result(app, jmx, csv_text, "", analysis, steps, removed, samplers, summary)
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
