# Free emulator setup for recording

The MCP server automates every step below. This page lists them so you can also do them by hand.

## Android Emulator (Windows / macOS / Linux, free)
1. Install **Android Studio** (or the command-line SDK) and set `ANDROID_HOME`.
   - Windows default: `%LOCALAPPDATA%\Android\Sdk`
   - macOS: `~/Library/Android/sdk`
2. Device Manager → *Create device* → choose a phone (e.g. Pixel 7) → choose a system image whose target says **Google APIs**.
   Do **not** use a *Google Play* image: those cannot be rooted, so the proxy CA cannot be installed as a system certificate.
3. Start it writable: `emulator -avd Pixel_7 -writable-system` (MCP: `start_android_emulator`).
4. Proxy: `adb shell settings put global http_proxy 10.0.2.2:8080` (MCP: `configure_device_proxy`). `10.0.2.2` is the emulator's name for your computer.
5. CA certificate (MCP: `install_ca_cert`, run once per device):
   - Android 7–13: root, remount and copy `<hash>.0` into `/system/etc/security/cacerts`, then reboot. On API 29+ the first remount may need `adb disable-verity` and a reboot. The MCP tool tells you when.
   - Android 14+: the CA store moved into the Conscrypt APEX. The tool injects the CA for the current boot, so run it again after each emulator restart.
6. Remove the proxy afterwards: `adb shell settings put global http_proxy :0`.

## iOS Simulator (macOS only, free with Xcode)
1. Install Xcode, open it once, then run `xcode-select --install`.
2. Boot a simulator with `xcrun simctl boot "iPhone 15"` and `open -a Simulator` (MCP: `boot_ios_simulator`).
3. Get a **simulator build** (`.app`) of the app from the dev team. App Store IPAs do not run on the simulator. Install it with `xcrun simctl install booted MyApp.app`.
4. The simulator uses the Mac's proxy. MCP `configure_device_proxy(device="ios")` sets the HTTP and HTTPS proxy for your network service (default `Wi-Fi`). Clear it afterwards.
5. CA: `xcrun simctl keychain booted add-root-cert ~/.mitmproxy/mitmproxy-ca-cert.pem` (MCP: `install_ca_cert(device="ios")`).

**No Mac?** Record iOS on a colleague's Mac and share the HAR. Alternatively, Appetize.io (browser-based, limited free plan) can capture network traffic and export a HAR that this tool accepts.

## Common problems
| Symptom | Cause | Fix |
|---|---|---|
| App says *no internet*, nothing recorded | CA not trusted | Install as a system CA (Android), or ask for a build whose `network_security_config` trusts user CAs |
| `detect_pinning` lists your API host | SSL pinning | Perf/debug build with pinning disabled |
| Only Google/Apple traffic | App uses QUIC/HTTP3 | Ask for a test build with QUIC disabled, or disable QUIC in the app's network layer |
| The two recordings have different request counts | The journeys diverged (e.g. an onboarding screen appeared once) | `launch_app(..., clear_data=true)` before both recordings |
