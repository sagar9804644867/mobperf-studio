"""mobperf-mcp - MCP server for mobile performance-test recording and script generation.

Works with the free Android Emulator (Android SDK, Windows/macOS/Linux) and the free iOS Simulator
(Xcode, macOS). Pair it with Mobile MCP (`npx -y @mobilenext/mobile-mcp@latest`) so the agent can
tap through the app while this server records, names steps and builds the JMeter script.

Run:   python -m mcp_server.server          (from the repo root)
Claude Code:  claude mcp add mobperf -- python -m mcp_server.server
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:  # MCP Python SDK 2.x
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # MCP Python SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from engine.jmx_lint import jmeter_load_check, lint  # noqa: E402
from engine.pipeline import Options, generate_files  # noqa: E402
from engine.validator import find_jmeter, validate  # noqa: E402

HOME = os.path.join(os.path.expanduser("~"), ".mobperf")
STATE = os.path.join(HOME, "state.json")
ADDON = os.path.join(ROOT, "mcp_server", "capture_addon.py")
CA_DIR = os.path.join(os.path.expanduser("~"), ".mitmproxy")
IS_WIN = os.name == "nt"

server = _Server("mobperf", instructions=(
    "Mobile performance-test recorder. Typical flow: environment_check -> list_devices -> start/boot device -> "
    "start_recording -> configure_device_proxy -> install_ca_cert (first time) -> launch_app -> mark_step before each "
    "screen/action -> stop_recording. Record the same journey twice with different test users, then generate_script "
    "with both recordings, then check_jmx and validate_script."))


# ------------------------------------------------------------------ helpers
def _state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(st: dict):
    os.makedirs(HOME, exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2)


def _run(cmd, timeout=120) -> dict:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"cmd": " ".join(cmd), "code": p.returncode, "out": (p.stdout + p.stderr).strip()[-3000:]}
    except FileNotFoundError:
        return {"cmd": " ".join(cmd), "code": 127, "out": f"{cmd[0]} not found on PATH"}
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "code": 124, "out": "timed out"}


def _sdk_tool(name: str, sub: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        base = os.environ.get(env)
        if base:
            for ext in ("", ".exe", ".bat"):
                p = os.path.join(base, sub, name + ext)
                if os.path.exists(p):
                    return p
    default = {"Darwin": "~/Library/Android/sdk", "Windows": "~/AppData/Local/Android/Sdk", "Linux": "~/Android/Sdk"}.get(platform.system())
    if default:
        for ext in ("", ".exe"):
            p = os.path.join(os.path.expanduser(default), sub, name + ext)
            if os.path.exists(p):
                return p
    return None


def adb() -> str:
    return _sdk_tool("adb", "platform-tools") or "adb"


def _adb(serial, *args, timeout=120):
    cmd = [adb()] + (["-s", serial] if serial else []) + list(args)
    return _run(cmd, timeout)


def _spawn(cmd, log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    log = open(log_path, "a", encoding="utf-8")
    kw = {"creationflags": 0x00000200} if IS_WIN else {"start_new_session": True}
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, **kw)


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if IS_WIN:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ca_pem() -> str:
    return os.path.join(CA_DIR, "mitmproxy-ca-cert.pem")


def _subject_hash_old(pem_path: str) -> str:
    """Android's cert file name = OpenSSL subject_hash_old (MD5 of DER subject, first 4 bytes little-endian)."""
    from cryptography import x509
    with open(pem_path, "rb") as fh:
        cert = x509.load_pem_x509_certificate(fh.read())
    d = hashlib.md5(cert.subject.public_bytes()).digest()
    return f"{int.from_bytes(d[:4], 'little'):08x}"


# ------------------------------------------------------------------ tools
@server.tool()
def environment_check() -> dict:
    """Check which tools are installed (Android SDK, Xcode simulator, mitmproxy, JMeter, Java) and how to fix missing ones."""
    res = {
        "os": platform.platform(),
        "adb": _sdk_tool("adb", "platform-tools"),
        "android_emulator": _sdk_tool("emulator", "emulator"),
        "xcrun_simctl": shutil.which("xcrun") if platform.system() == "Darwin" else None,
        "mitmdump": shutil.which("mitmdump"),
        "jmeter": find_jmeter(),
        "java": shutil.which("java"),
        "mitmproxy_ca": _ca_pem() if os.path.exists(_ca_pem()) else None,
    }
    hints = []
    if not res["adb"] or not res["android_emulator"]:
        hints.append("Android: install Android Studio (free) or the command-line SDK; set ANDROID_HOME. Create an AVD with a 'Google APIs' image (not 'Google Play') so the proxy certificate can be installed as a system CA.")
    if platform.system() == "Darwin" and not res["xcrun_simctl"]:
        hints.append("iOS: install Xcode from the App Store, then run: xcode-select --install")
    if platform.system() != "Darwin":
        hints.append("iOS Simulator needs macOS. On Windows/Linux record iOS on a Mac, or import a HAR exported from Appetize.io / Proxyman.")
    if not res["mitmdump"]:
        hints.append("pip install mitmproxy")
    if not res["jmeter"]:
        hints.append("Optional: install Apache JMeter 5.6+ and set JMETER_HOME so validate_script/check_jmx run real JMeter (otherwise the built-in replayer and structure checkpoint are used).")
    if not res["mitmproxy_ca"]:
        hints.append("Run start_recording once - mitmproxy creates its CA certificate on first start.")
    res["hints"] = hints
    return res


@server.tool()
def list_devices() -> dict:
    """List running Android emulators/devices, available Android AVDs and iOS simulators."""
    out = {"android_running": [], "android_avds": [], "ios_simulators": []}
    r = _adb(None, "devices", "-l")
    for line in r["out"].splitlines()[1:]:
        if line.strip() and ("\tdevice" in line or " device " in line):
            out["android_running"].append(line.split()[0])
    emu = _sdk_tool("emulator", "emulator")
    if emu:
        out["android_avds"] = [x for x in _run([emu, "-list-avds"])["out"].splitlines() if x and not x.startswith("INFO")]
    if platform.system() == "Darwin" and shutil.which("xcrun"):
        r = _run(["xcrun", "simctl", "list", "devices", "available", "--json"])
        try:
            for rt, devs in json.loads(r["out"]).get("devices", {}).items():
                for d in devs:
                    out["ios_simulators"].append({"name": d["name"], "udid": d["udid"], "state": d["state"],
                                                  "runtime": rt.split(".")[-1]})
        except ValueError:
            pass
    return out


@server.tool()
def start_android_emulator(avd: str, writable_system: bool = True, wait_seconds: int = 240) -> dict:
    """Boot an Android AVD. writable_system=True is needed to install the proxy CA as a system certificate."""
    emu = _sdk_tool("emulator", "emulator")
    if not emu:
        return {"ok": False, "error": "Android emulator not found. Install the Android SDK and set ANDROID_HOME."}
    cmd = [emu, "-avd", avd, "-no-snapshot-load", "-netdelay", "none", "-netspeed", "full"]
    if writable_system:
        cmd.append("-writable-system")
    proc = _spawn(cmd, os.path.join(HOME, "logs", f"emulator_{avd}.log"))
    deadline = time.time() + wait_seconds
    serial = None
    while time.time() < deadline:
        r = _adb(None, "devices")
        ems = [l.split()[0] for l in r["out"].splitlines() if l.startswith("emulator-") and "\tdevice" in l]
        if ems:
            serial = ems[-1]
            if _adb(serial, "shell", "getprop", "sys.boot_completed", timeout=10)["out"].strip() == "1":
                return {"ok": True, "serial": serial, "pid": proc.pid}
        time.sleep(3)
    return {"ok": False, "serial": serial, "error": "boot not completed in time - check the emulator window/log"}


@server.tool()
def boot_ios_simulator(name_or_udid: str) -> dict:
    """Boot an iOS Simulator (macOS + Xcode only) and open the Simulator app."""
    if platform.system() != "Darwin":
        return {"ok": False, "error": "The iOS Simulator only runs on macOS."}
    r = _run(["xcrun", "simctl", "boot", name_or_udid])
    _run(["open", "-a", "Simulator"])
    ok = r["code"] == 0 or "Booted" in r["out"]
    return {"ok": ok, "detail": r["out"]}


@server.tool()
def start_recording(session_name: str = "", port: int = 8080, listen_host: str = "0.0.0.0") -> dict:
    """Start mitmproxy with the MobPerf capture addon. Traffic is saved continuously to <session>/recording.har."""
    st = _state()
    if _alive(st.get("pid")):
        return {"ok": False, "error": f"A recording is already running (session {st.get('session')}). Call stop_recording first."}
    if not shutil.which("mitmdump"):
        return {"ok": False, "error": "mitmdump not found - pip install mitmproxy"}
    name = re.sub(r"[^A-Za-z0-9_\-]+", "_", session_name) or time.strftime("rec_%Y%m%d_%H%M%S")
    sess = os.path.join(HOME, "sessions", name)
    os.makedirs(sess, exist_ok=True)
    with open(os.path.join(sess, "step.txt"), "w", encoding="utf-8") as fh:
        fh.write("Launch")
    cmd = ["mitmdump", "-q", "--listen-host", listen_host, "-p", str(port), "-s", ADDON,
           "--set", f"mobperf_dir={sess}", "--set", "ssl_insecure=true", "--set", "connection_strategy=lazy"]
    proc = _spawn(cmd, os.path.join(sess, "mitmdump.log"))
    time.sleep(2.5)
    if proc.poll() is not None:
        return {"ok": False, "error": "mitmdump exited - see " + os.path.join(sess, "mitmdump.log")}
    _save({"pid": proc.pid, "port": port, "session": name, "dir": sess, "started": time.time()})
    return {"ok": True, "session": name, "dir": sess, "proxy_port": port,
            "android_proxy": f"10.0.2.2:{port}", "ios_proxy": f"127.0.0.1:{port}",
            "next": "configure_device_proxy, install_ca_cert (first time only), launch_app, then mark_step before each action"}


@server.tool()
def configure_device_proxy(device: str = "android", serial: str = "", port: int = 0, macos_network_service: str = "Wi-Fi") -> dict:
    """Point the emulator/simulator at the recording proxy. device='android' or 'ios'.
    iOS Simulator uses the Mac's proxy settings, so this changes the macOS proxy for the given network service."""
    port = port or _state().get("port", 8080)
    if device.lower() == "android":
        r = _adb(serial or None, "shell", "settings", "put", "global", "http_proxy", f"10.0.2.2:{port}")
        return {"ok": r["code"] == 0, "detail": r, "note": "10.0.2.2 is the emulator's alias for your computer."}
    if platform.system() != "Darwin":
        return {"ok": False, "error": "iOS Simulator proxy is configured on macOS."}
    out = [_run(["networksetup", "-setwebproxy", macos_network_service, "127.0.0.1", str(port)]),
           _run(["networksetup", "-setsecurewebproxy", macos_network_service, "127.0.0.1", str(port)])]
    return {"ok": all(o["code"] == 0 for o in out), "detail": out,
            "note": "Remember clear_device_proxy(device='ios') afterwards - this is the Mac's system proxy."}


@server.tool()
def clear_device_proxy(device: str = "android", serial: str = "", macos_network_service: str = "Wi-Fi") -> dict:
    """Remove the proxy from the emulator (or the Mac, for the iOS Simulator)."""
    if device.lower() == "android":
        r = _adb(serial or None, "shell", "settings", "put", "global", "http_proxy", ":0")
        return {"ok": r["code"] == 0, "detail": r}
    out = [_run(["networksetup", "-setwebproxystate", macos_network_service, "off"]),
           _run(["networksetup", "-setsecurewebproxystate", macos_network_service, "off"])]
    return {"ok": all(o["code"] == 0 for o in out), "detail": out}


@server.tool()
def install_ca_cert(device: str = "android", serial: str = "", reboot: bool = True) -> dict:
    """Install the mitmproxy CA so HTTPS can be recorded.
    Android: installs as a SYSTEM certificate (needs a 'Google APIs' image started with -writable-system).
    Android 14+: injects into the Conscrypt APEX store (valid until the next reboot).
    iOS Simulator: adds the CA to the booted simulator's keychain."""
    pem = _ca_pem()
    if not os.path.exists(pem):
        return {"ok": False, "error": "mitmproxy CA not created yet - call start_recording once first."}
    if device.lower() == "ios":
        r = _run(["xcrun", "simctl", "keychain", "booted", "add-root-cert", pem])
        return {"ok": r["code"] == 0, "detail": r}
    h = _subject_hash_old(pem)
    steps = []
    sdk = _adb(serial or None, "shell", "getprop", "ro.build.version.sdk")["out"].strip()
    api = int(sdk) if sdk.isdigit() else 0
    steps.append(_adb(serial or None, "root"))
    time.sleep(2)
    tmp_dev = f"/data/local/tmp/{h}.0"
    steps.append(_adb(serial or None, "push", pem, tmp_dev))
    if api >= 34:
        script = (
            "mkdir -p -m 700 /data/local/tmp/mp-ca && cp /apex/com.android.conscrypt/cacerts/* /data/local/tmp/mp-ca/ && "
            "mount -t tmpfs tmpfs /system/etc/security/cacerts && "
            "mv /data/local/tmp/mp-ca/* /system/etc/security/cacerts/ && "
            f"cp {tmp_dev} /system/etc/security/cacerts/ && "
            "chown root:root /system/etc/security/cacerts/* && chmod 644 /system/etc/security/cacerts/* && "
            "chcon u:object_r:system_file:s0 /system/etc/security/cacerts/* && "
            "for p in $(pidof zygote zygote64); do nsenter --mount=/proc/$p/ns/mnt -- "
            "mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts; done && "
            "for p in $(ps -A -o PID,PPID | awk '$2 ~ /^('$(pidof zygote zygote64 | tr ' ' '|')')$/ {print $1}'); do "
            "nsenter --mount=/proc/$p/ns/mnt -- mount --bind /system/etc/security/cacerts /apex/com.android.conscrypt/cacerts; done")
        steps.append(_adb(serial or None, "shell", script, timeout=180))
        ok = steps[-1]["code"] == 0
        return {"ok": ok, "api": api, "cert": f"{h}.0", "steps": steps,
                "note": "Android 14+: CA injected into the Conscrypt APEX for this boot. Re-run after every emulator restart, then relaunch the app."}
    steps.append(_adb(serial or None, "remount", timeout=120))
    if "reboot" in steps[-1]["out"].lower() and "verity" in steps[-1]["out"].lower():
        steps.append(_adb(serial or None, "disable-verity"))
        return {"ok": False, "api": api, "steps": steps,
                "next": "Verity was disabled - reboot the emulator (keep -writable-system), then call install_ca_cert again."}
    steps.append(_adb(serial or None, "shell", f"cp {tmp_dev} /system/etc/security/cacerts/{h}.0 && chmod 644 /system/etc/security/cacerts/{h}.0"))
    ok = steps[-1]["code"] == 0
    if ok and reboot:
        steps.append(_adb(serial or None, "reboot"))
    return {"ok": ok, "api": api, "cert": f"{h}.0", "steps": steps,
            "note": "System CA installed. After reboot, keep starting the AVD with -writable-system so it persists."}


@server.tool()
def install_app(path: str, device: str = "android", serial: str = "") -> dict:
    """Install an .apk on the Android emulator, or a simulator build (.app) on the booted iOS Simulator."""
    if device.lower() == "ios":
        return {"detail": _run(["xcrun", "simctl", "install", "booted", path], 300)}
    return {"detail": _adb(serial or None, "install", "-r", "-g", path, timeout=300)}


@server.tool()
def launch_app(app_id: str, device: str = "android", serial: str = "", clear_data: bool = False) -> dict:
    """Launch the app under test (Android package name or iOS bundle id). clear_data=True gives a fresh first-launch journey."""
    if device.lower() == "ios":
        if clear_data:
            _run(["xcrun", "simctl", "terminate", "booted", app_id])
        return {"detail": _run(["xcrun", "simctl", "launch", "booted", app_id])}
    if clear_data:
        _adb(serial or None, "shell", "pm", "clear", app_id)
    return {"detail": _adb(serial or None, "shell", "monkey", "-p", app_id, "-c", "android.intent.category.LAUNCHER", "1")}


@server.tool()
def mark_step(name: str) -> dict:
    """Name the business step that starts now (e.g. 'Login', 'Search Product'). Call BEFORE the action that triggers it.
    Every request until the next mark_step becomes part of this JMeter transaction."""
    st = _state()
    if not st.get("dir"):
        return {"ok": False, "error": "No active recording - call start_recording first."}
    with open(os.path.join(st["dir"], "step.txt"), "w", encoding="utf-8") as fh:
        fh.write(name.strip() or "Step")
    return {"ok": True, "step": name, "session": st.get("session")}


def _har_summary(path):
    try:
        with open(path, encoding="utf-8") as fh:
            har = json.load(fh)
    except (OSError, ValueError):
        return {"requests": 0, "steps": [], "hosts": []}
    entries = har["log"]["entries"]
    hosts = {}
    for e in entries:
        h = re.sub(r"^https?://([^/:]+).*", r"\1", e["request"]["url"])
        hosts[h] = hosts.get(h, 0) + 1
    return {"requests": len(entries), "steps": [p["title"] for p in har["log"].get("pages", [])],
            "hosts": sorted(hosts.items(), key=lambda x: -x[1])[:20]}


@server.tool()
def recording_status() -> dict:
    """Show what has been captured so far: request count, steps, hosts, and TLS failures (possible SSL pinning)."""
    st = _state()
    if not st.get("dir"):
        return {"recording": False}
    info = _har_summary(os.path.join(st["dir"], "recording.har"))
    info.update({"recording": _alive(st.get("pid")), "session": st["session"],
                 "current_step": open(os.path.join(st["dir"], "step.txt"), encoding="utf-8").read().strip()})
    info["possible_pinning"] = detect_pinning().get("hosts", [])
    return info


@server.tool()
def detect_pinning() -> dict:
    """List hosts whose TLS handshake with the proxy failed - the usual sign of certificate pinning."""
    st = _state()
    path = os.path.join(st.get("dir", ""), "tls_failures.txt")
    hosts = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            parts = line.strip().split("\t")
            if len(parts) == 2:
                hosts[parts[1]] = hosts.get(parts[1], 0) + 1
    advice = ("No TLS failures." if not hosts else
              "These hosts rejected the proxy certificate. If they are your app's API hosts the app uses SSL pinning or does not trust "
              "the CA: ask the dev team for a debug/perf build with a network_security_config that trusts user/system CAs "
              "(Android) or pinning disabled (iOS). SDK hosts (Google, Apple, analytics) can be ignored.")
    return {"hosts": sorted(hosts.items(), key=lambda x: -x[1]), "advice": advice}


@server.tool()
def stop_recording() -> dict:
    """Stop the proxy and return the HAR path and a summary of the recording."""
    st = _state()
    if not st.get("dir"):
        return {"ok": False, "error": "No recording session."}
    pid = st.get("pid")
    if _alive(pid):
        try:
            if IS_WIN:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass
    har = os.path.join(st["dir"], "recording.har")
    info = _har_summary(har)
    _save({"last_session": st.get("session"), "last_har": har})
    return {"ok": os.path.exists(har), "har": har, **info, "pinning": detect_pinning().get("hosts", []),
            "next": "Record the same journey again with a second test user, then generate_script(recording, second_recording)."}


def _fail_patterns(spec: str):
    spec = (spec or "default").strip()
    if spec.lower() == "default":
        return None
    if spec.lower() == "none":
        return []
    return [p for p in spec.split("|") if p]


@server.tool()
def generate_script(recording: str, app_name: str, second_recording: str = "", out_dir: str = "",
                    exclude_domains: str = "", include_hosts: str = "", run_validation: bool = False,
                    app_type: str = "mobile", fail_if_contains: str = "default", think_ms: int = -1,
                    think_dev_pct: int = 20, pacing_ms: int = 0, data_mode: str = "shared") -> dict:
    """Generate a correlated, parameterised, named JMeter script (.jmx + CSV + HTML report) from 1 or 2 HAR/.flow recordings.
    exclude_domains / include_hosts: comma-separated host suffixes. run_validation replays 1 user x 2 iterations.
    app_type: "mobile" (MOB_ names) or "web" (WEB_ names, for browser / API recordings).
    fail_if_contains: "default" = rules/assertions.yaml, "none" = no body checks, or a '|'-separated list of texts that
    fail a request when found in its response body (e.g. 'token expired|"error":"|Unauthorized').
    think_ms: -1 = recorded think time per step, otherwise a fixed value. think_dev_pct: random +/- % (0 = exact).
    pacing_ms: minimum iteration duration (0 = off). data_mode: "shared" (reuse CSV rows) or "unique" (each row once)."""
    out_dir = out_dir or os.path.join(HOME, "scripts", re.sub(r"[^A-Za-z0-9]+", "_", app_name))
    opts = Options(app_name=app_name,
                   extra_exclude=[d.strip() for d in exclude_domains.split(",") if d.strip()],
                   include_hosts=[d.strip() for d in include_hosts.split(",") if d.strip()],
                   kind="web" if app_type.strip().lower() == "web" else "mobile",
                   fail_patterns=_fail_patterns(fail_if_contains),
                   think_ms_fixed=None if think_ms is None or think_ms < 0 else think_ms,
                   think_dev_pct=think_dev_pct, pacing_ms=pacing_ms,
                   data_mode="unique" if data_mode.strip().lower() == "unique" else "shared")
    res, paths = generate_files(recording, second_recording or None, out_dir, opts, validate=run_validation)
    out = {"files": paths, "summary": {k: v for k, v in res.summary.items() if k not in ("jmx_check",)},
           "jmx_checkpoint": {"ok": res.summary["jmx_check"]["ok"], "errors": res.summary["jmx_check"]["errors"]},
           "transactions": [st.label(res.prefix, res.app, "{prefix}_{app}_{nn:02d}_{step}") for st in res.steps],
           "variables": [{"name": v.name, "kind": v.kind, "how": (v.extractor or {}).get("expr") or v.generator or "CSV"}
                         for v in res.analysis.variables],
           "flags": [f"{f.severity}: {f.message}" for f in res.analysis.flags]}
    if res.validation:
        out["validation"] = {"engine": res.validation["engine"], "passed": res.validation["passed"],
                             "failures": [r for r in res.validation["results"] if not r.get("ok")][:20]}
    return out


@server.tool()
def check_jmx(jmx_path: str) -> dict:
    """JMX structure checkpoint: will this file open in the JMeter GUI? Runs 10 structural checks and, if JMeter is
    installed, asks JMeter itself to load the plan (0 users, no traffic). Works on any .jmx, not only generated ones."""
    with open(jmx_path, encoding="utf-8", errors="replace") as fh:
        r = lint(fh.read())
    return {"ready_for_jmeter_gui": r.ok, **r.as_dict(), "jmeter_load": jmeter_load_check(jmx_path)}


@server.tool()
def validate_script(jmx_path: str, csv_path: str = "", iterations: int = 2) -> dict:
    """Run the script 1 user x N iterations (real JMeter if installed, else the built-in replayer) and report failures."""
    res = validate(jmx_path, csv_path or None, iterations)
    fails = [r for r in res["results"] if not r.get("ok")]
    return {"engine": res["engine"], "passed": res["passed"], "samples": len(res["results"]), "failures": fails[:25]}


def main():
    server.run()


if __name__ == "__main__":
    main()
