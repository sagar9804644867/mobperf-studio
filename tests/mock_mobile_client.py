"""Simulates the MobiBank Android app (okhttp) driving a full user journey through a proxy.

Each step writes its name to <session>/step.txt exactly like the MCP `mark_step` tool does.
"""
from __future__ import annotations

import os
import re
import secrets
import time
import uuid

import requests

UA = "okhttp/4.12.0"
COMMON = {"User-Agent": UA, "X-App-Version": "4.2.1", "X-Platform": "android", "X-Device-Model": "Pixel 7",
          "Accept": "application/json", "Accept-Language": "en-IN"}


def run(base: str, proxy: str, session_dir: str, username: str, password: str, think: float = 0.4):
    s = requests.Session()
    s.trust_env = False
    s.proxies = {"http": proxy, "https": proxy}

    def step(name):
        with open(os.path.join(session_dir, "step.txt"), "w") as fh:
            fh.write(name)
        time.sleep(think)

    step("App Launch")
    cfg = s.get(f"{base}/api/v2/app/config", params={"platform": "android", "appVersion": "4.2.1"}, headers=COMMON).json()
    csrf = cfg["security"]["csrfToken"]
    device_id = str(uuid.uuid4())
    reg = s.post(f"{base}/api/v2/device/register", json={"deviceId": device_id, "platform": "android", "model": "Pixel 7"},
                 headers={**COMMON, "X-CSRF-Token": csrf}).json()

    step("Login")
    state = secrets.token_hex(8)
    r = s.get(f"{base}/oauth/authorize", params={"client_id": "mobibank-android", "response_type": "code",
                                                  "state": state, "device_token": reg["deviceToken"]},
              headers=COMMON, allow_redirects=False)
    code = re.search(r"code=([^&]+)", r.headers["Location"]).group(1)
    tok = s.post(f"{base}/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": "mobibank-android",
                                              "username": username, "password": password},
                 headers={**COMMON, "Content-Type": "application/x-www-form-urlencoded"}).json()
    auth = {**COMMON, "Authorization": f"Bearer {tok['access_token']}", "X-Device-Id": device_id}

    step("View Accounts")
    accts = s.get(f"{base}/api/v2/users/{tok['user']['id']}/accounts", headers=auth).json()["accounts"]
    acc = accts[0]["id"]

    step("View Statement")
    tx = s.get(f"{base}/api/v2/accounts/{acc}/transactions", params={"page": 1, "_ts": int(time.time() * 1000)}, headers=auth).json()
    s.get(f"{base}/api/v2/accounts/{acc}/transactions", params={"cursor": tx["nextCursor"]}, headers=auth)

    step("Fund Transfer")
    html = s.post(f"{base}/api/v2/payments/initiate",
                  data={"accountId": acc, "payee": "Ravi Kumar", "amount": "250.00", "nonce": secrets.token_hex(16)},
                  headers={**auth, "X-Request-ID": str(uuid.uuid4())}).text
    ref = re.search(r'name="txnRef" value="([^"]+)"', html).group(1)

    step("Confirm OTP")
    rc = s.post(f"{base}/api/v2/payments/confirm", json={"txnRef": ref, "otp": "123456"}, headers=auth).json()
    s.get(f"{base}/api/v2/payments/receipt/{rc['receiptNo']}", headers=auth)
    return rc
