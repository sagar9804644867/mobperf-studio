"""mitmproxy addon used by MobPerf recordings.

Run:  mitmdump -s capture_addon.py --set mobperf_dir=<session folder> -p 8080

* Writes <session>/recording.har after every response (safe to stop at any time).
* Reads the current step name from <session>/step.txt (written by the MCP `mark_step` tool,
  or by hand: echo Login > step.txt) and stores it as the HAR page, so every transaction
  gets the right name.
* Logs failed TLS handshakes to <session>/tls_failures.txt (a strong sign of SSL pinning).
"""
from __future__ import annotations

import base64
import json
import os
import threading
from datetime import datetime, timezone

from mitmproxy import ctx, http

TEXT_HINTS = ("json", "text", "xml", "html", "javascript", "x-www-form-urlencoded", "graphql")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class MobPerfCapture:
    def __init__(self):
        self.entries = []
        self.pages = []
        self.page_ids = {}
        self.lock = threading.Lock()

    def load(self, loader):
        loader.add_option("mobperf_dir", str, os.path.expanduser("~/.mobperf/session"), "MobPerf session folder")

    @property
    def dir(self) -> str:
        d = ctx.options.mobperf_dir
        os.makedirs(d, exist_ok=True)
        return d

    def _step(self) -> str:
        try:
            with open(os.path.join(self.dir, "step.txt"), encoding="utf-8") as fh:
                return fh.read().strip() or "Launch"
        except OSError:
            return "Launch"

    def _page(self, title: str, ts: float) -> str:
        if title not in self.page_ids:
            pid = f"page_{len(self.pages) + 1}"
            self.page_ids[title] = pid
            self.pages.append({"startedDateTime": _iso(ts), "id": pid, "title": title, "pageTimings": {}})
        return self.page_ids[title]

    def request(self, flow: http.HTTPFlow):
        flow.metadata["mobperf_step"] = self._step()

    def response(self, flow: http.HTTPFlow):
        req, resp = flow.request, flow.response
        step = flow.metadata.get("mobperf_step") or self._step()
        ctype = resp.headers.get("content-type", "")
        content = {"size": len(resp.raw_content or b""), "mimeType": ctype}
        if any(h in ctype.lower() for h in TEXT_HINTS) or not ctype:
            content["text"] = resp.get_text(strict=False) or ""
        elif resp.raw_content:
            content["text"] = base64.b64encode(resp.content or b"").decode()
            content["encoding"] = "base64"
        body = req.get_text(strict=False) or ""
        entry = {
            "pageref": None,
            "startedDateTime": _iso(req.timestamp_start),
            "time": max(0.0, ((resp.timestamp_end or req.timestamp_start) - req.timestamp_start) * 1000),
            "request": {
                "method": req.method, "url": req.pretty_url, "httpVersion": req.http_version,
                "headers": [{"name": k, "value": v} for k, v in req.headers.items(multi=True)],
                "queryString": [{"name": k, "value": v} for k, v in req.query.items(multi=True)],
                "cookies": [], "headersSize": -1, "bodySize": len(req.raw_content or b""),
            },
            "response": {
                "status": resp.status_code, "statusText": resp.reason or "", "httpVersion": resp.http_version,
                "headers": [{"name": k, "value": v} for k, v in resp.headers.items(multi=True)],
                "cookies": [], "content": content, "redirectURL": resp.headers.get("location", ""),
                "headersSize": -1, "bodySize": len(resp.raw_content or b""),
            },
            "cache": {}, "timings": {"send": 0, "wait": 0, "receive": 0},
            "_mobperf_step": step,
        }
        if body:
            entry["request"]["postData"] = {"mimeType": req.headers.get("content-type", ""), "text": body}
        with self.lock:
            entry["pageref"] = self._page(step, req.timestamp_start)
            self.entries.append(entry)
            self._flush()

    def tls_failed_client(self, data):
        try:
            sni = data.context.client.sni or "unknown"
        except Exception:
            sni = "unknown"
        with self.lock, open(os.path.join(self.dir, "tls_failures.txt"), "a", encoding="utf-8") as fh:
            fh.write(f"{_iso(datetime.now().timestamp())}\t{sni}\n")

    def _flush(self):
        har = {"log": {"version": "1.2", "creator": {"name": "MobPerf Studio", "version": "1.0"},
                       "pages": self.pages, "entries": self.entries}}
        tmp = os.path.join(self.dir, "recording.har.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(har, fh)
        os.replace(tmp, os.path.join(self.dir, "recording.har"))


addons = [MobPerfCapture()]
