"""Real-world HAR quirks from browsers, proxies and API tools must never crash the engine or produce a JMX
that JMeter cannot open.

    python tests/test_har_robustness.py
"""
from __future__ import annotations

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.jmx_lint import lint  # noqa: E402
from engine.model import load_any  # noqa: E402
from engine.pipeline import Options, generate  # noqa: E402

failed = False


def check(cond, msg):
    global failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    failed |= not cond


def entry(method, url, status=200, req_headers=None, post=None, resp_text="", mime="application/json", t="2026-11-02T10:00:00.000Z",
          resp_headers=None, encoding=None, http="HTTP/1.1"):
    e = {"startedDateTime": t, "time": 42,
         "request": {"method": method, "url": url, "httpVersion": http, "headers": req_headers or [], "queryString": [],
                     "cookies": [], "headersSize": -1, "bodySize": -1},
         "response": {"status": status, "statusText": "", "httpVersion": http, "headers": resp_headers or [], "cookies": [],
                      "content": {"size": len(resp_text), "mimeType": mime, "text": resp_text}, "redirectURL": "",
                      "headersSize": -1, "bodySize": -1},
         "cache": {}, "timings": {"send": 1, "wait": 40, "receive": 1}}
    if encoding:
        e["response"]["content"]["encoding"] = encoding
    if post is not None:
        e["request"]["postData"] = post
    return e


def har(entries, pages=None):
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "test", "version": "1"}, "pages": pages or [],
                               "entries": entries}}).encode()


def run(name, data, rec2=None, kind="web", expect_requests=None):
    try:
        r1 = load_any(name + ".har", data)
        r2 = load_any(name + "_2.har", rec2) if rec2 else None
        res = generate(r1, r2, Options(app_name="Robust", kind=kind))
        lr = lint(res.jmx)
        ok = lr.ok and (expect_requests is None or res.summary["requests"] == expect_requests)
        check(ok, f"{name}: requests={res.summary['requests']} removed={res.summary['removed']} "
                  f"JMX {'valid' if lr.ok else 'INVALID ' + '; '.join(lr.errors)[:200]}")
        return res
    except Exception as ex:  # a crash is a failure
        check(False, f"{name}: crashed with {type(ex).__name__}: {ex}")


def main():
    api = "https://api.example.com"
    tok = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlMTIz"
    login = entry("POST", api + "/v1/auth/login", req_headers=[{"name": ":authority", "value": "api.example.com"},
                                                               {"name": ":method", "value": "POST"},
                                                               {"name": ":path", "value": "/v1/auth/login"},
                                                               {"name": "content-type", "value": "application/json"}],
                  post={"mimeType": "application/json", "text": json.dumps({"username": "alice", "password": "pw"})},
                  resp_text=json.dumps({"access_token": tok}), http="h2")
    print("HTTP/2 pseudo-headers (Chrome DevTools)")
    res = run("h2_pseudo_headers", har([login, entry("GET", api + "/v1/me", req_headers=[
        {"name": ":authority", "value": "api.example.com"}, {"name": "authorization", "value": "Bearer " + tok}],
        resp_text='{"name":"alice"}', http="h2")]), expect_requests=2)
    if res:
        check('name="Header.name">:authority' not in res.jmx and ">:method<" not in res.jmx,
              "pseudo-headers (:authority, :method, :path) are not sent as real headers")

    print("Bodies")
    run("base64_response", har([entry("GET", api + "/v1/data", resp_text=base64.b64encode(b'{"id": 7}').decode(), encoding="base64")]), expect_requests=1)
    run("form_params_without_text", har([entry("POST", api + "/login", post={"mimeType": "application/x-www-form-urlencoded",
                                                                              "params": [{"name": "user", "value": "alice"}, {"name": "pass", "value": "x"}]},
                                                resp_text="ok", mime="text/plain")]), expect_requests=1)
    run("binary_image_and_pdf", har([entry("GET", api + "/v1/statement.pdf", resp_text=base64.b64encode(bytes(range(256))).decode(),
                                           mime="application/pdf", encoding="base64"),
                                     entry("POST", api + "/v1/upload", post={"mimeType": "application/octet-stream", "text": "\x00\x01\x02binary"},
                                           resp_text='{"ok":true}')]))
    run("non_utf8_and_control_chars", har([entry("POST", api + "/v1/note", post={"mimeType": "text/plain", "text": "café \x0b\x1f ￾"},
                                                 resp_text="résumé \x07", mime="text/plain")]))
    run("graphql_batch_array", har([entry("POST", api + "/graphql", post={"mimeType": "application/json", "text": json.dumps(
        [{"operationName": "A", "query": "query A { a }"}, {"operationName": "B", "query": "query B { b }"}])}, resp_text='[{"data":{}},{"data":{}}]')]))
    run("soap_xml", har([entry("POST", api + "/ws/Account", req_headers=[{"name": "SOAPAction", "value": "getBalance"}],
                               post={"mimeType": "text/xml", "text": '<?xml version="1.0"?><soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><getBalance><id>42</id></getBalance></soap:Body></soap:Envelope>'},
                               resp_text="<balance>10</balance>", mime="text/xml")]))
    run("huge_body_2MB", har([entry("GET", api + "/v1/big", resp_text=json.dumps({"rows": ["x" * 100] * 20000}))]))

    print("Status codes and flow")
    run("cors_preflight_and_304_and_redirect", har([
        entry("OPTIONS", api + "/v1/me", status=204),
        entry("GET", api + "/v1/me", status=304),
        entry("GET", "https://www.example.com/old", status=302, resp_headers=[{"name": "Location", "value": "/new"}], mime="text/html"),
        entry("GET", "https://www.example.com/new", resp_text="<html>new</html>", mime="text/html"),
        entry("GET", api + "/v1/fail", status=500, resp_text='{"error":"boom"}'),
        entry("GET", api + "/v1/aborted", status=0)]))
    run("query_string_special_chars", har([entry("GET", api + "/search?q=caf%C3%A9+%26+bar&tags=a,b&empty=&x=%7B%22a%22%3A1%7D")]), expect_requests=1)
    run("ipv6_and_port_host", har([entry("GET", "http://[::1]:8080/health", resp_text="ok", mime="text/plain")]))

    print("Edge cases")
    run("empty_har", har([]), expect_requests=0)
    run("only_noise_and_static", har([entry("POST", "https://firebaselogging-pa.googleapis.com/v1/x"),
                                      entry("GET", "https://cdn.example.com/app.js", mime="application/javascript"),
                                      entry("GET", "https://www.google-analytics.com/collect")]), expect_requests=0)
    run("pages_with_titles_as_steps", har([dict(login, pageref="p1"), dict(entry("GET", api + "/v1/me", resp_text="{}"), pageref="p2")],
                                          pages=[{"id": "p1", "title": "Sign in"}, {"id": "p2", "title": "Dashboard"}]), expect_requests=2)
    run("missing_optional_fields", json.dumps({"log": {"entries": [{"request": {"method": "GET", "url": api + "/x"},
                                                                      "response": {"status": 200, "content": {}}}]}}).encode(), expect_requests=1)
    for bad, label in ((b"not json", "not a HAR (plain text)"), (b'{"foo": 1}', "JSON but not HAR")):
        try:
            r = load_any("bad.har", bad)
            check(len(r.entries) == 0, f"{label}: loads as an empty recording ({len(r.entries)} entries)")
        except Exception as ex:
            check(isinstance(ex, ValueError), f"{label}: clear ValueError ({str(ex)[:80]})")

    print("\nRESULT:", "ALL PASSED" if not failed else "FAILURES")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
