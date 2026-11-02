---
name: mobile-perf-testing
description: Record an Android Emulator or iOS Simulator app journey and turn it into a correlated, parameterised, correctly named JMeter script with the mobperf MCP server (plus Mobile MCP to tap through the app). Use when someone asks to record, script or performance-test a mobile app.
---

# Mobile performance testing with MobPerf Studio

You drive two MCP servers:
- **mobperf** (this repo): emulator/simulator setup, proxy, CA certificate, recording, step names, script generation, JMX checkpoint, validation.
- **mobile-mcp** (`npx -y @mobilenext/mobile-mcp@latest`): taps, types and reads the app screen. If it is not connected, ask the user to perform each action on the emulator and tell you when done.

## 0. Before you start - ask only what you cannot find out
- App: package name (Android) or bundle id (iOS), and the .apk / simulator .app build if it is not installed.
- The business journey, as ordered steps (e.g. Launch, Login, Search, Add to cart, Checkout).
- Two test accounts (a second user makes the correlation diff exact). Never put real customer data in a script.
- Test environment only. Never record or load-test production without written approval.

## 1. Environment
1. `environment_check` - fix anything missing using its hints.
2. `list_devices`.
3. Android: `start_android_emulator(avd)` (keeps `-writable-system`). Use a **Google APIs** image, not Google Play.
   iOS: `boot_ios_simulator(name)` (macOS only).

## 2. One-time HTTPS setup per device
1. `start_recording(session_name="setup")` - creates the mitmproxy CA on first run.
2. `install_ca_cert(device)`. Android below 14 reboots the emulator; Android 14+ must be re-run after every emulator restart.
3. `stop_recording`.

## 3. Record (do this twice: user 1, then user 2)
1. `start_recording(session_name="<app>_<user>")`
2. `configure_device_proxy(device)`
3. `launch_app(app_id, device, clear_data=true)` - fresh first launch so login/registration calls are captured.
4. For **every** step: call `mark_step("<Step Name>")` **before** the tap that triggers it, then perform the action (mobile-mcp or the user) and wait until the screen has finished loading.
   - Name steps by business intent in Title Case: "Login", "View Statement", "Fund Transfer". They become `MOB_<App>_<NN>_<StepName>` transactions.
   - Keep the journey identical between the two recordings (same screens, same order); only the user/test data should differ.
5. `recording_status` - check requests are arriving. If `possible_pinning` lists the app's own API host, stop and read section 6.
6. `stop_recording`, then `clear_device_proxy(device)` (always for iOS - it is the Mac's proxy).

## 4. Generate
`generate_script(recording=<har1>, second_recording=<har2>, app_name="<App>", run_validation=true)`
Body checks are on by default (`fail_if_contains="default"`, from rules/assertions.yaml). If the app has its own error
format, pass it, e.g. `fail_if_contains='"status":"FAILED"|SESSION_EXPIRED|"error":"'`; `"none"` turns them off.
Workload: `think_ms=-1` keeps the recorded think time per step, `think_dev_pct=20` adds random ±20%, `pacing_ms=60000`
sets a minimum iteration time, `data_mode="unique"` uses each CSV row once (ask the user how many test accounts they have).
For a browser / API recording (not mobile) pass `app_type="web"` so names become WEB_<App>_<NN>_<Step>.
- Add `exclude_domains` for any third-party SDK hosts that still appear, or `include_hosts` to keep only the app's API domains.
- Read back to the user: transactions, the counts of correlations / parameters / generated values, coverage %, and every flag.

## 5. Checkpoints - never hand over a script that fails these
1. `check_jmx(jmx)` must say `ready_for_jmeter_gui: true`. It runs 10 structural checks (well-formed XML, no illegal characters, hashTree pairing, testclass/guiclass, placement, property syntax, required properties, regex/JSONPath, variables defined before use, balanced `${}`) and, when JMeter is installed, loads the plan in JMeter itself.
2. `validate_script(jmx, csv, iterations=2)` must pass. For each failure, look at the request just before it: usually a value that needs a correlation the recording could not show (rerun with a second recording), or test data that is single-use (add CSV rows).
3. Report honestly. If a blocker flag remains (on-device HMAC signature, device attestation, real OTP, gRPC/protobuf), say exactly which request and what is needed - a test-environment bypass, the signing logic for a JSR223 PreProcessor, or a JMeter gRPC plugin. Do not claim 100% when a blocker exists.

## 6. Troubleshooting
- **No HTTPS traffic, app shows network error**: CA not trusted. Android 7+ apps ignore user CAs - the CA must be a system CA (section 2) or the build must have a `network_security_config` trusting user CAs.
- **TLS failures on the app's API host**: SSL pinning. Ask for a perf/debug build with pinning disabled.
- **Only SDK traffic recorded**: the app uses a VPN-style or QUIC connection - ask devs to disable QUIC/HTTP3 in the test build.
- **Different request counts between the two recordings**: the journeys diverged (e.g. one-time onboarding screen). Record both again from the same starting state (`clear_data=true`).

## 7. Hand-over
Give the user the .jmx, the CSV (ask them to add real test accounts, one row per virtual user), the HTML report and HOW_TO_RUN.txt. Run command:
`jmeter -n -t MOB_<App>.jmx -l run.jtl -e -o report -Jusers=50 -Jrampup=300 -Jloops=10`
