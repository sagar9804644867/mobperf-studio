"""Core data model and recording loaders (HAR, mitmproxy .flow, MobPerf session bundle)."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit, parse_qsl

TEXT_MIME_HINTS = ("json", "text", "xml", "html", "javascript", "x-www-form-urlencoded", "graphql")


@dataclass
class Entry:
    idx: int
    method: str
    url: str
    req_headers: list  # list[tuple[str, str]]
    req_body: str
    status: int
    resp_headers: list
    resp_body: str
    started: float = 0.0  # epoch seconds
    time_ms: float = 0.0
    page: Optional[str] = None  # step / transaction marker
    binary_body: bool = False

    # ---- convenience -------------------------------------------------
    @property
    def parts(self):
        return urlsplit(self.url)

    @property
    def scheme(self) -> str:
        return self.parts.scheme or "https"

    @property
    def host(self) -> str:
        return self.parts.hostname or ""

    @property
    def port(self) -> Optional[int]:
        p = self.parts.port
        if p is None:
            return None
        if (self.scheme == "https" and p == 443) or (self.scheme == "http" and p == 80):
            return None
        return p

    @property
    def path(self) -> str:
        return self.parts.path or "/"

    @property
    def query(self) -> str:
        return self.parts.query

    @property
    def query_pairs(self) -> list:
        return parse_qsl(self.query, keep_blank_values=True)

    def req_header(self, name: str) -> Optional[str]:
        for k, v in self.req_headers:
            if k.lower() == name.lower():
                return v
        return None

    def resp_header(self, name: str) -> Optional[str]:
        for k, v in self.resp_headers:
            if k.lower() == name.lower():
                return v
        return None

    @property
    def req_mime(self) -> str:
        return (self.req_header("content-type") or "").lower()

    @property
    def resp_mime(self) -> str:
        return (self.resp_header("content-type") or "").lower()


@dataclass
class Recording:
    entries: list
    source: str = ""
    meta: dict = field(default_factory=dict)


def _is_textual(mime: str) -> bool:
    mime = (mime or "").lower()
    return (not mime) or any(h in mime for h in TEXT_MIME_HINTS)


def _decode_content(content: dict) -> tuple[str, bool]:
    text = content.get("text") or ""
    mime = content.get("mimeType", "")
    if content.get("encoding") == "base64" and text:
        raw = base64.b64decode(text)
        try:
            return raw.decode("utf-8"), False
        except UnicodeDecodeError:
            return "", True
    return text, (not _is_textual(mime) and bool(text) and "\x00" in text)


def _parse_iso(ts: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def load_har(data: bytes | str, source: str = "") -> Recording:
    har = json.loads(data)
    log = har.get("log", har)
    pages = {p.get("id"): p.get("title") for p in log.get("pages", []) or []}
    entries = []
    for i, e in enumerate(log.get("entries", [])):
        req, resp = e.get("request", {}), e.get("response", {})
        body = ""
        pd = req.get("postData") or {}
        if pd.get("text") is not None:
            body = pd.get("text") or ""
        elif pd.get("params"):
            from urllib.parse import urlencode
            body = urlencode([(p.get("name", ""), p.get("value", "")) for p in pd["params"]])
        rbody, binary = _decode_content(resp.get("content") or {})
        hdrs = [(h["name"], h["value"]) for h in req.get("headers", []) if not h["name"].startswith(":")]
        rhdrs = [(h["name"], h["value"]) for h in resp.get("headers", []) if not h["name"].startswith(":")]
        page = e.get("_mobperf_step") or pages.get(e.get("pageref"))
        entries.append(Entry(
            idx=i, method=req.get("method", "GET").upper(), url=req.get("url", ""),
            req_headers=hdrs, req_body=body, status=int(resp.get("status") or 0),
            resp_headers=rhdrs, resp_body=rbody, started=_parse_iso(e.get("startedDateTime", "")),
            time_ms=float(e.get("time") or 0), page=page, binary_body=binary,
        ))
    return Recording(entries=entries, source=source, meta={"format": "har"})


def load_flow(path: str) -> Recording:
    """mitmproxy .flow / .mitm dump (requires mitmproxy installed locally)."""
    from mitmproxy import io as mio, http
    entries = []
    with open(path, "rb") as fh:
        for i, f in enumerate(mio.FlowReader(fh).stream()):
            if not isinstance(f, http.HTTPFlow) or f.response is None:
                continue
            entries.append(Entry(
                idx=len(entries), method=f.request.method, url=f.request.pretty_url,
                req_headers=list(f.request.headers.items(multi=True)),
                req_body=f.request.get_text(strict=False) or "",
                status=f.response.status_code,
                resp_headers=list(f.response.headers.items(multi=True)),
                resp_body=f.response.get_text(strict=False) or "",
                started=f.request.timestamp_start or 0.0,
                time_ms=((f.response.timestamp_end or 0) - (f.request.timestamp_start or 0)) * 1000,
                page=(f.metadata or {}).get("mobperf_step"),
            ))
    return Recording(entries=entries, source=path, meta={"format": "flow"})


def load_any(name: str, data: bytes) -> Recording:
    low = name.lower()
    if low.endswith((".flow", ".mitm")):
        import tempfile, os
        with tempfile.NamedTemporaryFile(delete=False, suffix=".flow") as t:
            t.write(data)
        try:
            return load_flow(t.name)
        finally:
            os.unlink(t.name)
    return load_har(data, source=name)


_SLUG = re.compile(r"[^A-Za-z0-9]+")


def slug(s: str, sep: str = "_") -> str:
    return _SLUG.sub(sep, s or "").strip(sep)
