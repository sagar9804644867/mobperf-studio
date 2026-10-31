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

## 7. Troubleshooting
| Problem | Fix |
|---|---|
| `ModuleNotFoundError` on Streamlit Cloud | Make sure `requirements.txt` is at the repo root and the main file path is exactly `streamlit_app.py` |
| App sleeps after inactivity | Normal on the free tier. Open the URL and click *Yes, get this app back up* |
| `git push` rejected (non-fast-forward) | The repo was created with a README. Run `git pull origin main --allow-unrelated-histories`, then push again |
| Upload too big | `.streamlit/config.toml` allows 200 MB. Trim the recording to the business journey |
