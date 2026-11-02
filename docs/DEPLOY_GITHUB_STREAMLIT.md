# Deploy: zip → GitHub → Streamlit Community Cloud

Your GitHub user: `sagar9804644867` (change it below if you use another account).

## 1. Unzip
Extract `mobperf-studio.zip` in your Downloads folder. You get a folder named `mobperf-studio`.

## 2. Test locally (optional, 2 minutes)
**Windows (PowerShell):**
```powershell
cd $HOME\Downloads\mobperf-studio
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-local.txt
python tests\test_e2e.py
python tests\test_jmx_checkpoint.py
streamlit run streamlit_app.py
```
**macOS / Linux:**
```bash
cd ~/Downloads/mobperf-studio
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-local.txt
python tests/test_e2e.py && python tests/test_jmx_checkpoint.py
streamlit run streamlit_app.py
```
The browser opens at http://localhost:8501. Switch on *Use the bundled sample* and click **Generate**.

## 3. Create the GitHub repository
1. Go to https://github.com/new
2. Repository name: `mobperf-studio`. Set it to **Public** (the Streamlit free tier deploys public repos; private repos need GitHub access granted to Streamlit).
3. Leave *Add README*, *.gitignore* and *license* **unticked** (the project already has them), then click **Create repository**.

## 4. Push the code
From inside the `mobperf-studio` folder (same commands in PowerShell, Git Bash or a terminal):
```bash
git init
git add .
git commit -m "MobPerf Studio v1.0 - mobile perf script generator with MCP recorder"
git branch -M main
git remote add origin https://github.com/sagar9804644867/mobperf-studio.git
git push -u origin main
```
If Git asks for a password, use a **Personal Access Token** (GitHub → Settings → Developer settings → Personal access tokens → *Tokens (classic)* → scope `repo`), or sign in through the Git Credential Manager pop-up.

Check the **Actions** tab. The *tests* workflow should go green, which reruns the three end-to-end tests on GitHub's machines.

## 5. Deploy on Streamlit Community Cloud
1. Go to https://share.streamlit.io and sign in with GitHub.
2. Click **Create app** → *Deploy a public app from GitHub*.
3. Fill in:
   - Repository: `sagar9804644867/mobperf-studio`
   - Branch: `main`
   - Main file path: `streamlit_app.py`
   - App URL (optional): e.g. `mobperf-studio` → `https://mobperf-studio.streamlit.app`
4. **Advanced settings** → Python version **3.12** → Save.
5. Click **Deploy**. The first build takes 2–4 minutes; it installs `requirements.txt` only (the recorder and MCP packages are for your own machine).

## 6. Updating later
```bash
git add .
git commit -m "describe the change"
git push
```
Streamlit Cloud redeploys automatically on every push to `main`.

## 7. Updating to v1.2 (Web / API section, body + correlation assertions, think time, pacing, data mode)
Files that are **new**: `engine/web_recorder.py`, `tests/test_workload.py`, `tests/groovy/timers_check.groovy`, `rules/assertions.yaml`, `tests/mock_web_app.py`, `tests/test_web_and_body.py`,
`tests/test_har_robustness.py`, `samples/shopweb_alice.har`, `samples/shopweb_bob.har`.
Files that **changed**: `streamlit_app.py`, `engine/jmx_builder.py`, `engine/validator.py`, `engine/pipeline.py`, `engine/rules.py`,
`engine/jmx_lint.py`, `engine/report.py`, `mcp_server/server.py`, `mcp_server/cli.py`, `README.md`, `requirements-local.txt`, `.github/workflows/tests.yml`,
`skills/mobile-perf-testing/SKILL.md`, `docs/DEPLOY_GITHUB_STREAMLIT.md`, `samples/MOB_MobiBank*`.
`requirements.txt` (what Streamlit Cloud installs) did **not** change.

**With Git** (unzip v1.2 over your existing folder, replacing files):
```bash
cd mobperf-studio
git add .
git commit -m "v1.2: Web/API section, body + correlation assertions, think time, pacing, data mode"
git push
```

**Without Git (GitHub website)**: open the repo → **Add file → Upload files** → drag the *contents* of the unzipped
`mobperf-studio` folder (all folders and files, not the folder itself) → **Commit changes**. GitHub replaces files with the
same path and adds the new ones. The `.github` and `.streamlit` folders are hidden on Windows: in File Explorer turn on
*View → Show → Hidden items* so they are dragged too.

Streamlit Cloud redeploys by itself within a minute or two. If it does not, open https://share.streamlit.io → your app → ⋮ → **Reboot app**.
Check: the sidebar now shows **📱 Mobile app** and **🌐 Web / API app**, the results have an **Assertions** tab and a *Body checks* metric, and Settings has a **Workload realism** block (think time, variation, pacing, test data).

## 8. Troubleshooting
| Problem | Fix |
|---|---|
| `ModuleNotFoundError` on Streamlit Cloud | Make sure `requirements.txt` is at the repo root and the main file path is exactly `streamlit_app.py` |
| App sleeps after inactivity | Normal on the free tier. Open the URL and click *Yes, get this app back up* |
| `git push` rejected (non-fast-forward) | The repo was created with a README. Run `git pull origin main --allow-unrelated-histories`, then push again |
| Upload too big | `.streamlit/config.toml` allows 200 MB. Trim the recording to the business journey |
