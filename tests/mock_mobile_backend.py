"""MobiBank - a deliberately STRICT mock mobile-banking backend used to prove correlation.

Every dynamic value is checked server-side, so a script only passes if it is correctly
correlated and parameterised:
  * session cookie + CSRF token bound together            (cookie + header correlation)
  * device token issued at registration                   (JSON correlation)
  * OAuth code delivered in a 302 Location header          (header regex correlation)
  * OAuth state must echo the client's random value        (generated value)
  * access token, user id, account id, paging cursor       (JSON correlation, path + query)
  * request timestamp must be within 5 minutes             (generated __time)
  * payment nonce + X-Request-ID must never repeat         (generated random / UUID)
  * txnRef delivered inside an HTML hidden field           (body boundary regex)
  * credentials per user                                   (CSV parameters)
Run standalone:  python tests/mock_mobile_backend.py 18080
"""
from __future__ import annotations

import json
import re
import secrets
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

USERS = {"alice": ("Alice@123", "U1001", "Alice Rao"), "bob": ("Bob@123", "U1002", "Bob Das"),
         "carol": ("Carol@123", "U1003", "Carol Sen")}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.sessions = {}      # session cookie -> csrf
        self.devices = {}       # deviceToken -> deviceId
        self.codes = {}         # code -> deviceToken (one time)
        self.tokens = {}        # access_token -> user key
        self.cursors = {}       # cursor -> (token, account)
        self.nonces = set()
        self.request_ids = set()
        self.txns = {}          # txnRef -> token (one time)
        self.receipts = {}      # receiptNo -> token
        self.errors = []
        self.ok = 0


S = State()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MobiBank/2.0"

    def log_message(self, *a):
        pass

    # ---------------------------------------------------------------- utils
    def _send(self, code, body, ctype="application/json", headers=None):
        data = (json.dumps(body) if not isinstance(body, str) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        with S.lock:
            if code >= 400:
                S.errors.append(f"{self.command} {self.path} -> {code} {body}")
            else:
                S.ok += 1

    def _fail(self, code, why):
        self._send(code, {"error": why})

    def _cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode() if n else ""

    def _user(self):
        auth = self.headers.get("Authorization") or ""
        m = re.match(r"Bearer (.+)", auth)
        return S.tokens.get(m.group(1)) if m else None

    def _session_ok(self):
        sid = self._cookie("SESSION")
        return sid in S.sessions and S.sessions[sid] == self.headers.get("X-CSRF-Token")

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        u = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        if p == "/api/v2/app/config":
            sid, csrf = secrets.token_hex(16), secrets.token_urlsafe(24)
            S.sessions[sid] = csrf
            return self._send(200, {"minVersion": "4.0.0", "currency": "INR", "features": {"upi": True, "cards": True},
                                    "security": {"csrfToken": csrf}},
                              headers=[("Set-Cookie", f"SESSION={sid}; Path=/; HttpOnly")])
        if p == "/oauth/authorize":
            dt = q.get("device_token")
            if dt not in S.devices:
                return self._fail(401, "unknown device_token")
            if not re.fullmatch(r"[0-9a-f]{16}", q.get("state", "")):
                return self._fail(400, "bad state")
            code = secrets.token_urlsafe(18)
            S.codes[code] = dt
            return self._send(302, "", "text/plain", headers=[("Location", f"mobibank://oauth/callback?code={code}&state={q['state']}")])
        user = self._user()
        if p.startswith("/api/v2/") and user is None:
            return self._fail(401, "invalid access token")
        m = re.fullmatch(r"/api/v2/users/(U\d+)/accounts", p)
        if m:
            if USERS[user][1] != m.group(1):
                return self._fail(403, "user id does not match token")
            return self._send(200, {"accounts": [
                {"id": f"ACC{USERS[user][1][1:]}{i}", "type": t, "balance": 1000 * i, "currency": "INR"}
                for i, t in ((1, "SAVINGS"), (2, "CURRENT"))]})
        m = re.fullmatch(r"/api/v2/accounts/(ACC\d+)/transactions", p)
        if m:
            acc = m.group(1)
            if not acc.startswith("ACC" + USERS[user][1][1:]):
                return self._fail(403, "account does not belong to user")
            if "cursor" in q:
                if S.cursors.get(q["cursor"]) != (user, acc):
                    return self._fail(400, "invalid cursor")
                return self._send(200, {"items": [{"id": "T9", "amount": -50}], "nextCursor": None})
            ts = int(q.get("_ts", "0"))
            if abs(ts / 1000 - time.time()) > 300:
                return self._fail(400, "stale or missing _ts timestamp")
            cur = secrets.token_hex(12)
            S.cursors[cur] = (user, acc)
            return self._send(200, {"items": [{"id": "T1", "amount": -120}, {"id": "T2", "amount": 5000}], "nextCursor": cur})
        m = re.fullmatch(r"/api/v2/payments/receipt/(R\w+)", p)
        if m:
            if S.receipts.get(m.group(1)) != user:
                return self._fail(404, "receipt not found")
            return self._send(200, {"receiptNo": m.group(1), "status": "SUCCESS"})
        return self._fail(404, "no route")

    def do_POST(self):
        u = urlsplit(self.path)
        p = u.path
        body = self._body()
        ctype = self.headers.get("Content-Type", "")
        if p == "/api/v2/device/register":
            if not self._session_ok():
                return self._fail(403, "CSRF token / SESSION cookie mismatch")
            j = json.loads(body or "{}")
            try:
                uuid.UUID(j.get("deviceId", ""))
            except ValueError:
                return self._fail(400, "deviceId must be a UUID")
            dt = "dt_" + secrets.token_hex(20)
            S.devices[dt] = j["deviceId"]
            return self._send(201, {"deviceId": j["deviceId"], "deviceToken": dt})
        if p == "/oauth/token":
            f = {k: v[0] for k, v in parse_qs(body).items()}
            code = f.get("code")
            if code not in S.codes:
                return self._fail(400, "invalid or reused authorization code")
            S.codes.pop(code)
            user = f.get("username")
            if user not in USERS or USERS[user][0] != f.get("password"):
                return self._fail(401, "bad credentials")
            at = secrets.token_urlsafe(32)
            S.tokens[at] = user
            return self._send(200, {"access_token": at, "token_type": "Bearer", "expires_in": 900,
                                    "refresh_token": secrets.token_urlsafe(32),
                                    "user": {"id": USERS[user][1], "name": USERS[user][2]}})
        user = self._user()
        if user is None:
            return self._fail(401, "invalid access token")
        if p == "/api/v2/payments/initiate":
            if "x-www-form-urlencoded" not in ctype:
                return self._fail(415, "form expected")
            f = {k: v[0] for k, v in parse_qs(body).items()}
            rid = self.headers.get("X-Request-ID", "")
            if f.get("nonce") in S.nonces or not re.fullmatch(r"[0-9a-f]{32}", f.get("nonce", "")):
                return self._fail(409, "nonce missing or replayed")
            if rid in S.request_ids:
                return self._fail(409, "X-Request-ID replayed")
            S.nonces.add(f["nonce"])
            S.request_ids.add(rid)
            if not f.get("accountId", "").startswith("ACC" + USERS[user][1][1:]):
                return self._fail(403, "account does not belong to user")
            ref = "TXN" + secrets.token_hex(8).upper()
            S.txns[ref] = user
            html = (f"<html><body><form id=\"otp\" action=\"/api/v2/payments/confirm\">"
                    f"<input type=\"hidden\" name=\"txnRef\" value=\"{ref}\"/>"
                    f"<p>Enter the OTP sent to your mobile</p></form></body></html>")
            return self._send(200, html, "text/html")
        if p == "/api/v2/payments/confirm":
            j = json.loads(body or "{}")
            if S.txns.pop(j.get("txnRef"), None) != user:
                return self._fail(400, "unknown or reused txnRef")
            if j.get("otp") != "123456":
                return self._fail(401, "wrong OTP")
            rn = "R" + secrets.token_hex(6)
            S.receipts[rn] = user
            return self._send(200, {"status": "SUCCESS", "receiptNo": rn})
        return self._fail(404, "no route")


def serve(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18080
    print(f"MobiBank mock on http://127.0.0.1:{port}")
    serve(port)
    while True:
        time.sleep(3600)
