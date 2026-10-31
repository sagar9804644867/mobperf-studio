"""Proves the JMX structure checkpoint passes generated scripts and catches GUI-breaking mistakes.

    python tests/test_jmx_checkpoint.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.jmx_lint import lint  # noqa: E402

failed = False


def check(cond, msg):
    global failed
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    failed |= not cond


def main():
    jmx = open(os.path.join(ROOT, "samples", "MOB_MobiBank.jmx"), encoding="utf-8").read()
    r = lint(jmx)
    for c in r.checks:
        print(f"    {c[0]} {'PASS' if c[2] else 'FAIL'}  {c[1]}")
    check(r.ok, f"generated MobiBank script passes all checks ({len(r.errors)} errors)")
    for e in r.errors:
        print("     ", e)

    # each mutation reproduces a real-world "cannot open in JMeter GUI" cause
    cases = {
        "L01 control character from recorded body": (lambda s: s.replace("grant_type=", "grant_type=\x01", 1), "L01"),
        "L01 broken XML (unclosed tag)": (lambda s: s.replace("</HTTPSamplerProxy>", "", 1), "L01"),
        "L03 missing hashTree after a sampler": (lambda s: re.sub(r"(</HTTPSamplerProxy>)\s*<hashTree>.*?</hashTree>", r"\1", s, count=1, flags=re.S), "L03"),
        "L04 wrong guiclass": (lambda s: s.replace('guiclass="HttpTestSampleGui"', 'guiclass="HttpTestSampleGUI"', 1), "L04"),
        "L04 tag/testclass mismatch": (lambda s: s.replace('testclass="RegexExtractor"', 'testclass="RegExExtractor"', 1), "L04"),
        "L06 boolProp not true/false": (lambda s: s.replace('<boolProp name="HTTPSampler.follow_redirects">false', '<boolProp name="HTTPSampler.follow_redirects">no', 1), "L06"),
        "L06 intProp not numeric": (lambda s: s.replace('<intProp name="Assertion.test_type">8', '<intProp name="Assertion.test_type">equals', 1), "L06"),
        "L07 sampler without method": (lambda s: s.replace('<stringProp name="HTTPSampler.method">GET</stringProp>', "", 1), "L07"),
        "L08 regex that does not compile": (lambda s: s.replace("code=(.+?)", "code=((.+?)", 1), "L08"),
        "L08 invalid JSONPath": (lambda s: s.replace("$.deviceToken", "deviceToken", 1), "L08"),
        "L09 variable used before it is defined": (lambda s: s.replace("${c_accessToken}", "${c_accesToken}", 1), "L09"),
        "L09 unknown JMeter function": (lambda s: s.replace("${__UUID()}", "${__GUID()}", 1), "L09"),
        "L10 unbalanced braces": (lambda s: s.replace("${c_code}", "${c_code", 1), "L10"),
    }
    for name, (mut, code) in cases.items():
        broken = mut(jmx)
        if broken == jmx:
            check(False, f"{name}: mutation did not apply")
            continue
        rr = lint(broken)
        hit = [e for e in rr.errors if e.startswith(code)]
        check(not rr.ok and hit, f"catches {name}" + (f"  ->  {hit[0][:100]}" if hit else ""))

    for path in glob.glob(os.path.join(ROOT, "samples", "*.jmx")):
        check(lint(open(path, encoding="utf-8").read()).ok, f"sample {os.path.basename(path)} passes")
    print("\nRESULT:", "FAILED" if failed else "ALL CHECKS PASSED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
