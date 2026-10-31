# 📱 MobPerf Studio

**Mobile emulator recording → ready-to-run JMeter script.**
Record any Android Emulator or iOS Simulator app journey, and get a `.jmx` that is correlated, parameterised, correctly named, and checked to open in the JMeter GUI. Driven by an MCP server, so Claude (or any MCP client) can do the whole recording for you.

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
| **Naming** | Transactions `MOB_<App>_<NN>_<Step>` from your `mark_step` names (or detected from time gaps / GraphQL operation names). Samplers `<NN>_<sub>_<METHOD>_<endpoint>`. Variables `c_*` correlated, `p_*` CSV, `g_*` generated, `host_*` environment. |
| **Clean script** | 70+ SDK/telemetry domains removed (Firebase, Crashlytics, AppsFlyer, Branch, Sentry, Datadog RUM...), static assets removed, cookie manager, per-step think time, response-code assertions, all hosts / users / ramp-up / loops overridable with `-J`. |
| **JMX checkpoint** | 10 structural checks that catch "Problem loading XML" before JMeter does + a real JMeter load (0 users) when JMeter is installed. |
| **Honest flags** | On-device HMAC signatures, device attestation, real OTPs, gRPC/protobuf, recorded errors → reported as blockers, never faked. |

### Why record twice?
Record the same journey with two different test users. Every value that differs between the runs is **proven** dynamic, so nothing is guessed. The report shows a *dynamic values resolved* score (for example 16/16 = 100%). With one recording the engine still works, using heuristics.

## Proven on two very different apps (`tests/`)

| Test | App style | Result |
|---|---|---|
| `test_e2e.py` | **MobiBank**: REST + OAuth (302 `Location`), cookie + CSRF, device registration, Bearer token, path ids, cursor paging, `_ts` timestamp, one-time nonce + `X-Request-ID`, txnRef in an **HTML** hidden field. Backend rejects every stale / replayed / foreign value. Recorded through real mitmproxy with step markers. | 10 requests, 9 correlations, 5 params, 5 generated, **100% dynamic resolved**. Replayed 3 iterations including a **user who was never recorded**: 30/30 pass, 0 backend rejections. A raw, uncorrelated replay is rejected. |
| `test_e2e_shop.py` | **ShopKart**: iOS-style GraphQL, JWT, numeric customer id, cart id + weak **ETag / If-Match**, checkout token in **XML**, uppercase idempotency UUID, ISO request time, on-device `X-Signature`. HAR **without step markers** (like Charles / Proxyman). | 4 steps found from timing and named from GraphQL operations, `X-Signature` flagged as blocker, 21/21 replayed samples pass incl. a 3rd unrecorded user. |
| `test_jmx_checkpoint.py` | 13 deliberately broken JMX files (control characters, missing hashTree, wrong guiclass, bad booleans, missing method, broken regex/JSONPath, undefined variables, unknown functions, unbalanced `${`). | All 13 caught with the exact line/element. |

Run them: `pip install -r requirements-local.txt` then `python tests/test_e2e.py`, `python tests/test_e2e_shop.py`, `python tests/test_jmx_checkpoint.py`. GitHub Actions runs all three on every push.

## Three ways to use it

### 1. Streamlit app (no install)
Upload one or two HAR / mitmproxy `.flow` files, review correlations, rename steps, edit the CSV, check any `.jmx`, and download the bundle. Try it with the bundled MobiBank sample.

### 2. Claude + MCP (fully automated recording)
```bash
pip install -r requirements-local.txt
claude mcp add mobperf -- python -m mcp_server.server          # run from the repo folder
claude mcp add mobile-mcp -- npx -y @mobilenext/mobile-mcp@latest
```
Copy `skills/mobile-perf-testing/` into your skills folder (Claude Code: `~/.claude/skills/`), then ask:
> Record com.mobibank on my Pixel_7 AVD: Launch, Login, View Statement, Fund Transfer. Record it with alice and bob, generate the JMeter script and run both checkpoints.

**MCP tools**: `environment_check`, `list_devices`, `start_android_emulator`, `boot_ios_simulator`, `start_recording`, `configure_device_proxy`, `install_ca_cert`, `install_app`, `launch_app`, `mark_step`, `recording_status`, `detect_pinning`, `stop_recording`, `clear_device_proxy`, `generate_script`, `check_jmx`, `validate_script`.

### 3. Command line
```bash
python -m mcp_server.cli generate userA.har userB.har --app MyApp --out out --validate
python -m mcp_server.cli check out/MOB_MyApp.jmx
python -m mcp_server.cli validate out/MOB_MyApp.jmx --csv out/MOB_MyApp_data.csv
```

Free emulator setup (Android + iOS, CA certificate, pinning): [docs/EMULATOR_SETUP.md](docs/EMULATOR_SETUP.md).
Publish to GitHub and Streamlit Cloud: [docs/DEPLOY_GITHUB_STREAMLIT.md](docs/DEPLOY_GITHUB_STREAMLIT.md).

## Project layout
```
engine/          model, rules, correlate, naming, jmx_builder, jmx_lint (checkpoint), validator, report, pipeline
mcp_server/      server.py (MCP), capture_addon.py (mitmproxy), cli.py
skills/          mobile-perf-testing/SKILL.md (Claude skill)
rules/           sdk_noise.yaml, naming.yaml, correlation_rules.yaml  - edit to tune
samples/         recordings + generated script from the MobiBank and ShopKart test apps
tests/           strict mock apps + end-to-end tests
streamlit_app.py the web UI
```

## Limits (by design, flagged in the report)
- Values computed on the device from secrets (HMAC request signing, Play Integrity / App Attest) cannot come from traffic. Use a test-environment bypass, or add the signing logic as a JSR223 PreProcessor.
- SSL-pinned apps need a debug/perf build that trusts the proxy CA.
- gRPC / protobuf needs a JMeter gRPC plugin.
- Real OTPs need a fixed test OTP or an OTP-reader API.

Only record and load-test environments you are authorised to test, and never commit recordings that contain real user data.

## License
MIT
