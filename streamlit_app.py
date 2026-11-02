"""MobPerf Studio - Streamlit UI.

Two sections, one engine:
  * Mobile app  - recording from an Android Emulator / iOS Simulator (MCP recorder, mitmproxy, Charles, Proxyman...)
  * Web / API   - HAR from browser DevTools, the bundled Playwright recorder, Postman, Fiddler, any HTTP proxy
Both produce a correlated, parameterised, correctly named JMeter script with status and response-body checks,
and both pass the JMX checkpoint before you download.
"""
from __future__ import annotations

import io
import json
import os
import zipfile

import pandas as pd
import streamlit as st

from engine.jmx_lint import lint
from engine.model import load_any
from engine.pipeline import Options, generate, how_to_run
from engine.report import build_report
from engine.rules import assertion_rules, noise_rules

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")

st.set_page_config(page_title="MobPerf Studio", page_icon="📱", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.6rem;max-width:1250px}
.hero{background:linear-gradient(120deg,#0b1b3a 0%,#1463ff 70%,#3fa9ff 100%);color:#fff;border-radius:16px;padding:22px 26px;margin-bottom:18px}
.hero h1{color:#fff;font-size:30px;margin:0;letter-spacing:.5px}
.hero p{margin:6px 0 0;opacity:.9}
.pill{display:inline-block;background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.3);border-radius:999px;padding:2px 10px;margin:8px 6px 0 0;font-size:12px}
.ok{background:#e7f7ef;border:1px solid #9bd8bb;border-radius:10px;padding:10px 14px;color:#0a6b45}
.bad{background:#fdecec;border:1px solid #f1a8a8;border-radius:10px;padding:10px 14px;color:#9b1c1c}
div[data-testid="stMetricValue"]{color:#1463ff}
</style>""", unsafe_allow_html=True)

st.markdown("""<div class="hero"><h1>📱🌐 MobPerf Studio</h1>
<p>Mobile or web recording &rarr; ready-to-run JMeter script. Correlated, parameterised, named, body-checked,
and verified to open in the JMeter GUI.</p>
<span class="pill">Android Emulator</span><span class="pill">iOS Simulator</span><span class="pill">Web &amp; API (HAR)</span>
<span class="pill">MCP recorder</span><span class="pill">JMeter 5.6</span><span class="pill">Record-twice diff</span></div>""",
            unsafe_allow_html=True)

PAGES = {
    "mobile": "📱 Mobile app - generate script",
    "web": "🌐 Web / API app - generate script",
    "check": "🩺 JMX checkpoint",
    "rec_mobile": "🎥 Record mobile (emulator + MCP)",
    "rec_web": "🧭 Record web app (browser)",
    "about": "ℹ️ How it works",
}
st.sidebar.markdown("**Mobile performance testing**")
st.sidebar.caption("Android Emulator / iOS Simulator recordings")
st.sidebar.markdown("**Web / API performance testing**")
st.sidebar.caption("Browser, SPA, REST, GraphQL, SOAP recordings")
page_label = st.sidebar.radio("Navigate", list(PAGES.values()))
page = next(k for k, v in PAGES.items() if v == page_label)

KIND = {
    "mobile": {"sample": ("mobibank_alice.har", "mobibank_bob.har"), "sample_label": "MobiBank - strict mock banking app",
               "app": "MobiBank", "prefix": "MOB",
               "help1": "HAR from the MobPerf recorder, mitmproxy, Charles, Proxyman, Fiddler (export as HAR) or Appetize.",
               "label1": "Recording 1 (user A)"},
    "web": {"sample": ("shopweb_alice.har", "shopweb_bob.har"), "sample_label": "ShopWeb - strict mock web shop (CSRF, cookie session, token in HTML)",
            "app": "ShopWeb", "prefix": "WEB",
            "help1": "HAR from Chrome / Edge / Firefox DevTools (Network tab - Save all as HAR with content), the bundled "
                     "Playwright recorder, Postman / Insomnia (HAR export), Fiddler, Charles or any HTTP proxy.",
            "label1": "Recording 1 (user A)"},
}


# ------------------------------------------------------------------ helpers
def _load(upload):
    if upload is None:
        return None
    rec = load_any(upload.name, upload.getvalue())
    rec.source = upload.name
    return rec


def _sample(name):
    with open(os.path.join(SAMPLES, name), "rb") as fh:
        rec = load_any(name, fh.read())
    rec.source = name
    return rec


def _show_html(html: str, height: int = 900):
    if hasattr(st, "iframe"):  # Streamlit >= 1.52
        import base64
        st.iframe("data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode(), height=height)
    else:
        import streamlit.components.v1 as components
        components.html(html, height=height, scrolling=True)


def _checkpoint_box(jc: dict):
    if jc["ok"]:
        st.markdown("<div class='ok'><b>✅ JMX checkpoint passed</b> - the script is structurally valid and will open in the JMeter GUI.</div>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<div class='bad'><b>❌ JMX checkpoint failed</b> - JMeter would not load this file:</div>", unsafe_allow_html=True)
        for e in jc["errors"]:
            st.error(e)
    with st.expander("Checkpoint details (10 checks)"):
        st.dataframe(pd.DataFrame([{"Code": c["code"], "Check": c["check"], "Result": "PASS" if c["passed"] else "FAIL"}
                                   for c in jc["checks"]]), hide_index=True, width="stretch")
        for w in jc.get("warnings", []):
            st.caption("⚠️ " + w)


def _lines(text: str):
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


# ================================================================== GENERATE (shared by both sections)
def generator_page(kind: str):
    cfg = KIND[kind]
    ss = f"{kind}_"  # separate session state per section, so switching sections keeps both results
    if kind == "mobile":
        st.subheader("📱 Mobile app → JMeter script")
        st.caption("Recording from an Android Emulator or iOS Simulator. Transactions are named MOB_<App>_<NN>_<Step>.")
    else:
        st.subheader("🌐 Web / API application → JMeter script")
        st.caption("Any browser app, single-page app or API recording saved as HAR. Transactions are named WEB_<App>_<NN>_<Step>. "
                   "Tip: in DevTools tick *Preserve log*, then right-click the request list → *Save all as HAR with content*.")
    c1, c2 = st.columns(2)
    with c1:
        up1 = st.file_uploader(cfg["label1"], type=["har", "json", "flow", "mitm"], help=cfg["help1"], key=ss + "up1")
    with c2:
        up2 = st.file_uploader("Recording 2 - same journey, different user (recommended)", type=["har", "json", "flow", "mitm"],
                               help="Values that change between the two recordings are proven dynamic, which makes correlation exact.",
                               key=ss + "up2")
    use_sample = st.toggle(f"Use the bundled sample ({cfg['sample_label']}, 2 recordings)", value=up1 is None, key=ss + "sample")

    with st.expander("Settings", expanded=False):
        app_name = st.text_input("App name (used in transaction names)", value=cfg["app"] if use_sample else "MyApp", key=ss + "app")
        cc1, cc2 = st.columns(2)
        with cc1:
            extra = st.text_area("Extra domains to exclude (one per line)", placeholder="analytics.mycompany.com", key=ss + "extra")
            keep_static = st.checkbox("Keep static assets (.png, .js, .css ...)", value=False, key=ss + "static",
                                      help="Off by default: JMeter can fetch embedded resources itself, and CDNs are not your system under test.")
        with cc2:
            include = st.text_area("Only keep these hosts (optional, one per line)", placeholder="api.myapp.com", key=ss + "include")
            gap = st.slider("Without step markers: a quiet gap of N seconds starts a new transaction", 0.5, 10.0, 2.0, 0.5, key=ss + "gap")
        st.caption(f"{len(noise_rules().get('domains', []))} SDK / telemetry / ad domains are excluded by default (rules/sdk_noise.yaml).")
        st.markdown("**Workload realism** - baked into the script as defaults; every one can still be overridden with `-J` at run time")
        w1, w2 = st.columns(2)
        with w1:
            think_mode = st.radio("Think time", ["Recorded per step", "Fixed for every step"], horizontal=True, key=ss + "think_mode",
                                  help="Recorded = the pause the user really made before the next step.")
            think_fixed = st.number_input("Fixed think time (ms)", 0, 600000, 3000, 500, key=ss + "think_fixed",
                                          disabled=think_mode.startswith("Recorded"))
            think_dev = st.slider("Random variation ± %", 0, 100, 20, 5, key=ss + "think_dev",
                                  help="20 = each pause is a random value between 80% and 120% of the think time, "
                                       "so virtual users do not hit the server in lock-step. 0 = exact.")
        with w2:
            pacing_s = st.number_input("Pacing: minimum seconds per iteration (0 = off)", 0, 3600, 0, 5, key=ss + "pacing",
                                       help="Each user waits for whatever time is left so one iteration takes at least this long. "
                                            "Throughput per user = 1 iteration per pacing interval. Example: 60 s x 50 users = 50 iterations/min.")
            data_mode = st.radio("Test data (CSV)", ["Shared - reuse rows", "Unique - each row once"], key=ss + "data_mode",
                                 help="Shared: rows are handed out in turn and reused when the file ends. "
                                      "Unique: every row is used once across all users; a user stops when the rows run out "
                                      "(provide at least users x loops rows).")
        st.markdown("**Response body checks** - fail a request when its response *contains* any of these, even with HTTP 200")
        body_on = st.checkbox("Add body checks (recommended)", value=True, key=ss + "body_on")
        pats = st.text_area("Fail if response contains (one per line, case-sensitive)",
                            value="\n".join(assertion_rules().get("fail_patterns", [])), height=170, key=ss + "pats",
                            disabled=not body_on)
        st.caption("A text is only checked on requests whose recorded response did NOT already contain it, "
                   "so a page that normally says 'Unauthorized access is logged' is not flagged.")

    if st.button("🚀 Generate JMeter script", type="primary", width="stretch", key=ss + "go"):
        try:
            if use_sample and up1 is None:
                r1, r2 = _sample(cfg["sample"][0]), _sample(cfg["sample"][1])
            else:
                r1, r2 = _load(up1), _load(up2)
            if r1 is None:
                st.warning("Upload a recording or switch on the sample.")
                st.stop()
            opts = Options(app_name=app_name, extra_exclude=_lines(extra), include_hosts=_lines(include),
                           keep_static=keep_static, step_gap_s=gap, kind=kind,
                           fail_patterns=_lines(pats) if body_on else [],
                           think_ms_fixed=None if think_mode.startswith("Recorded") else int(think_fixed),
                           think_dev_pct=int(think_dev), pacing_ms=int(pacing_s) * 1000,
                           data_mode="unique" if data_mode.startswith("Unique") else "shared")
            st.session_state.update({ss + "res": generate(r1, r2, opts), ss + "recs": (r1, r2), ss + "opts": opts,
                                     ss + "csv_edit": None})
        except Exception as ex:  # surface parsing problems clearly
            st.error(f"Could not process the recording: {ex}")

    res = st.session_state.get(ss + "res")
    if not res:
        return
    s = res.summary
    m = st.columns(7)
    m[0].metric("Transactions", s["transactions"])
    m[1].metric("Requests", s["requests"], f"-{s['removed']} noise", delta_color="off")
    m[2].metric("Correlations", s["correlations"])
    m[3].metric("CSV params", s["parameters"])
    m[4].metric("Generated", s["generated"])
    m[5].metric("Body checks", s.get("body_checks", 0))
    m[6].metric("Dynamic resolved", f"{s['coverage_pct']:.0f}%" if s["coverage_pct"] is not None else "1 rec")
    st.caption(f"⏱️ Think time: **{s.get('think')} ± {s.get('think_dev_pct')}%** · "
               f"Pacing: **{str(s.get('pacing_ms') // 1000) + ' s per iteration' if s.get('pacing_ms') else 'off'}** · "
               f"Test data: **{'unique (each row once)' if s.get('data_mode') == 'unique' else 'shared (rows reused)'}** · "
               f"override at run time with -Jthink_ms, -Jthink_dev_pct, -Jpacing_ms")
    _checkpoint_box(s["jmx_check"])
    for f in res.analysis.flags:
        (st.error if f.severity == "blocker" else st.warning)(
            (f"Request #{f.pos + 1}: " if f.pos is not None else "") + f.message)
    if s["coverage_pct"] is None:
        st.info("Tip: upload a second recording of the same journey with a different test user - "
                "every changed value is then proven dynamic instead of inferred.")

    tabs = st.tabs(["Transactions", "Correlations", "Parameters / CSV", "Generated values", "Assertions", "Noise removed", "JMX", "Report"])
    with tabs[0]:
        st.caption(f"Rename steps here, then click Apply - transaction names follow {res.prefix}_<App>_<NN>_<Step>.")
        df = pd.DataFrame([{"NN": stp.nn, "Step": stp.name, "Requests": len(stp.positions), "Think time (ms)": stp.think_ms}
                           for stp in res.steps])
        ed = st.data_editor(df, hide_index=True, disabled=["NN", "Requests", "Think time (ms)"], width="stretch", key=ss + "steps_ed")
        if st.button("Apply step names", key=ss + "apply"):
            ren = {o: n for o, n in zip(df["Step"], ed["Step"]) if o != n}
            if ren:
                opts = st.session_state[ss + "opts"]
                opts.step_renames = {**opts.step_renames, **ren}
                r1, r2 = st.session_state[ss + "recs"]
                st.session_state[ss + "res"] = generate(r1, r2, opts)
                st.rerun()
        st.dataframe(pd.DataFrame(res.samplers)[["transaction", "sampler", "method", "status", "url"]],
                     hide_index=True, width="stretch")
    with tabs[1]:
        rows = [{"Variable": v.name, "Type": v.extractor["type"].upper(), "From": v.extractor["where"],
                 "Expression": v.extractor["expr"], "Match": v.extractor.get("match", 1),
                 "Response #": v.producer + 1, "Used in": len(v.used_in), "Recorded value": v.value[:60], "Evidence": v.evidence}
                for v in res.analysis.variables if v.kind == "correlation"]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.write("None")
    with tabs[2]:
        if res.csv:
            st.caption("Add one row per virtual user (real test accounts). This edited CSV goes into the download.")
            base = st.session_state.get(ss + "csv_edit") or res.csv
            cdf = pd.read_csv(io.StringIO(base), dtype=str, keep_default_na=False)
            edited = st.data_editor(cdf, num_rows="dynamic", width="stretch", key=ss + "csv_ed")
            st.session_state[ss + "csv_edit"] = edited.to_csv(index=False)
        else:
            st.write("No user-input values detected.")
    with tabs[3]:
        rows = [{"Variable": v.name, "Function": v.generator, "Recorded value": v.value, "Evidence": v.evidence}
                for v in res.analysis.variables if v.kind == "generated"]
        if rows:
            st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        else:
            st.write("None")
    with tabs[4]:
        st.caption("Every request gets a Response Code assertion (the recorded status). Body checks fail the request when the "
                   "response contains error text - this catches HTTP 200 responses that carry 'token expired', '\"error\":' and similar. "
                   "Correlated values are checked too: each extracted variable gets a variable-scope Response Assertion, "
                   "so if a correlation finds nothing (value <var>_NOT_FOUND) the request that should have produced it fails in JMeter.")
        st.dataframe(pd.DataFrame([{"Sampler": r["sampler"], "Status check": r["status"],
                                    "Fails if body contains": r.get("body_check") or "-"} for r in res.samplers]),
                     hide_index=True, width="stretch")
    with tabs[5]:
        st.dataframe(pd.DataFrame([{"Method": e.method, "URL": e.url, "Reason": r} for e, r in res.removed]),
                     hide_index=True, width="stretch")
    with tabs[6]:
        st.code(res.jmx[:60000], language="xml")
    with tabs[7]:
        _show_html(res.report_html)

    csv_final = st.session_state.get(ss + "csv_edit") or res.csv
    jmx_n, csv_n, rep_n = res.file_names()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(jmx_n, res.jmx)
        if csv_final:
            z.writestr(csv_n, csv_final)
        z.writestr(rep_n, build_report(res))
        z.writestr("HOW_TO_RUN.txt", how_to_run(res))
    d1, d2, d3 = st.columns(3)
    d1.download_button("⬇️ Download bundle (.zip)", buf.getvalue(), f"{res.base}_bundle.zip", "application/zip",
                       type="primary", width="stretch", key=ss + "dl_zip")
    d2.download_button("⬇️ .jmx only", res.jmx, jmx_n, "application/xml", width="stretch", key=ss + "dl_jmx")
    if csv_final:
        d3.download_button("⬇️ CSV data", csv_final, csv_n, "text/csv", width="stretch", key=ss + "dl_csv")


if page in ("mobile", "web"):
    generator_page(page)

# ================================================================== CHECKPOINT
elif page == "check":
    st.subheader("JMX checkpoint - will it open in the JMeter GUI?")
    st.write("Upload **any** .jmx (generated here or hand-made, mobile or web). The same 10 checks catch the usual causes of "
             "*Problem loading XML*, *CannotResolveClassException* and silent runtime failures.")
    up = st.file_uploader("JMeter script", type=["jmx", "xml"])
    if up:
        r = lint(up.getvalue().decode("utf-8", errors="replace"))
        _checkpoint_box(r.as_dict())
        st.caption("For a 100% guarantee also run locally: `python -m mcp_server.cli check <file.jmx>` - "
                   "it additionally loads the plan in your installed JMeter (0 users, nothing is sent).")

# ================================================================== RECORD MOBILE
elif page == "rec_mobile":
    st.subheader("Record on a free emulator with the MCP recorder")
    st.write("Streamlit Cloud cannot run an emulator or a proxy, so recording happens on your machine. "
             "Claude (or any MCP client) drives it with two MCP servers, then you upload the HAR in the Mobile section - "
             "or generate the script locally with the same engine.")
    st.markdown("""
| Platform | Free emulator | Runs on |
|---|---|---|
| Android | Android Emulator (Android Studio / SDK) - use a **Google APIs** image | Windows, macOS, Linux |
| iOS | Xcode iOS Simulator - needs a **simulator build (.app)** | macOS only |
""")
    st.markdown("**1. Install (once)**")
    st.code("git clone https://github.com/<your-user>/mobperf-studio.git\ncd mobperf-studio\npip install -r requirements-local.txt", language="bash")
    st.markdown("**2. Add the MCP servers to Claude Code**")
    st.code("claude mcp add mobperf -- python -m mcp_server.server\nclaude mcp add mobile-mcp -- npx -y @mobilenext/mobile-mcp@latest", language="bash")
    st.markdown("**3. Ask Claude**")
    st.code('Record the MobiBank app (com.mobibank) on my Pixel_7 AVD: Launch, Login, View Statement, Fund Transfer.\n'
            'Record it twice with users alice and bob, then generate the JMeter script and run the checkpoints.', language="text")
    st.markdown("**MCP tools**: environment_check · list_devices · start_android_emulator · boot_ios_simulator · start_recording · "
                "configure_device_proxy · install_ca_cert · install_app · launch_app · **mark_step** · recording_status · "
                "detect_pinning · stop_recording · clear_device_proxy · generate_script · **check_jmx** · validate_script")
    with open(os.path.join(ROOT, "skills", "mobile-perf-testing", "SKILL.md"), encoding="utf-8") as fh:
        st.download_button("⬇️ Claude skill (SKILL.md)", fh.read(), "SKILL.md", "text/markdown")

# ================================================================== RECORD WEB
elif page == "rec_web":
    st.subheader("Record a web application")
    st.markdown("""
**Option A - your browser (nothing to install)**
1. Open the site in Chrome / Edge / Firefox, press **F12** → **Network** tab, tick **Preserve log**.
2. Do the journey once as user A. Right-click the request list → **Save all as HAR with content**.
3. Clear, sign out, and repeat as user B → second HAR.
4. Upload both in **🌐 Web / API app**. Step names come from quiet gaps (adjust the slider) - rename them in the Transactions tab.

**Option B - the bundled Playwright recorder (named steps, repeatable)**
Write the journey once as a scenario; every `step` becomes a transaction. Run it twice with different users.
""")
    sample = [{"step": "Login"}, {"goto": "https://shop.example.com/login"},
              {"fill": ["input[name=user]", "alice"]}, {"fill": ["input[name=pass]", "Alice@123"]},
              {"click": "button[type=submit]"}, {"step": "Search"}, {"fill": ["input[name=q]", "shoes"]},
              {"press": "Enter"}, {"step": "View orders"}, {"goto": "https://shop.example.com/orders"}]
    scen = st.text_area("scenario_alice.json", json.dumps(sample, indent=2), height=300)
    try:
        from engine.web_recorder import validate_scenario
        validate_scenario(json.loads(scen))
        st.success("Scenario is valid.")
    except Exception as ex:
        st.error(f"Scenario problem: {ex}")
    st.download_button("⬇️ Download scenario", scen, "scenario_alice.json", "application/json")
    st.code("pip install -r requirements-local.txt\npython -m playwright install chromium\n\n"
            "python -m engine.web_recorder scenario_alice.json shop_alice.har\n"
            "python -m engine.web_recorder scenario_bob.json   shop_bob.har\n\n"
            "python -m mcp_server.cli generate shop_alice.har shop_bob.har --app Shop --type web --validate",
            language="bash")
    st.caption("Actions: step · goto · click · fill [selector, value] · press · wait_ms.  "
               "Add --headful to watch the browser, --ignore-https-errors for test sites with self-signed certificates.")
    st.markdown("**Option C - API only**: export a Postman / Insomnia collection run as HAR, or record through any HTTP proxy "
                "(Fiddler, Charles, mitmproxy) - same upload.")

# ================================================================== ABOUT
else:
    st.subheader("How the engine decides")
    st.markdown("""
The **Mobile** and **Web / API** sections share one engine. Only the recording source and the name prefix differ.

For every value a request sends:

1. **Seen in an earlier response** → correlation `c_*` - JSON Extractor when it is a JSON field, otherwise a boundary Regex Extractor on the body or response headers (HTML hidden fields, meta tags, Location, Set-Cookie...).
2. **Created by the client** (UUID, timestamp, ISO date, random nonce) → generated `g_*`, recreated every iteration with JMeter functions.
3. **Typed / chosen by the user** (login, password, search, amount...) → CSV parameter `p_*`.
4. **Same in both recordings and not token-like** → left as a constant.

**Record twice** with different test users and step 4 stops being a guess: anything that changed is dynamic, and the
*Dynamic resolved* score tells you how many of those were handled.

**Assertions** - every request checks the recorded status code **and** fails when the response body contains error text
(`"error":"`, `token expired`, `Unauthorized`... - editable). A text that was already in the recorded response is not
checked on that request, so normal content does not cause false failures. Correlations are checked too: an extractor that
finds nothing fails validation.

**Honest limits** - flagged as blockers, never faked: client-side request signing (HMAC), device attestation (Play Integrity / App Attest),
CAPTCHA, real OTPs, gRPC / protobuf bodies, WebSockets, SSL-pinned apps (need a test build).

**Naming** - `MOB_<App>_<NN>_<Step>` (mobile) or `WEB_<App>_<NN>_<Step>` (web) transactions,
`<NN>_<sub>_<METHOD>_<endpoint>` samplers, `c_` / `p_` / `g_` variables, `host_*` variables overridable with `-J`, think time per step.

**Workload** - think time per step (recorded or fixed) with random ± % variation, optional pacing (minimum iteration
time), and a test-data mode: shared rows or unique rows (each used once, the user stops when they run out).
Think time is excluded from transaction response times.

**Checkpoints** - the JMX checkpoint (10 structural checks + optional real JMeter load) and a 1 user × 2 iteration replay.
""")
