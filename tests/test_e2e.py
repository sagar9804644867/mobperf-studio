"""End-to-end proof: record through mitmproxy -> generate -> replay against a strict backend.

    python tests/test_e2e.py

Steps
  1. Start the strict MobiBank mock backend.
  2. Record the journey twice (alice, bob) through mitmdump + the MobPerf capture addon.
  3. Inject typical SDK noise (Firebase, Crashlytics, AppsFlyer, images) into recording 1.
  4. Generate the script from both recordings.
  5. Add a THIRD user (carol) to the CSV who was never recorded, and replay 3 iterations.
     Every request must pass: the backend rejects stale/replayed/foreign values.
  6. Negative control: replaying the raw recording without correlation must fail.
  7. Single-recording mode must also produce a passing script.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import requests  # noqa: E402

import mock_mobile_backend as backend  # noqa: E402
import mock_mobile_client as client  # noqa: E402
from engine.model import load_har  # noqa: E402
from engine.pipeline import Options, generate_files  # noqa: E402
from engine.validator import PyReplay  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def record(base, user, pwd, workdir):
    sess = os.path.join(workdir, f"session_{user}")
    os.makedirs(sess, exist_ok=True)
    port = free_port()
    proc = subprocess.Popen(["mitmdump", "-q", "--listen-host", "127.0.0.1", "-p", str(port), "-s",
                             os.path.join(ROOT, "mcp_server", "capture_addon.py"), "--set", f"mobperf_dir={sess}"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    try:
        client.run(base, f"http://127.0.0.1:{port}", sess, user, pwd)
        time.sleep(0.5)
    finally:
        proc.terminate()
        proc.wait(10)
    har = os.path.join(workdir, f"rec_{user}.har")
    shutil.copy(os.path.join(sess, "recording.har"), har)
    return har


def inject_noise(path):
    har = json.load(open(path))
    entries = har["log"]["entries"]
    t = entries[0]["startedDateTime"]
    def fake(url, method="POST"):
        return {"startedDateTime": t, "time": 20, "request": {"method": method, "url": url, "headers": [], "httpVersion": "HTTP/2"},
                "response": {"status": 200, "headers": [{"name": "content-type", "value": "application/json"}],
                             "content": {"mimeType": "application/json", "text": "{\"ok\":true,\"token\":\"abcdef1234567890XYZ\"}"}},
                "pageref": entries[0].get("pageref"), "_mobperf_step": entries[0].get("_mobperf_step")}
    noise = [fake("https://app-measurement.com/a"), fake("https://firebaseinstallations.googleapis.com/v1/projects/x/installations"),
             fake("https://firebase-settings.crashlytics.com/spi/v2/platforms/android/gmp/1:abc/settings", "GET"),
             fake("https://t.appsflyersdk.com/api/v6.5/androidevent?app_id=com.mobibank"),
             fake("https://cdn.mobibank.example/img/banner_diwali.png", "GET")]
    har["log"]["entries"] = entries[:2] + noise + entries[2:]
    json.dump(har, open(path, "w"))
    return len(noise)


def naive_replay(har_path, base_port):
    """Replays the recorded requests exactly as captured (what a recorder gives you before correlation)."""
    rec = load_har(open(har_path).read())
    s = requests.Session()
    s.trust_env = False
    fails = 0
    for e in rec.entries:
        if "127.0.0.1" not in e.url:
            continue
        hdrs = {k: v for k, v in e.req_headers if k.lower() not in ("host", "content-length", "connection")}
        r = s.request(e.method, e.url, data=e.req_body.encode() if e.req_body else None, headers=hdrs, allow_redirects=False)
        fails += r.status_code >= 400
    return fails


def check(cond, msg):
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        check.failed = True


check.failed = False


def main():
    port = free_port()
    srv = backend.serve(port)
    base = f"http://127.0.0.1:{port}"
    work = tempfile.mkdtemp(prefix="mobperf_e2e_")
    print(f"Mock backend {base}\nWork dir {work}\n")

    print("[1] Recording journey twice through mitmproxy + capture addon")
    h1 = record(base, "alice", "Alice@123", work)
    h2 = record(base, "bob", "Bob@123", work)
    n1 = len(json.load(open(h1))["log"]["entries"])
    check(n1 == 10, f"recording 1 captured {n1} requests with step markers")
    pages = [p["title"] for p in json.load(open(h1))["log"]["pages"]]
    check(pages == ["App Launch", "Login", "View Accounts", "View Statement", "Fund Transfer", "Confirm OTP"], f"steps captured: {pages}")
    nn = inject_noise(h1)
    shutil.copy(h1, os.path.join(ROOT, "samples", "mobibank_alice.har"))
    shutil.copy(h2, os.path.join(ROOT, "samples", "mobibank_bob.har"))

    print("\n[2] Generating script from both recordings")
    out = os.path.join(work, "out")
    res, paths = generate_files(h1, h2, out, Options(app_name="MobiBank"))
    s = res.summary
    print(f"  summary: {json.dumps({k: s[k] for k in ('transactions','requests','removed','correlations','parameters','generated','coverage_pct','blockers')})}")
    check(s["removed"] == nn, f"{s['removed']} SDK/static noise requests removed")
    check(s["requests"] == 10, "10 business requests scripted")
    check(s["coverage_pct"] == 100.0, f"dynamic value coverage {s['coverage_pct']}% ({s['dynamic_resolved']}/{s['dynamic_total']})")
    ET.fromstring(res.jmx)
    check(True, "JMX is well-formed XML")
    names = [st.label("MOB", "MobiBank", "{prefix}_{app}_{nn:02d}_{step}") for st in res.steps]
    check(names[0] == "MOB_MobiBank_01_AppLaunch" and names[-1] == "MOB_MobiBank_06_ConfirmOTP", f"transactions: {names}")
    for v in res.analysis.variables:
        how = v.extractor["expr"] if v.extractor else (v.generator or ",".join(v.csv_values))
        print(f"    {v.kind:<11} {v.name:<22} {how[:70]}")
    for f in res.analysis.flags:
        print(f"    FLAG {f.severity}: {f.message[:110]}")

    print("\n[3] Replaying generated JMX - 3 iterations incl. a user never recorded (carol)")
    csv_text = open(paths["csv"]).read().rstrip("\n").split("\n")
    header = csv_text[0].split(",")
    carol = []
    for col in header:
        carol.append({"p_username": "carol", "p_password": "Carol@123"}.get(col, csv_text[1].split(",")[header.index(col)]))
    csv_full = "\n".join(csv_text + [",".join(carol)]) + "\n"
    open(paths["csv"], "w").write(csv_full)
    before_err = len(backend.S.errors)
    rr = PyReplay(open(paths["jmx"]).read(), csv_full, {"think_ms": "0"}).run(3)
    bad = [r for r in rr["results"] if not r["ok"]]
    for r in bad:
        print("    failed:", r["sampler"], r["status"], r["error"], r.get("response_snippet", "")[:120])
    check(rr["passed"] and len(rr["results"]) == 30, f"{len(rr['results'])} samples, {len(bad)} failures")
    check(len(backend.S.errors) == before_err, "backend logged zero rejected requests during replay")

    print("\n[4] Negative control - raw recording replayed without correlation")
    fails = naive_replay(h1, port)
    check(fails >= 3, f"uncorrelated replay rejected {fails} requests (backend is strict)")

    print("\n[5] Single recording mode (no second recording)")
    res1, p1 = generate_files(h2, None, os.path.join(work, "out_single"), Options(app_name="MobiBank"))
    rr1 = PyReplay(open(p1["jmx"]).read(), open(p1["csv"]).read() if "csv" in p1 else "", {"think_ms": "0"}).run(2)
    bad1 = [r for r in rr1["results"] if not r["ok"]]
    for r in bad1:
        print("    failed:", r["sampler"], r["status"], r["error"], r.get("response_snippet", "")[:120])
    check(rr1["passed"], f"single-recording script: {len(rr1['results'])} samples, {len(bad1)} failures")

    shutil.copy(paths["jmx"], os.path.join(ROOT, "samples", "MOB_MobiBank.jmx"))
    shutil.copy(paths["csv"], os.path.join(ROOT, "samples", "MOB_MobiBank_data.csv"))
    shutil.copy(paths["report"], os.path.join(ROOT, "samples", "MOB_MobiBank_report.html"))
    srv.shutdown()
    print("\nRESULT:", "FAILED" if check.failed else "ALL CHECKS PASSED")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
