# Testing the backend and the Windows app

You need a Linux machine (or VM) with Docker or nerdctl for the server, and a Windows 10/11 PC for the app.
The cheapest way to try real models is OpenRouter (the examples below use three models that cost a few cents per
million tokens).

## A. Server (Linux, Docker or nerdctl)

1. **Get the code** (either way):
   ```bash
   git clone -b claude/loving-cray-anj6xj https://github.com/magiccpp/multi-chat.git && cd multi-chat
   # or: unzip multichat.zip && cd multichat
   ```
2. **Create the secrets and the config**:
   ```bash
   make init            # creates .env (random MULTICHAT_API_TOKEN + DB password) and config.yaml
   ```
   No `make`? `cp .env.example .env && cp config.example.yaml config.yaml`, then set `MULTICHAT_API_TOKEN` and
   `POSTGRES_PASSWORD` in `.env` (e.g. `openssl rand -hex 32` / `openssl rand -hex 16`).
3. **Add your OpenRouter key** to `.env`:
   ```
   OPENROUTER_API_KEY=sk-or-v1-…
   ```
4. **Pick the models** in `config.yaml`: replace the `openrouter` endpoint's `models: []` with
   ```yaml
       models:
         - {name: openai/gpt-oss-20b,             display_name: GPT-OSS 20B,       color: "#10a37f", input_price: 0.018, output_price: 0.09}
         - {name: qwen/qwen3.7-flash,             display_name: Qwen 3.7 Flash,    color: "#7c3aed", input_price: 0.03,  output_price: 0.13}
         - {name: deepseek/deepseek-v4-flash-0731, display_name: DeepSeek V4 Flash, color: "#2563eb", input_price: 0.0444, output_price: 0.1333}
   ```
   (Prices are USD per 1M tokens; check openrouter.ai/models. Endpoints without a key are harmless — or delete them.)
5. **Start it**:
   ```bash
   make up                              # Docker
   make up COMPOSE="nerdctl compose"    # nerdctl
   ```
   Without make: `docker compose up -d --build` / `nerdctl compose up -d --build`.
6. **Check it**:
   ```bash
   curl http://localhost:8000/api/health                 # {"ok":true,...}
   docker compose logs server | tail                     # "config synced ... 'models': N"
   grep MULTICHAT_API_TOKEN .env                         # the token the apps need
   ```
7. **Optional: try it from the server's terminal** (Python 3.11+):
   ```bash
   python3 -m venv ~/mc && ~/mc/bin/pip install ./clients/core ./clients/tui
   export MULTICHAT_URL=http://localhost:8000 MULTICHAT_TOKEN=<token from .env>
   ~/mc/bin/multichat run "How should we cache a slow upstream API? 3 bullets." --models "gpt-oss,qwen,deepseek" --mode shared
   ~/mc/bin/multichat vote <message id> up && ~/mc/bin/multichat stats
   ~/mc/bin/multichat          # the terminal UI
   ```
8. **Let the Windows PC reach it**: open TCP 8000 in the server's firewall (e.g. `sudo ufw allow 8000/tcp`), and note
   the server's IP (`hostname -I`). For use over the internet, put HTTPS in front (see the README) — the token is the
   only thing protecting the server.

Stop with `make down`; data stays in the `pgdata` / `appdata` volumes.

## B. Windows app

1. Install **Python 3.12 or 3.13** from python.org (tick *Add python.exe to PATH*) and **Git for Windows**.
2. Get the code (git clone as above, or unzip) and open **PowerShell** in the folder.
3. **Build the .exe** (takes a few minutes the first time):
   ```powershell
   powershell -ExecutionPolicy Bypass -File clients\desktop\build_exe.ps1
   .\clients\desktop\dist\MultiChat\MultiChat.exe
   ```
   Or run from source without building:
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\pip install .\clients\core .\clients\desktop
   .\.venv\Scripts\multichat-desktop
   ```
   Windows SmartScreen may warn about an unsigned app: *More info → Run anyway*.
4. **Connect**: server URL `http://<server-ip>:8000`, token = `MULTICHAT_API_TOKEN` from the server's `.env`
   (stored in Windows Credential Manager).
5. **Plan with several models**: click **Models (0)** → tick the three OpenRouter models → Save. Type a question,
   press Enter. Switch **Isolated / Shared** in the toolbar and ask again — in Shared mode later models comment on
   earlier answers. Use the tabs to see one model's thread; ▲/▼ to vote. The status bar shows tokens and $ per model.
6. **Let it work on this PC**: left bar **Runner** → tick *Let MultiChat run tasks on this PC* → *Add folder…* (e.g.
   `C:\Users\you\mc-test`, put a small project or a single `.py` file there) → Save. The status bar shows
   *runner online*.
7. **Attach the folder**: toolbar folder button → *This PC* → Browse → choose a folder inside the allowed one → Open.
   The file tree appears on the left.
8. **Build**: on the best reply click **★ Pick as builder** (switches to BUILD). Set *Edits: ask* and *Commands: ask*,
   then ask e.g. *"Add a function `greet(name)` to hello.py with a test, then run `python hello.py`"*. Edits open as
   diffs with **Accept / Reject**; commands show an **Approve / Reject** card in the chat. Afterwards the change opens
   as a diff with **Revert this turn**.
9. **Statistics**: left bar **Stats** — cost, tokens, votes, win rate, latency per model.
10. Closing the window keeps MultiChat in the tray (so the runner stays available); right-click the tray icon → Quit.

## If something goes wrong

| Symptom | Check |
| --- | --- |
| "Cannot reach …" when connecting | server IP/port, firewall, `curl http://<ip>:8000/api/health` from the PC |
| "The server rejected the token" | copy `MULTICHAT_API_TOKEN` exactly from `.env` |
| Replies say `[error from openrouter: …]` | `OPENROUTER_API_KEY` in `.env`, then `docker compose up -d` to reload; the server must reach openrouter.ai |
| Model list empty | `docker compose logs server` for config errors; `config.yaml` indentation |
| runner offline / error | Runner settings has a folder; same URL/token; the server log shows `runner … connected` |
| "outside the folders this runner is allowed to use" | pick a folder inside one added in Runner settings |
| Install command fails "needs administrator" | expected — the runner never elevates; run that install yourself |

Logs: server `docker compose logs -f server`; the Windows app prints to the console when started from source.

## C. iPhone app (needs a Mac with Xcode 16+)

1. On the server, enable HTTPS: point a DNS name at it, open ports 80/443, add `MULTICHAT_DOMAIN=your.domain` to `.env`,
   then `docker compose -f compose.yaml -f compose.https.yaml up -d`. Check `https://your.domain/api/health`.
   (At home you can skip this and use `http://<server LAN IP>:8000`.)
2. On the Mac: `brew install xcodegen && cd ios && xcodegen && open MultiChat.xcodeproj`.
3. Target **MultiChat → Signing & Capabilities**: choose your Team (Apple ID) and a unique bundle id.
4. iPhone: enable **Developer Mode** (Settings → Privacy & Security), connect it, select it in Xcode, press **Run**.
5. In the app: server URL + token → Sessions → ✎ new session → pick the OpenRouter models → ask something; vote with
   👍/👎; long-press-free "…" menu → **Pick as builder**; approvals from a desktop/TUI runner appear as cards
   (edits open a diff). Stats tab shows cost and win rate per model.
