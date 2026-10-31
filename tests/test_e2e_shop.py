"""Second app: GraphQL + XML + ETag + ISO time + signature flag, recorded WITHOUT step markers.

    python tests/test_e2e_shop.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import mock_shop_app as shop  # noqa: E402
from engine.pipeline import Options, generate_files  # noqa: E402
from engine.validator import PyReplay  # noqa: E402
from test_e2e import check, free_port  # noqa: E402
import test_e2e  # noqa: E402


def record(base, email, pwd, work):
    sess = os.path.join(work, "s_" + email.split("@")[0])
    port = free_port()
    proc = subprocess.Popen(["mitmdump", "-q", "--listen-host", "127.0.0.1", "-p", str(port), "-s",
                             os.path.join(ROOT, "mcp_server", "capture_addon.py"), "--set", f"mobperf_dir={sess}"])
    time.sleep(2.5)
    try:
        shop.run_client(base, f"http://127.0.0.1:{port}", email, pwd)
        time.sleep(0.5)
    finally:
        proc.terminate()
        proc.wait(10)
    har = json.load(open(os.path.join(sess, "recording.har")))
    # behave like a HAR exported from Charles/Proxyman: no pages, no step markers
    har["log"]["pages"] = []
    for e in har["log"]["entries"]:
        e.pop("pageref", None)
        e.pop("_mobperf_step", None)
    out = os.path.join(work, f"shop_{email.split('@')[0]}.har")
    json.dump(har, open(out, "w"))
    return out


def main():
    port = free_port()
    srv = shop.serve(port)
    base = f"http://127.0.0.1:{port}"
    work = tempfile.mkdtemp(prefix="mobperf_shop_")
    print("[1] Recording ShopKart twice (no step markers)")
    h1 = record(base, "riya@shop.test", "Riya#2026", work)
    h2 = record(base, "aman@shop.test", "Aman#2026", work)
    shutil.copy(h1, os.path.join(ROOT, "samples", "shopkart_riya_nomarkers.har"))
    shutil.copy(h2, os.path.join(ROOT, "samples", "shopkart_aman_nomarkers.har"))

    print("[2] Generating")
    res, paths = generate_files(h1, h2, os.path.join(work, "out"), Options(app_name="ShopKart"))
    names = [st.name for st in res.steps]
    print("  steps:", names)
    print("  samplers:", [r["sampler"] for r in res.samplers])
    for v in res.analysis.variables:
        how = v.extractor["expr"] if v.extractor else (v.generator or ",".join(map(str, v.csv_values)))
        print(f"    {v.kind:<11} {v.name:<22} {how[:70]}")
    for f in res.analysis.flags:
        print(f"    FLAG {f.severity}: {f.message[:100]}")
    check(len(res.steps) == 4, f"gap-based detection found {len(res.steps)} steps (expected 4)")
    check(any("Login" in n for n in names) and any("Search" in n for n in names), "steps named from GraphQL operations / paths")
    check(any(f.severity == "blocker" and "X-Signature" in f.message for f in res.analysis.flags), "on-device X-Signature flagged as blocker")
    kinds = {v.name: v for v in res.analysis.variables}
    check(any(v.extractor and v.extractor["where"] == "body" and "checkoutToken" in v.extractor["expr"] for v in kinds.values()),
          "checkout token extracted from XML body")
    check(any(v.generator and "UPPER" in v.generator for v in kinds.values()), "uppercase UUID idempotency key generated")
    check(any(v.generator and "yyyy-MM-dd" in v.generator for v in kinds.values()), "ISO request time generated")

    print("[3] Replay 3 iterations incl. never-recorded user neha")
    lines = open(paths["csv"]).read().rstrip("\n").split("\n")
    hdr = lines[0].split(",")
    import csv as _csv, io as _io
    first = next(_csv.reader(_io.StringIO(lines[1])))
    row = [{"p_email": "neha@shop.test", "p_password": "Neha#2026"}.get(c, first[i]) for i, c in enumerate(hdr)]
    buf = _io.StringIO()
    _csv.writer(buf, lineterminator="\n").writerow(row)
    csv_full = "\n".join(lines) + "\n" + buf.getvalue()
    before = len(shop.St.errors)
    rr = PyReplay(open(paths["jmx"]).read(), csv_full, {"think_ms": "0"}).run(3)
    bad = [r for r in rr["results"] if not r["ok"]]
    for r in bad:
        print("    failed:", r["sampler"], r["status"], r["error"], r.get("response_snippet", "")[:150])
    check(rr["passed"], f"{len(rr['results'])} samples, {len(bad)} failures")
    check(len(shop.St.errors) == before, "backend rejected nothing during replay")
    srv.shutdown()
    print("\nRESULT:", "FAILED" if test_e2e.check.failed else "ALL CHECKS PASSED")
    return 1 if test_e2e.check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
