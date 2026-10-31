"""Loads the YAML rule files and applies the noise filter."""
from __future__ import annotations

import os
from functools import lru_cache

import yaml

RULES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rules")


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    with open(os.path.join(RULES_DIR, name), encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def noise_rules() -> dict:
    return load("sdk_noise.yaml")


def naming_rules() -> dict:
    return load("naming.yaml")


def correlation_rules() -> dict:
    return load("correlation_rules.yaml")


def host_matches(host: str, domains) -> bool:
    host = (host or "").lower()
    for d in domains:
        d = d.lower().strip()
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def filter_noise(entries, extra_exclude=(), include_hosts=(), keep_static=False, keep_failed_cors=False):
    """Split entries into (kept, removed[(entry, reason)])."""
    rules = noise_rules()
    domains = list(rules.get("domains", [])) + [d for d in extra_exclude if d]
    exts = tuple(rules.get("static_extensions", []))
    skip_methods = {m.upper() for m in rules.get("skip_methods", [])}
    kept, removed = [], []
    for e in entries:
        if include_hosts and not host_matches(e.host, include_hosts):
            removed.append((e, "host not in include list"))
        elif e.method in skip_methods:
            removed.append((e, f"{e.method} request"))
        elif host_matches(e.host, domains):
            removed.append((e, "SDK / telemetry domain"))
        elif not keep_static and e.path.lower().endswith(exts):
            removed.append((e, "static asset"))
        elif e.status == 0:
            removed.append((e, "no response recorded"))
        else:
            kept.append(e)
    return kept, removed
