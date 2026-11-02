"""ShopWeb - a strict mock WEB application (browser, not mobile) used by tests/test_web_and_body.py.

What makes it strict:
  * GET /login renders an HTML form with a one-time hidden CSRF token; POST /login rejects a reused or unknown one
  * login sets a random session cookie and redirects (302) to /home
  * /home renders the API token inside an HTML <meta> tag; page JavaScript calls the API with it
  * /api/orders needs "Authorization: Bearer <token>" for THIS session
  * S["expire"] = True makes /api/orders answer HTTP 200 with {"error": "token expired"} -
    the case a status-code-only assertion misses
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

USERS = {"alice": "Alice@123", "bob": "Bob@123", "carol": "Carol@123"}
S = {"drop_token": False, "expire": False, "csrf": set(), "sessions": {}, "tokens": {}, "hits": 0}

LOGIN = """<!doctype html><html><head><title>ShopWeb - Sign in</title></head><body>
<h1>ShopWeb</h1><form method="post" action="/login">
<input name="user"><input name="pass" type="password">
<input type="hidden" name="csrf" value="{csrf}">
<button type="submit">Sign in</button></form></body></html>"""

HOME = """<!doctype html><html><head><title>ShopWeb - Home</title>
<meta name="api-token" content="{token}"></head><body><h1>Hello {user}</h1><ul id="orders"></ul>
<script>
const t = document.querySelector('meta[name=api-token]').content;
fetch('/api/orders', {{headers: {{'Authorization': 'Bearer ' + t}}}})
  .then(r => r.json()).then(d => {{ document.getElementById('orders').textContent = JSON.stringify(d); }});
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8", headers=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _session(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "sid" and v in S["sessions"]:
                return v
        return None

    def do_GET(self):
        S["hits"] += 1
        if self.path == "/login":
            c = secrets.token_hex(12)
            S["csrf"].add(c)
            return self._send(200, LOGIN.format(csrf=c))
        if self.path == "/home":
            sid = self._session()
            if not sid:
                return self._send(401, "Unauthorized - please sign in")
            page = HOME.format(token=S["sessions"][sid]["token"], user=S["sessions"][sid]["user"])
            if S["drop_token"]:  # a release that stopped sending the token: correlation must fail loudly
                page = page.replace(f'<meta name="api-token" content="{S["sessions"][sid]["token"]}">', "")
            return self._send(200, page)
        if self.path == "/api/orders":
            auth = self.headers.get("Authorization", "")
            tok = auth[7:] if auth.startswith("Bearer ") else ""
            sid = self._session()
            if not sid or S["sessions"][sid]["token"] != tok:
                return self._send(401, json.dumps({"error": "invalid token"}), "application/json")
            if S["expire"]:  # the failure a 200-only assertion cannot see
                return self._send(200, json.dumps({"error": "token expired"}), "application/json")
            user = S["sessions"][sid]["user"]
            # 'Unauthorized' is part of a NORMAL response here, so the engine must not turn it into a failure check
            return self._send(200, json.dumps({"user": user, "orders": [{"id": 1001, "total": 499}],
                                               "note": "Unauthorized access attempts are logged"}), "application/json")
        if self.path == "/favicon.ico":
            return self._send(404, "")
        return self._send(404, "not found", "text/plain")

    def do_POST(self):
        S["hits"] += 1
        n = int(self.headers.get("Content-Length") or 0)
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode()).items()}
        if self.path != "/login":
            return self._send(404, "not found", "text/plain")
        c = form.get("csrf", "")
        if c not in S["csrf"]:
            return self._send(403, "bad csrf", "text/plain")
        S["csrf"].discard(c)  # one-time token
        if USERS.get(form.get("user")) != form.get("pass"):
            return self._send(401, "Unauthorized", "text/plain")
        sid, tok = secrets.token_hex(16), secrets.token_urlsafe(24)
        S["sessions"][sid] = {"user": form["user"], "token": tok}
        return self._send(302, "", headers={"Location": "/home", "Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})


def serve(port: int):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
