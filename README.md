# 📱🌐 MobPerf Studio

**Mobile or web recording → ready-to-run JMeter script.**
Two sections, one engine:

- **📱 Mobile app**: record an Android Emulator or iOS Simulator journey (driven by an MCP server, so Claude or any MCP client can do the recording).
- **🌐 Web / API app**: upload a HAR from browser DevTools, Postman, Fiddler or any proxy, or record with the bundled Playwright recorder.

Both give you a `.jmx` that is correlated, parameterised, correctly named, checks status **and response body**, and is verified to open in the JMeter GUI.

```
 Android Emulator / iOS Simulator
        │  proxy + CA certificate installed by the MCP server
        ▼
 mitmproxy + MobPerf capture addon  ◄── mobperf MCP (mark_step "Login", "Search"...)
        │                                  + Mobile MCP taps through the app
        ▼
 recording_userA.har  +  recording_userB.har
        │
        ▼  engine (same code in the MCP server, CLI and Streamlit app)
 noise filter → correlation → parameterisation → naming → JMX builder
        → JMX checkpoint (10 checks + real JMeter load) → 1 user × 2 iteration replay
        ▼
 MOB_<App>.jmx  +  MOB_<App>_data.csv  +  HTML report  +  HOW_TO_RUN.txt
```

## What you get in every script

| | |
|---|---|
| **Correlation** | Every value the server sends back and the app reuses: JSON Extractor (JSON fields) or boundary Regex Extractor (HTML, XML, text, `Location`, `Set-Cookie`). Tokens, session ids, CSRF, OAuth codes, cart ids, ETags, paging cursors... |
| **Generated values** | UUIDs (lower/upper case), epoch ms/s timestamps, ISO dates, random nonces → recreated every iteration with JMeter functions. |
| **Parameterisation** | Login, password, search terms, amounts, payee... → `CSV Data Set` with one row per recorded user. Add real test accounts. |
| **Assertions** | Recorded status code on every request, **plus body checks**: the request fails when the response contains `"error":"`, `token expired`, `Unauthorized`... (editable list, `rules/assertions.yaml`), so an HTTP 200 carrying an error is caught. A text already present in the recorded response is not checked on that request (no false failures). **Correlated values are asserted too**: each extracted variable has a variable-scope assertion, so a correlation that finds nothing fails the request that should have produced it. |
| **Workload** | **Think time** per step (recorded, or one fixed value) with **random ± % variation** (default 20%) so users do not hit the server in lock-step; excluded from transaction times. **Pacing**: minimum time per iteration, the user waits only for what is left. **Test data**: *shared* (rows reused) or *unique* (each CSV row once, the user stops when rows run out). All baked in as defaults and overridable with `-Jthink_ms`, `-Jthink_dev_pct`, `-Jpacing_ms`. |
| **Naming** | Transactions `MOB_<App>_<NN>_<Step>` (mobile) or `WEB_<App>_<NN>_<Step>` (web) from your `mark_step` names (or detected from time gaps / GraphQL operation names). Samplers `<NN>_<sub>_<METHOD>_<endpoint>`. Variables `c_*` correlated, `p_*` CSV, `g_*` generated, `host_*` environment. |
| **Clean script** | 70+ SDK/telemetry domains removed (Firebase, Crashlytics, AppsFlyer, Branch, Sentry, Datadog RUM...), static assets removed, cookie manager, all hosts / users / ramp-up / loops overridable with `-J`. |
| **JMX checkpoint** | 10 structural checks that catch "Problem loading XML" before JMeter does + a real JMeter load (0 users) when JMeter is installed. |
| **Honest flags** | On-device HMAC signatures, device attestation, real OTPs, gRPC/protobuf, recorded errors → reported as blockers, never faked. |

### Why record twice?
Record the same journey with two different test users. Every value that differs between the runs is **proven** dynamic, so nothing is guessed. The report shows a *dynamic values resolved* score (for example 16/16 = 100%). With one recording the engine still works, using heuristics.

## Tested on (`tests/`)

| Test | App style | Result |
|---|---|---|
| `test_e2e.py` | **MobiBank**: REST + OAuth (302 `Location`), cookie + CSRF, device registration, Bearer token, path ids, cursor paging, `_ts` timestamp, one-time nonce + `X-Request-ID`, txnRef in an **HTML** hidden field. Backend rejects every stale / replayed / foreign value. Recorded through real mitmproxy with step markers. | 10 requests, 9 correlations, 5 params, 5 generated, **100% dynamic resolved**. Replayed 3 iterations including a **user who was never recorded**: 30/30 pass, 0 backend rejections. A raw, uncorrelated replay is rejected. |
| `test_e2e_shop.py` | **ShopKart**: iOS-style GraphQL, JWT, numeric customer id, cart id + weak **ETag / If-Match**, checkout token in **XML**, uppercase idempotency UUID, ISO request time, on-device `X-Signature`. HAR **without step markers** (like Charles / Proxyman). | 4 steps found from timing and named from GraphQL operations, `X-Signature` flagged as blocker, 21/21 replayed samples pass incl. a 3rd unrecorded user. |
| `test_web_and_body.py` | **ShopWeb** (web): recorded in **real headless Chromium** via the Playwright recorder. One-time CSRF token in an HTML hidden field, cookie session after a 302, API token in an HTML `<meta>` tag used as `Authorization: Bearer` by page JavaScript. | 2 correlations from HTML, 100% dynamic resolved, replay passes incl. unrecorded user. **Negative controls**: HTTP 200 + `{"error":"token expired"}` fails the script (and passes with body checks off, showing the gap); a page without the token fails on the correlation assertion; `Unauthorized` that is part of the normal response is not turned into a check. |
| `test_har_robustness.py` | 17 real-world HAR quirks: HTTP/2 pseudo-headers (`:authority`), base64 bodies, form `params` without text, binary/PDF, non-UTF-8 and control characters, GraphQL batch arrays, SOAP/XML, a 2 MB body, CORS preflight, 304, redirects, 500, aborted requests, special characters in query strings, IPv6 host, empty HAR, noise-only HAR, page titles as steps, missing optional fields, invalid files. | No crash, every JMX passes the checkpoint. |
| `test_workload.py` | Think time, pacing and data mode measured on real requests: `-Jthink_ms=300` waits exactly 300 ms per step, ±50% gives random waits inside 200–600 ms, pacing 1500 ms makes each iteration last 1500 ms, `-J…=0` switches baked values off, unique data runs 2 of 3 iterations with 2 rows and stops cleanly. The generated **Groovy timer scripts are also run in real Groovy 4** with JMeter's bindings and parsed like JMeter's JSR223Timer. | All pass. |
| `test_jmx_checkpoint.py` | 13 deliberately broken JMX files (control characters, missing hashTree, wrong guiclass, bad booleans, missing method, broken regex/JSONPath, undefined variables, unknown functions, unbalanced `${`). | All 13 caught with the exact line/element. |

Run them: `pip install -r requirements-local.txt`, `python -m playwright install chromium`, then each `python tests/test_*.py`. GitHub Actions runs all six on every push.

**What is not covered**: these are realistic mock apps plus synthetic HAR quirks, not every app in existence. Not tested here: a real Android/iOS emulator, a real JMeter run (JMeter was not installable in the build sandbox; the JMX is checked against JMeter's own element and property names, and `cli check` loads it in your JMeter), and third-party production sites.

## Ways to use it

### 1. Streamlit app (no install)
Pick **📱 Mobile app** or **🌐 Web / API app** in the sidebar. Upload one or two HAR / mitmproxy `.flow` files, review correlations and assertions, edit the *fail if response contains* list, rename steps, edit the CSV, check any `.jmx`, and download the bundle. Each section has a bundled sample (MobiBank, ShopWeb).

### 2. Claude + MCP (fully automated recording)
```bash
pip install -r requirements-local.txt
claude mcp add mobperf -- python -m mcp_server.server          # run from the repo folder
claude mcp add mobile-mcp -- npx -y @mobilenext/mobile-mcp@latest
```
Copy `skills/mobile-perf-testing/` into your skills folder (Claude Code: `~/.claude/skills/`), then ask:
> Record com.mobibank on my Pixel_7 AVD: Launch, Login, View Statement, Fund Transfer. Record it with alice and bob, generate the JMeter script and run both checkpoints.

**MCP tools**: `environment_check`, `list_devices`, `start_android_emulator`, `boot_ios_simulator`, `start_recording`, `configure_device_proxy`, `install_ca_cert`, `install_app`, `launch_app`, `mark_step`, `recording_status`, `detect_pinning`, `stop_recording`, `clear_device_proxy`, `generate_script`, `check_jmx`, `validate_script`.

### 3. Web app recorder (Playwright, on your machine)
```bash
pip install -r requirements-local.txt && python -m playwright install chromium
python -m engine.web_recorder scenario_alice.json shop_alice.har      # add --headful to watch
python -m engine.web_recorder scenario_bob.json   shop_bob.har
```
A scenario is a JSON list of actions: `{"step": "Login"}`, `{"goto": url}`, `{"fill": [selector, value]}`, `{"click": selector}`, `{"press": "Enter"}`, `{"wait_ms": 500}`. Each `step` becomes a transaction. An example is on the *Record web app* page.

### 4. Command line
```bash
python -m mcp_server.cli generate userA.har userB.har --app MyApp --out out --validate
python -m mcp_server.cli generate shop_alice.har shop_bob.har --app Shop --type web --fail-pattern "token expired" --fail-pattern "SessionTimeout"
python -m mcp_server.cli generate userA.har --app MyApp --no-body-checks
python -m mcp_server.cli generate userA.har userB.har --app MyApp --think-dev-pct 30 --pacing-ms 60000 --data-mode unique
python -m mcp_server.cli check out/MOB_MyApp.jmx
python -m mcp_server.cli validate out/MOB_MyApp.jmx --csv out/MOB_MyApp_data.csv
```

Free emulator setup (Android + iOS, CA certificate, pinning): [docs/EMULATOR_SETUP.md](docs/EMULATOR_SETUP.md).
Publish to GitHub and Streamlit Cloud: [docs/DEPLOY_GITHUB_STREAMLIT.md](docs/DEPLOY_GITHUB_STREAMLIT.md).

## Project layout
```
engine/          model, rules, correlate, naming, jmx_builder, jmx_lint (checkpoint), validator, report, pipeline,
                 web_recorder (Playwright)
mcp_server/      server.py (MCP), capture_addon.py (mitmproxy), cli.py
skills/          mobile-perf-testing/SKILL.md (Claude skill)
rules/           sdk_noise.yaml, naming.yaml, correlation_rules.yaml, assertions.yaml  - edit to tune
samples/         recordings + generated script from the MobiBank, ShopKart and ShopWeb test apps
tests/           strict mock apps + end-to-end tests
streamlit_app.py the web UI
```

## Limits (by design, flagged in the report)
- Values computed on the device from secrets (HMAC request signing, Play Integrity / App Attest) cannot come from traffic. Use a test-environment bypass, or add the signing logic as a JSR223 PreProcessor.
- SSL-pinned apps need a debug/perf build that trusts the proxy CA.
- gRPC / protobuf needs a JMeter gRPC plugin.
- Real OTPs need a fixed test OTP or an OTP-reader API; CAPTCHA needs a test bypass.
- WebSockets / server-sent events are not converted (JMeter needs a WebSocket plugin).
- Body checks are plain text and case-sensitive. Keep the list short and specific to your app's error format.

Only record and load-test environments you are authorised to test, and never commit recordings that contain real user data.

## License
MIT
