"""Self-contained HTML correlation report shipped next to the .jmx."""
from __future__ import annotations

import html


def _esc(x) -> str:
    return html.escape("" if x is None else str(x))


def build_report(res) -> str:
    a = res.analysis
    s = res.summary
    rows_tx = "".join(
        f"<tr><td>{_esc(r['transaction'])}</td><td>{_esc(r['sampler'])}</td><td>{_esc(r['method'])}</td>"
        f"<td class='u'>{_esc(r['url'][:140])}</td><td>{_esc(r['status'])}</td></tr>" for r in res.samplers)

    def var_rows(kind):
        out = ""
        for v in a.variables:
            if v.kind != kind:
                continue
            how = ""
            if v.extractor:
                how = f"{v.extractor['type'].upper()} ({v.extractor['where']}): <code>{_esc(v.extractor['expr'])}</code> match {v.extractor.get('match', 1)}"
            elif v.generator:
                how = f"<code>{_esc(v.generator)}</code>"
            else:
                how = "CSV: " + ", ".join(_esc(x) for x in v.csv_values[:4])
            sample = v.value if len(v.value) <= 48 else v.value[:44] + "..."
            out += (f"<tr><td><b>{_esc(v.name)}</b></td><td>{how}</td><td><code>{_esc(sample)}</code></td>"
                    f"<td>{_esc(len(v.used_in))}</td><td>{_esc(v.evidence)}</td></tr>")
        return out or "<tr><td colspan=5 class='muted'>None</td></tr>"

    flags = "".join(f"<li class='{_esc(f.severity)}'><b>{_esc(f.severity.upper())}</b> "
                    f"{('request #' + str(f.pos + 1) + ': ') if f.pos is not None else ''}{_esc(f.message)}</li>"
                    for f in a.flags) or "<li class='ok'>No blockers found.</li>"
    removed = "".join(f"<tr><td>{_esc(e.method)}</td><td class='u'>{_esc(e.url[:120])}</td><td>{_esc(r)}</td></tr>"
                      for e, r in res.removed[:300])
    val = ""
    if res.validation:
        vr = res.validation
        val = "".join(f"<tr class='{'ok' if r.get('ok') else 'bad'}'><td>{_esc(r.get('iteration', ''))}</td><td>{_esc(r.get('sampler'))}</td>"
                      f"<td>{_esc(r.get('status'))}</td><td>{_esc(r.get('ms'))}</td><td>{_esc(r.get('error'))}</td></tr>"
                      for r in vr["results"])
        val = (f"<h2>Validation run ({_esc(vr['engine'])}) - {'PASSED' if vr['passed'] else 'FAILED'}</h2>"
               f"<table><tr><th>Iter</th><th>Sampler</th><th>HTTP</th><th>ms</th><th>Error</th></tr>{val}</table>")

    jc = s.get("jmx_check") or {}
    jl = s.get("jmeter_load")
    chk = "".join(f"<tr class='{'ok' if c['passed'] else 'bad'}'><td>{_esc(c['code'])}</td><td>{_esc(c['check'])}</td><td>{'PASS' if c['passed'] else 'FAIL'}</td></tr>"
                  for c in jc.get("checks", []))
    chk += ("<tr><td>JM</td><td>Loaded by a real JMeter install (non-GUI, 0 users)</td><td>"
            + ("not run - JMeter not found on this machine" if jl is None else ("PASS" if jl["ok"] else "FAIL: " + _esc(", ".join(jl["errors"]))))
            + "</td></tr>")
    chk_err = "".join(f"<li class='blocker'>{_esc(x)}</li>" for x in jc.get("errors", []))
    jmx_block = (f"<h2>JMX structure checkpoint - {'READY to open in JMeter GUI' if jc.get('ok') else 'WILL NOT LOAD'}</h2>"
                 f"<table><tr><th>Code</th><th>Check</th><th>Result</th></tr>{chk}</table>" + (f"<ul>{chk_err}</ul>" if chk_err else ""))
    cov = s["coverage_pct"]
    cov_txt = f"{cov:.0f}%" if cov is not None else "n/a (single recording)"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>MobPerf report - {_esc(s['app'])}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{{--bg:#f6f9ff;--card:#fff;--ink:#0b1b3a;--muted:#5b6b88;--accent:#1463ff;--ok:#0a8f5b;--bad:#c62828;--warn:#b26a00}}
body{{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}}
main{{max-width:1200px;margin:0 auto;padding:24px 16px}}
h1{{margin:0 0 4px;font-size:24px}} h2{{margin:28px 0 8px;font-size:18px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-top:16px}}
.kpi{{background:var(--card);border:1px solid #dfe7f5;border-radius:10px;padding:12px}} .kpi b{{display:block;font-size:22px;color:var(--accent)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid #dfe7f5;border-radius:8px;overflow:hidden;font-size:13px}}
th,td{{padding:6px 8px;border-bottom:1px solid #eef2fa;text-align:left;vertical-align:top}} th{{background:#eaf1ff}}
td.u{{word-break:break-all}} code{{font-size:12px;word-break:break-all}} .muted{{color:var(--muted)}}
li.blocker b{{color:var(--bad)}} li.warning b{{color:var(--warn)}} li.ok{{color:var(--ok)}} tr.bad td{{color:var(--bad)}}
</style></head><body><main>
<h1>MobPerf Studio - {_esc(s['app'])}</h1><div class="muted">Generated {_esc(s['generated_at'])} - source: {_esc(s['source'])}</div>
<div class="kpis">
<div class="kpi"><b>{s['transactions']}</b>transactions</div><div class="kpi"><b>{s['requests']}</b>requests scripted</div>
<div class="kpi"><b>{s['removed']}</b>noise requests removed</div><div class="kpi"><b>{s['correlations']}</b>correlations</div>
<div class="kpi"><b>{s['parameters']}</b>CSV parameters</div><div class="kpi"><b>{s['generated']}</b>generated values</div>
<div class="kpi"><b>{cov_txt}</b>dynamic values resolved</div><div class="kpi"><b>{s['blockers']}</b>blockers</div>
<div class="kpi"><b>{'PASS' if (s.get('jmx_check') or {}).get('ok') else 'FAIL'}</b>JMX load checkpoint</div>
</div>
{jmx_block}
<h2>Needs attention</h2><ul>{flags}</ul>
{val}
<h2>Correlations (values returned by the server)</h2><table><tr><th>Variable</th><th>Extractor</th><th>Recorded value</th><th>Used in</th><th>Evidence</th></tr>{var_rows('correlation')}</table>
<h2>Parameters (CSV test data)</h2><table><tr><th>Variable</th><th>Source</th><th>Recorded value</th><th>Used in</th><th>Evidence</th></tr>{var_rows('parameter')}</table>
<h2>Generated by the app (recreated every iteration)</h2><table><tr><th>Variable</th><th>Function</th><th>Recorded value</th><th>Used in</th><th>Evidence</th></tr>{var_rows('generated')}</table>
<h2>Transactions and samplers</h2><table><tr><th>Transaction</th><th>Sampler</th><th>Method</th><th>URL</th><th>Recorded</th></tr>{rows_tx}</table>
<h2>Removed as noise</h2><table><tr><th>Method</th><th>URL</th><th>Reason</th></tr>{removed or "<tr><td colspan=3 class='muted'>None</td></tr>"}</table>
</main></body></html>"""
