"""ShopKart - second strict mock app (iOS-style, GraphQL + REST + XML) and its client.

Covers different patterns from MobiBank:
  * GraphQL operations, JWT in Authorization, numeric customer id
  * cart id + weak ETag (If-Match header) from a GraphQL response
  * checkout token inside an XML response
  * uppercase UUID idempotency key, ISO-8601 request time (both must be fresh)
  * an X-Signature header (HMAC computed on-device) -> must be FLAGGED, not faked
  * recording WITHOUT step markers (like a Charles / Proxyman HAR) -> gap-based step detection
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

CUSTOMERS = {"riya@shop.test": ("Riya#2026", 40021877), "aman@shop.test": ("Aman#2026", 40021911),
             "neha@shop.test": ("Neha#2026", 40022050)}


class St:
    jwts, carts, checkout, idem, errors, lock = {}, {}, {}, set(), [], threading.Lock()


def jwt_for(cid):
    h = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    p = base64.urlsafe_b64encode(json.dumps({"sub": cid, "iat": int(time.time()), "jti": secrets.token_hex(6)}).encode()).rstrip(b"=").decode()
    return f"{h}.{p}.{secrets.token_urlsafe(32)}"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        if code >= 400:
            St.errors.append(f"{self.path} {code} {body}")

    def cust(self):
        m = re.match(r"Bearer (.+)", self.headers.get("Authorization", ""))
        return St.jwts.get(m.group(1)) if m else None

    def do_GET(self):
        c = self.cust()
        m = re.fullmatch(r"/rest/v1/carts/(crt_[0-9a-f]+)", self.path)
        if not c:
            return self.send(401, {"error": "jwt"})
        if not m or St.carts.get(m.group(1), (None,))[0] != c:
            return self.send(404, {"error": "cart"})
        if self.headers.get("If-Match") != St.carts[m.group(1)][1]:
            return self.send(412, {"error": "etag mismatch"})
        tok = "chk-" + secrets.token_hex(10)
        St.checkout[tok] = m.group(1)
        xml = (f'<?xml version="1.0"?><cart id="{m.group(1)}"><items count="1"/><total currency="INR">1999.00</total>'
               f"<checkoutToken>{tok}</checkoutToken></cart>")
        self.send(200, xml, "application/xml")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        if self.path == "/graphql":
            q = json.loads(body)
            op, v = q.get("operationName"), q.get("variables", {})
            if op == "Login":
                cu = CUSTOMERS.get(v.get("email"))
                if not cu or cu[0] != v.get("password"):
                    return self.send(200, {"errors": [{"message": "bad credentials"}]}) or St.errors.append("login")
                tok = jwt_for(cu[1])
                St.jwts[tok] = cu[1]
                return self.send(200, {"data": {"login": {"jwt": tok, "customer": {"id": cu[1], "tier": "GOLD"}}}})
            c = self.cust()
            if not c:
                return self.send(401, {"errors": [{"message": "jwt"}]})
            if op == "Profile":
                if v.get("customerId") != c:
                    return self.send(403, {"errors": [{"message": "customer mismatch"}]})
                return self.send(200, {"data": {"profile": {"id": c, "city": "Kolkata"}}})
            if op == "Search":
                return self.send(200, {"data": {"search": {"products": [{"sku": "SKU-RUN-778", "price": 1999}, {"sku": "SKU-RUN-901", "price": 2499}]}}})
            if op == "AddToCart":
                cid, etag = "crt_" + secrets.token_hex(8), 'W/"' + secrets.token_hex(6) + '"'
                St.carts[cid] = (c, etag)
                return self.send(200, {"data": {"addToCart": {"cart": {"id": cid, "etag": etag}}}})
        if self.path == "/rest/v1/checkout":
            c = self.cust()
            j = json.loads(body)
            key = self.headers.get("X-Idempotency-Key", "")
            if not c:
                return self.send(401, {"error": "jwt"})
            if key in St.idem or not re.fullmatch(r"[0-9A-F\-]{36}", key):
                return self.send(409, {"error": "idempotency key replayed"})
            St.idem.add(key)
            rt = datetime.strptime(j.get("requestTime", "2000-01-01T00:00:00.000Z"), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            if abs(rt.timestamp() - time.time()) > 300:
                return self.send(400, {"error": "stale requestTime"})
            if St.checkout.pop(j.get("checkoutToken"), None) != j.get("cartId"):
                return self.send(400, {"error": "checkout token"})
            return self.send(201, {"orderId": "ORD" + secrets.token_hex(5).upper(), "status": "PLACED"})
        if self.path == "/rest/v1/analytics/secure-event":
            return self.send(202, {"accepted": True})  # signature not enforced in this test env
        self.send(404, {"error": "route"})


def serve(port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_client(base, proxy, email, password, gap=2.5):
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": proxy}
    hd = {"User-Agent": "ShopKart/7.3 (iPhone; iOS 17.4) CFNetwork/1494 Darwin/23.4.0", "Accept": "application/json",
          "X-Client": "ios"}
    gql = lambda op, var, h=hd: s.post(f"{base}/graphql", json={"operationName": op, "query": f"query {op} {{ ... }}", "variables": var}, headers=h).json()
    login = gql("Login", {"email": email, "password": password})["data"]["login"]
    auth = {**hd, "Authorization": "Bearer " + login["jwt"]}
    gql("Profile", {"customerId": login["customer"]["id"]}, auth)
    time.sleep(gap)
    sku = gql("Search", {"term": "running shoes", "first": 20}, auth)["data"]["search"]["products"][0]["sku"]
    time.sleep(gap)
    cart = gql("AddToCart", {"sku": sku, "qty": 1}, auth)["data"]["addToCart"]["cart"]
    time.sleep(gap)
    xml = s.get(f"{base}/rest/v1/carts/{cart['id']}", headers={**auth, "If-Match": cart["etag"]}).text
    tok = re.search(r"<checkoutToken>(.+?)</checkoutToken>", xml).group(1)
    body = {"cartId": cart["id"], "checkoutToken": tok, "paymentMode": "COD",
            "requestTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now().microsecond // 1000:03d}Z"}
    s.post(f"{base}/rest/v1/checkout", json=body, headers={**auth, "X-Idempotency-Key": str(uuid.uuid4()).upper()})
    sig = hmac.new(b"on-device-secret", json.dumps(body).encode(), hashlib.sha256).hexdigest()
    s.post(f"{base}/rest/v1/analytics/secure-event", json={"event": "order_placed"}, headers={**auth, "X-Signature": sig})
