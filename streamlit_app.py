"""MobPerf Studio - Streamlit UI.

Upload a mobile recording (HAR from the MobPerf MCP recorder, mitmproxy, Charles, Proxyman, Appetize...)
and get a correlated, parameterised, correctly named JMeter script that passes the JMX checkpoint.
"""
from __future__ import annotations

import io
import os
import zipfile

import pandas as pd
import streamlit as st

from engine.jmx_lint import lint
from engine.model import load_any
from engine.pipeline import Options, generate, how_to_run
from engine.report import build_report
from engine.rules import noise_rules

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

st.markdown("""<div class="hero"><h1>📱 MobPerf Studio</h1>
<p>Mobile emulator recording &rarr; ready-to-run JMeter script. Correlated, parameterised, named, and checked to open in the JMeter GUI.</p>
<span class="pill">Android Emulator</span><span class="pill">iOS Simulator</span><span class="pill">MCP recorder</span>
<span class="pill">JMeter 5.6</span><span class="pill">Record-twice diff</span></div>""", unsafe_allow_html=True)

page = st.sidebar.radio("Navigate", ["⚡ Generate script", "🩺 JMX checkpoint", "🎥 Record with emulator (MCP)", "ℹ️ How it works"])


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


# ================================================================== GENERATE
if page.startswith("⚡"):
    c1, c2 = st.columns(2)
    with c1:
        up1 = st.file_uploader("Recording 1 (user A)", type=["har", "json", "flow", "mitm"],
                               help="HAR from the MobPerf recorder, mitmproxy, Charles, Proxyman, Fiddler (export as HAR) or Appetize.")
    with c2:
        up2 = st.file_uploader("Recording 2 - same journey, different user (recommended)", type=["har", "json", "flow", "mitm"],
                               help="Values that change between the two recordings are proven dynamic, which makes correlation exact.")
    use_sample = st.toggle("Use the bundled sample (MobiBank - strict mock banking app, 2 recordings)", value=up1 is None)

    with st.expander("Settings", expanded=False):
        app_name = st.text_input("App name (used in transaction names)", value="MobiBank" if use_sample else "MyApp")
        cc1, cc2 = st.columns(2)
        with cc1:
            extra = st.text_area("Extra domains to exclude (one per line)", placeholder="analytics.mycompany.com")
            keep_static = st.checkbox("Keep static assets (.png, .js, .css ...)", value=False)
        with cc2:
            include = st.text_area("Only keep these hosts (optional, one per line)", placeholder="api.myapp.com")
            gap = st.slider("Without step markers: a quiet gap of N seconds starts a new transaction", 0.5, 10.0, 2.0, 0.5)
        st.caption(f"{len(noise_rules().get('domains', []))} SDK / telemetry domains are excluded by default (rules/sdk_noise.yaml).")
    if not use_sample:
        app_name = app_name if app_name != "MobiBank" else "MyApp"

    if st.button("🚀 Generate JMeter script", type="primary", width="stretch"):
        try:
            if use_sample and up1 is None:
                r1, r2 = _sample("mobibank_alice.har"), _sample("mobibank_bob.har")
            else:
                r1, r2 = _load(up1), _load(up2)
            if r1 is None:
                st.warning("Upload a recording or switch on the sample.")
                st.stop()
            opts = Options(app_name=app_name, extra_exclude=[x.strip() for x in extra.splitlines() if x.strip()],
                           include_hosts=[x.strip() for x in include.splitlines() if x.strip()],
                           keep_static=keep_static, step_gap_s=gap)
            st.session_state.update(res=generate(r1, r2, opts), recs=(r1, r2), opts=opts, csv_edit=None)
        except Exception as ex:  # surface parsing problems clearly
            st.error(f"Could not process the recording: {ex}")

    res = st.session_state.get("res")
    if res:
        s = res.summary
        m = st.columns(6)
        m[0].metric("Transactions", s["transactions"])
        m[1].metric("Requests", s["requests"], f"-{s['removed']} noise", delta_color="off")
        m[2].metric("Correlations", s["correlations"])
        m[3].metric("CSV params", s["parameters"])
        m[4].metric("Generated", s["generated"])
        m[5].metric("Dynamic resolved", f"{s['coverage_pct']:.0f}%" if s["coverage_pct"] is not None else "1 rec")
        _checkpoint_box(s["jmx_check"])
        for f in res.analysis.flags:
            (st.error if f.severity == "blocker" else st.warning)(
                (f"Request #{f.pos + 1}: " if f.pos is not None else "") + f.message)
        if s["coverage_pct"] is None:
            st.info("Tip: upload a second recording of the same journey with a different test user - "
                    "every changed value is then proven dynamic instead of inferred.")

        tabs = st.tabs(["Transactions", "Correlations", "Parameters / CSV", "Generated values", "Noise removed", "JMX", "Report"])
        with tabs[0]:
            st.caption("Rename steps here, then click Apply - transaction names follow MOB_<App>_<NN>_<Step>.")
            df = pd.DataFrame([{"NN": stp.nn, "Step": stp.name, "Requests": len(stp.positions), "Think time (ms)": stp.think_ms}
                               for stp in res.steps])
            ed = st.data_editor(df, hide_index=True, disabled=["NN", "Requests", "Think time (ms)"], width="stretch", key="steps_ed")
            if st.button("Apply step names"):
                ren = {o: n for o, n in zip(df["Step"], ed["Step"]) if o != n}
                if ren:
                    opts = st.session_state["opts"]
                    opts.step_renames = {**opts.step_renames, **ren}
                    r1, r2 = st.session_state["recs"]
                    st.session_state["res"] = generate(r1, r2, opts)
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
                base = st.session_state.get("csv_edit") or res.csv
                cdf = pd.read_csv(io.StringIO(base), dtype=str, keep_default_na=False)
                edited = st.data_editor(cdf, num_rows="dynamic", width="stretch", key="csv_ed")
                st.session_state["csv_edit"] = edited.to_csv(index=False)
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
            st.dataframe(pd.DataFrame([{"Method": e.method, "URL": e.url, "Reason": r} for e, r in res.removed]),
                         hide_index=True, width="stretch")
        with tabs[5]:
            st.code(res.jmx[:60000], language="xml")
        with tabs[6]:
            _show_html(res.report_html)

        csv_final = st.session_state.get("csv_edit") or res.csv
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
                           type="primary", width="stretch")
        d2.download_button("⬇️ .jmx only", res.jmx, jmx_n, "application/xml", width="stretch")
        if csv_final:
            d3.download_button("⬇️ CSV data", csv_final, csv_n, "text/csv", width="stretch")

# ================================================================== CHECKPOINT
elif page.startswith("🩺"):
    st.subheader("JMX checkpoint - will it open in the JMeter GUI?")
    st.write("Upload **any** .jmx (generated here or hand-made). The same 10 checks catch the usual causes of "
             "*Problem loading XML*, *CannotResolveClassException* and silent runtime failures.")
    up = st.file_uploader("JMeter script", type=["jmx", "xml"])
    if up:
        r = lint(up.getvalue().decode("utf-8", errors="replace"))
        _checkpoint_box(r.as_dict())
        st.caption("For a 100% guarantee also run locally: `python -m mcp_server.cli check <file.jmx>` - "
                   "it additionally loads the plan in your installed JMeter (0 users, nothing is sent).")

# ================================================================== RECORD
elif page.startswith("🎥"):
    st.subheader("Record on a free emulator with the MCP recorder")
    st.write("Streamlit Cloud cannot run an emulator or a proxy, so recording happens on your machine. "
             "Claude (or any MCP client) drives it with two MCP servers, then you upload the HAR here - "
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

# ================================================================== ABOUT
else:
    st.subheader("How the engine decides")
    st.markdown("""
For every value a request sends:

1. **Seen in an earlier response** → correlation `c_*` - JSON Extractor when it is a JSON field, otherwise a boundary Regex Extractor on the body or response headers (Location, Set-Cookie...).
2. **Created by the app** (UUID, timestamp, ISO date, random nonce) → generated `g_*`, recreated every iteration with JMeter functions.
3. **Typed / chosen by the user** (login, password, search, amount...) → CSV parameter `p_*`.
4. **Same in both recordings and not token-like** → left as a constant.

**Record twice** with different test users and step 4 stops being a guess: anything that changed is dynamic, and the
*Dynamic resolved* score tells you how many of those were handled.

**Honest limits** - flagged as blockers, never faked: on-device request signing (HMAC), device attestation (Play Integrity / App Attest),
real OTPs, gRPC / protobuf bodies, SSL-pinned apps (need a test build).

**Naming** - `MOB_<App>_<NN>_<Step>` transactions, `<NN>_<sub>_<METHOD>_<endpoint>` samplers, `c_` / `p_` / `g_` variables,
`host_*` variables overridable with `-J`, think time per step.

**Checkpoints** - the JMX checkpoint (10 structural checks + optional real JMeter load) and a 1 user × 2 iteration replay.
""")
