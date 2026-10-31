"""Transaction (step) detection and naming."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .correlate import looks_token

NOISE_SEGS = {"api", "apis", "rest", "mobile", "mob", "app", "apps", "service", "services", "svc", "public", "private",
              "gateway", "gw", "bff", "ws", "json", "graphql", "index", "default"}
VERSION_RE = re.compile(r"^v\d+(\.\d+)*$", re.I)


@dataclass
class Step:
    nn: int
    name: str
    positions: list = field(default_factory=list)
    think_ms: int = 0

    def label(self, prefix: str, app: str, pattern: str) -> str:
        return pattern.format(prefix=prefix, app=app, nn=self.nn, step=self.name)


def pascal(s: str) -> str:
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", s or "") if p]
    out = "".join(p[0].upper() + p[1:] for p in parts)
    return out or "Step"


def meaningful_segments(path: str):
    segs = []
    for s in path.split("/"):
        s = re.sub(r"\.(json|xml|php|aspx?|jsp|do|action)$", "", s, flags=re.I)
        if not s or s.lower() in NOISE_SEGS or VERSION_RE.match(s):
            continue
        if looks_token(s) or re.fullmatch(r"\d+", s):
            continue
        segs.append(s)
    return segs


def short_name(entry) -> str:
    segs = meaningful_segments(entry.path)
    if not segs:
        segs = [entry.host.split(".")[0]]
    s = "_".join(segs[-2:])
    if entry.path.rstrip("/").lower().endswith("graphql") or "graphql" in entry.path.lower():
        m = re.search(r'"operationName"\s*:\s*"([A-Za-z0-9_]+)"', entry.req_body or "")
        if m:
            s = "gql_" + m.group(1)
    return re.sub(r"[^A-Za-z0-9_\-]+", "", s)[:48] or "root"


def guess_step_name(entries) -> str:
    pick = next((e for e in entries if e.method != "GET"), entries[0])
    m = re.search(r'"operationName"\s*:\s*"([A-Za-z0-9_]+)"', pick.req_body or "")
    if m:
        return pascal(m.group(1))
    segs = meaningful_segments(pick.path)
    if not segs:
        return "Launch" if pick is entries[0] else "Step"
    if len(segs) >= 2 and segs[-1].lower() in {"list", "get", "details", "detail", "all", "search", "init", "initiate", "confirm", "submit", "create", "update", "delete", "status", "summary", "verify", "validate"}:
        return pascal(segs[-1] + "_" + segs[-2])
    return pascal(segs[-1])


def detect_steps(entries, gap_s: float = 2.0, default_think_ms: int = 3000):
    groups = []  # list[(marker, [positions])]
    has_markers = any(e.page for e in entries)
    if has_markers:
        cur = None
        for pos, e in enumerate(entries):
            marker = e.page or (cur[0] if cur else "Launch")
            if cur is None or marker != cur[0]:
                cur = (marker, [])
                groups.append(cur)
            cur[1].append(pos)
    else:
        cur = None
        last_end = None
        for pos, e in enumerate(entries):
            start = e.started
            if cur is None or (last_end is not None and start and start - last_end > gap_s):
                cur = (None, [])
                groups.append(cur)
            cur[1].append(pos)
            end = (e.started or 0) + (e.time_ms or 0) / 1000.0
            last_end = max(last_end or 0, end) if start else last_end

    steps, used = [], {}
    for i, (marker, positions) in enumerate(groups):
        name = pascal(marker) if marker else guess_step_name([entries[p] for p in positions])
        used[name] = used.get(name, 0) + 1
        if used[name] > 1:
            name = f"{name}{used[name]}"
        # think time = quiet time between the end of this step and the start of the next one
        think = default_think_ms
        if i + 1 < len(groups):
            last = entries[positions[-1]]
            nxt = entries[groups[i + 1][1][0]]
            if last.started and nxt.started:
                gap = (nxt.started - (last.started + last.time_ms / 1000.0)) * 1000
                if gap > 300:
                    think = int(min(max(gap, 500), 30000) // 100 * 100)
        steps.append(Step(i + 1, name, positions, think))
    return steps
