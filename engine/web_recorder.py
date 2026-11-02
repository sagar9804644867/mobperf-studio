"""Record a web / API journey with headless Chromium (Playwright) into a HAR file.

The HAR goes straight into the same engine as a mobile recording, so the output is the same kind of
JMeter script: correlated, parameterised, named, body-checked and JMX-checkpointed.

Scenario = a JSON list of actions, run in order. A {"step": "..."} action starts a new transaction
(the same role as mark_step on mobile):

    [
      {"step": "Login"},
      {"goto": "https://shop.example.com/login"},
      {"fill": ["input[name=user]", "alice"]},
      {"fill": ["input[name=pass]", "secret"]},
      {"click": "button[type=submit]"},
      {"step": "View orders"},
      {"wait_ms": 500}
    ]

Run it:
    python -m engine.web_recorder scenario.json recording.har [--headful] [--ignore-https-errors]

Needs Playwright and a browser once:  pip install playwright && python -m playwright install chromium
"""
from __future__ import annotations

import json
import os
import sys
import time

from .model import _parse_iso

ACTIONS = ("goto", "click", "fill", "press", "wait_ms")


def _launch_args() -> list[str]:
    # Chromium refuses to start as root without this (containers, some CI images). Harmless elsewhere.
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []


def validate_scenario(scenario) -> list[dict]:
    if not isinstance(scenario, list) or not scenario:
        raise ValueError("scenario must be a non-empty list of actions")
    for i, act in enumerate(scenario, 1):
        if not isinstance(act, dict):
            raise ValueError(f"action {i} must be an object, e.g. {{\"goto\": \"https://...\"}}")
        if "step" in act:
            if not str(act["step"]).strip():
                raise ValueError(f"action {i}: step name is empty")
            continue
        kinds = [k for k in ACTIONS if k in act]
        if len(kinds) != 1:
            raise ValueError(f"action {i} needs exactly one of: step, {', '.join(ACTIONS)}")
        if kinds[0] == "fill" and (not isinstance(act["fill"], list) or len(act["fill"]) != 2):
            raise ValueError(f"action {i}: fill takes [selector, value]")
    return scenario


def tag_steps(har: dict, marks: list[tuple[float, str]]) -> dict:
    """Give every HAR entry the step whose marker came last before the request started."""
    marks = sorted(marks)
    first = marks[0][1] if marks else None
    for e in har.get("log", {}).get("entries", []):
        t = _parse_iso(e.get("startedDateTime", ""))
        name = first
        for ts, label in marks:
            if ts <= t + 0.001:
                name = label
            else:
                break
        if name:
            e["_mobperf_step"] = name
    return har


def record_web(scenario, har_path: str, headless: bool = True, settle_ms: int = 600,
               ignore_https_errors: bool = False) -> dict:
    """Run the scenario in Chromium and write an annotated HAR. Returns a small summary."""
    from playwright.sync_api import sync_playwright

    scenario = validate_scenario(scenario)
    raw = har_path + ".raw"
    marks: list[tuple[float, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=_launch_args())
        ctx = browser.new_context(record_har_path=raw, record_har_content="embed",
                                  ignore_https_errors=ignore_https_errors)
        page = ctx.new_page()
        try:
            for act in scenario:
                if "step" in act:
                    marks.append((time.time(), str(act["step"]).strip()))
                    continue
                if "goto" in act:
                    page.goto(str(act["goto"]), wait_until="load")
                elif "click" in act:
                    page.click(str(act["click"]))
                elif "fill" in act:
                    sel, val = act["fill"]
                    page.fill(str(sel), str(val))
                elif "press" in act:
                    page.keyboard.press(str(act["press"]))
                elif "wait_ms" in act:
                    page.wait_for_timeout(int(act["wait_ms"]))
                try:  # let XHR / fetch calls triggered by this action finish before the next step
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(settle_ms)
        finally:
            ctx.close()  # writes the HAR
            browser.close()

    with open(raw, encoding="utf-8") as fh:
        har = json.load(fh)
    os.remove(raw)
    tag_steps(har, marks)
    with open(har_path, "w", encoding="utf-8") as fh:
        json.dump(har, fh)
    entries = har.get("log", {}).get("entries", [])
    return {"requests": len(entries), "steps": [m[1] for m in marks], "har": har_path}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="mobperf-web-record")
    ap.add_argument("scenario", help="JSON file with the list of actions")
    ap.add_argument("har", help="output HAR file")
    ap.add_argument("--headful", action="store_true", help="show the browser window")
    ap.add_argument("--ignore-https-errors", action="store_true", help="for test sites with self-signed certificates")
    a = ap.parse_args(argv)
    with open(a.scenario, encoding="utf-8") as fh:
        scenario = json.load(fh)
    out = record_web(scenario, a.har, headless=not a.headful, ignore_https_errors=a.ignore_https_errors)
    print(f"Recorded {out['requests']} requests in {len(out['steps'])} steps -> {out['har']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
