# MultiChat

A self-hosted code agent that talks to several LLMs at once. You **plan** with multiple models, compare and vote on their answers, then switch to **build** with the model that understood the problem best. The builder edits files, runs commands, downloads and installs things on your own PC or an SSH host, with your approval.

- **Several models in one session**: any OpenAI-compatible endpoint (OpenAI, OpenRouter, DeepSeek, Gemini, xAI, Groq, Mistral, GitHub Models, Ollama…), Anthropic, plus **Claude Code**, **Cursor** and **GitHub Copilot** as agent participants. Models can join or leave mid-session.
- **Shared or isolated**: models either see each other's replies (and build on or challenge them) or answer independently.
- **PLAN → BUILD**: ★ pick a builder. It gets the plan discussion, with your votes marked, and works through tools.
- **Votes and statistics**: ▲/▼ on any reply from the desktop app, the TUI or the plain CLI. A statistics view shows cost, tokens, latency, time-to-first-token, tokens/s, approval rate, win rate (how often a model was picked as builder) and cost per upvote.
- **Only usable models**: at start-up and every 30 minutes the server checks every endpoint's key and fetches the vendor's model list (OpenRouter: hundreds of models, with prices). Models that can't be used right now are greyed out in every client, with the reason, and can't be added to a session.
- **Documents and images**: attach PDFs, Word files, text/code files and images to a message (desktop: 📎, drag-and-drop or **Ctrl+V** for screenshots; TUI: `/attach`; CLI: `--attach`; iPhone: Photos, Files or paste). Document text goes to every model; images go to models that can see them.
- **Cost per session**: the chat shows a *Session cost* bar with each model's cost and tokens and the session total (click a model to open its tab); **Details…** opens a per-model table with replies, input/output tokens, prices per 1M, input/output cost, total and share. Every reply shows its own tokens and $, model tabs show each model's cost, and the session list shows what each session cost. TUI: `/cost`; CLI: `multichat sessions`; iPhone: per reply and per session.
- **Prices without configuration**: OpenRouter reports prices itself; for vendors whose APIs don't (OpenAI, Anthropic, DeepSeek, Google, xAI, Mistral, GitHub Models) the server takes OpenRouter's public list price for the same model and re-prices earlier usage that was recorded as $0. Models with no known price show "price unknown" instead of $0; set a price in the Endpoints dialog or `config.yaml` to override.
- **Token meter**: per-model tokens and $ for the current session, live. Values estimated when a vendor doesn't report usage are marked with `~`.
- **Where work happens**: on **your PC** through a runner built into the clients (PowerShell on Windows, bash on Linux), on **SSH hosts**, or **no folder** at all (chat only; attach one later). Approvals per session, per-turn diffs and **Revert**.
- **Clients**: a **Windows desktop app** (PySide6, IDE layout), a **Linux terminal UI** (Textual, opencode-style) and an **iPhone app** (SwiftUI, iOS 17+). All connect to one **containerized server** (FastAPI + PostgreSQL). The server holds all configuration and keys, and stores every session and chat, so each device sees the same history.

```
 Windows desktop app ─┐                       ┌─ OpenAI / Anthropic / OpenRouter / DeepSeek / …
   (built-in runner)  │   HTTPS + SSE / WSS   │
 Linux TUI / CLI ─────┼──▶  MultiChat server ──┼─ web_fetch / web_search
   (built-in runner)  │   (FastAPI, Postgres) │
 headless runners ────┘     config.yaml       └─ SSH hosts (asyncssh)
                                │
   runner ◀── file / command / agent-CLI requests (your PC: PowerShell or bash; Claude Code, Cursor, Copilot)
```

## 1. Run the server (Docker or nerdctl)

```bash
git clone https://github.com/magiccpp/multi-chat.git && cd multi-chat
make init                       # creates .env (random token + DB password) and config.yaml
$EDITOR .env config.yaml        # add vendor keys; choose endpoints and models
make up                         # docker compose up -d --build
make up COMPOSE="nerdctl compose"   # …or with nerdctl
```

Without `make`, run `cp .env.example .env`, `cp config.example.yaml config.yaml`, then `docker compose up -d` (or `nerdctl compose up -d`).
The server listens on `SERVER_PORT` (default 8000). The token clients need is `MULTICHAT_API_TOKEN` in `.env`. If clients connect from other machines, put a TLS reverse proxy (Caddy, nginx, Traefik) in front, because runners connect over `wss://`.

Alternatively, `install.sh --server` (below) clones the repo, generates the secrets and starts the stack for you.

### Configuration lives on the server

`config.yaml` (see [`config.example.yaml`](config.example.yaml)) declares:

| Section | What |
| --- | --- |
| `endpoints` | `kind: openai_compatible \| anthropic \| agent_cli \| mock`, `base_url`, `api_key: ${ENV_VAR}`, `auto_models: true` (or glob patterns like `["openai/*"]`) to import the vendor's chat models automatically, and `models` with `display_name`, `color`, `input_price` / `output_price` (USD per 1M tokens) |
| `ssh_connections` | hosts the agents may work on (`auth: key \| password \| agent`, `shell: bash \| powershell`) |
| `web_search` | `provider: brave \| tavily \| searxng` and key, used by the `web_search` tool (`web_fetch` needs no key) |
| `web.allow_private` | let `web_fetch` reach LAN addresses (off by default; SSRF guard) |
| `defaults` | approval defaults for new sessions, tool-loop step cap, command timeout, `availability_interval_minutes` (key/model-list re-check, default 30) |

Keys come from `.env` via `${NAME}`. They're stored encrypted in Postgres (`MULTICHAT_SECRET_KEY`, or a key generated into the `appdata` volume) and never sent to clients; the API only ever returns masked keys. Rows from `config.yaml` are read-only in the clients. To apply edits without a restart, call `POST /api/admin/reload-config` or restart. The desktop app's **Endpoints** dialog can also add endpoints and models (with "Import from vendor", which pulls prices from OpenRouter).

**OpenRouter** is built in: set `OPENROUTER_API_KEY` in `.env` and the server offers every OpenRouter chat model your key can use, with prices, whether or not `config.yaml` mentions OpenRouter. OpenRouter endpoints, and any endpoint without models listed, import the vendor's model list automatically; `auto_models: false` or a pattern list narrows that.

**Availability.** Each endpoint has a status: `ok`, `no_key`, `error` (e.g. "OpenRouter rejected the API key") or `unchecked`. The server checks at start-up, every `availability_interval_minutes`, after config reloads and endpoint edits, and on demand (`POST /api/endpoints/check`, the **Check all keys now** button in the desktop Endpoints dialog, `/endpoints check` in the TUI, pull-to-refresh in the iPhone model picker). A model is unavailable when its endpoint has no key or a rejected one, when the vendor no longer offers it, or, for agent CLIs, when no connected runner has the CLI installed. Unavailable models stay in sessions they were already in, but can't be added.

**Attachments.** Up to 20 MB per file. The server extracts text from PDF, Word (.docx) and text/code files and inlines it into the message for every model. Images are downscaled to at most 1568 px and sent as images to models that support vision; other models get a note that an image was omitted. Files are stored in Postgres with the session.

### Claude Code, Cursor and GitHub Copilot

These join a session like any model, but they run their own agent loop on the machine that holds the folder: your PC (runner) or an SSH host. The CLI must be installed there.

| | Command used | Credentials | PLAN | BUILD |
| --- | --- | --- | --- | --- |
| Claude Code | `claude -p --output-format stream-json` (`--resume` keeps its context) | `ANTHROPIC_API_KEY` from the endpoint (`--bare` mode) | `--permission-mode plan` | approvals go through a bridge into MultiChat's Approve/Reject (runner only) |
| Cursor | `cursor-agent -p --output-format stream-json` | `CURSOR_API_KEY` from your Cursor dashboard | proposes only | `--force` (auto-applies; use Revert) |
| GitHub Copilot | `copilot -p … -s` | `GH_TOKEN`, or `copilot login` on that machine | write and shell tools denied | `--allow-all-tools` (auto-applies; use Revert) |

Anthropic does not allow third-party apps to offer claude.ai (subscription) login, so MultiChat has no "Sign in with Claude". Use an API key for the Claude Code endpoint (or leave it empty and the CLI uses whatever sign-in that machine already has). For GitHub's API models, use the **GitHub Models** preset (`https://models.github.ai/inference`, a token with `models:read`).

Cursor's and Copilot's output formats are parsed tolerantly, and Copilot token counts are estimated (`~`).

## 2. Linux: terminal UI and CLI

```bash
curl -fsSL https://raw.githubusercontent.com/magiccpp/multi-chat/main/install.sh | bash
```

This installs into `~/.local/share/multichat/venv`, links `~/.local/bin/multichat` and asks for the server URL and token (saved to `~/.config/multichat/client.toml`, mode 600). Options: `--ref <branch|tag>`, `--upgrade`, `--uninstall`, `--no-config`, `--runner-service` (systemd user service for a headless runner) and `--server [dir]` (also deploy the server with docker/nerdctl compose). For non-interactive installs, set `MULTICHAT_URL` and `MULTICHAT_TOKEN`.

```
multichat                         open the TUI (latest session); its runner shares the current folder
multichat --root ~/src --root ~/work     folders the runner may use
multichat run "Why is login slow?" --models gpt-5,claude --mode shared [--json] [--attach log.txt --attach shot.png]
multichat vote 1234 up            # or: multichat vote '#12' down --session 7
multichat history | stats | sessions | models
multichat runner --root ~/src     # headless runner only
multichat login                   # change server/token
```

**TUI keys**: `enter` sends (`alt+enter` / `ctrl+j` for a new line) · `tab` / `shift+tab` switch between All and per-model tabs · `alt+↑` focuses replies, then `↑`/`↓` move · `+` `-` `0` vote · `b` pick as builder · `enter` expand tool output · `y` copy · `1..9` quick-vote after a PLAN turn · `ctrl+b` sidebar · `ctrl+p` command palette · `ctrl+a` pending approval.

**Slash commands**: `/new` (ignored while the current session is still empty), `/attach <file>…|clear`, `/sessions`, `/models`, `/plan`, `/build [model]`, `/builder <model>`, `/mode shared|isolated`, `/to <models|all>`, `/folder none|here|<path>|ssh:<conn>:<path>|runner:<name>:<path>`, `/approve edits|cmds auto|ask`, `/vote <#n|up|down> [up|down|clear]`, `/quickvote on|off`, `/revert`, `/stop`, `/stats`, `/endpoints [check]`, `/rename`, `/help`. In `/models`, type to filter; unavailable models are greyed out.

## 3. Windows: desktop app

**Download [MultiChat-Setup.exe](https://github.com/magiccpp/multi-chat/releases/latest/download/MultiChat-Setup.exe)** (from the [latest release](https://github.com/magiccpp/multi-chat/releases/latest)) and run it. Nothing else is needed: no Python, no PowerShell, no administrator rights. It installs for your user into `%LOCALAPPDATA%\Programs\MultiChat`, adds a Start menu entry (desktop icon and start-at-sign-in are optional), closes a running MultiChat when you upgrade, and can be removed from *Settings → Apps*. The installer isn't code-signed, so SmartScreen may say "Windows protected your PC": click *More info → Run anyway*. A portable `MultiChat-portable.zip` is on the same release.

The installer is built by GitHub Actions (`.github/workflows/windows.yml`) on every push that changes the Windows app: it runs the runner tests on Windows, builds the app with PyInstaller, smoke-tests the exe, builds the installer with Inno Setup, installs and uninstalls it silently, then publishes it on the `desktop-latest` release.

Developers can build locally with `clients\desktop\build_exe.ps1` (Python 3.12; it stops a running MultiChat first, and also builds the installer if Inno Setup 6 is installed), or run from source: `pip install clients\core clients\desktop`, then `multichat-desktop`.

On first start, enter the server URL and token; the token is kept in Windows Credential Manager. Then:

- **This PC is used automatically**: the runner is on by default (allowed folder: your user folder), and a session without a folder uses this PC as soon as the runner is online. So in PLAN you can just ask "check my Windows version" or "ssh to my server and check the containers". Choosing *No folder* in the folder dialog keeps that session PC-free; *Runner* settings has the switch to turn the automatic attach off.
- **Runner** (left bar): *Let MultiChat run tasks on this PC* and the folders models may use. Commands run in PowerShell with your normal rights. Installs that need admin rights fail rather than elevate. You can also ask to confirm every command locally. Closing the window keeps the runner in the tray.
- **Models (N)**: tick models from any endpoint to join the session. Search filters the list; unavailable models are greyed out with the reason in the tooltip. **Check availability** asks the server to re-check keys now.
- **＋ New session** starts a session with the current (available) models and opens the model picker. It does nothing while the current session is still empty.
- **Attachments**: 📎 **Attach** picks files; you can also drop files on the message box or paste a screenshot with **Ctrl+V**. Pending files show as chips (✕ removes). Sent images appear as thumbnails, documents as chips; click to open.
- **Folder button**: choose *No folder*, *This PC*, *Another runner* or an *SSH host*. For a new SSH host, you confirm its key fingerprint once, then browse to a folder.
- **PLAN | BUILD**, **Builder**, **Shared/Isolated**, **Edits/Commands approval** sit in the toolbar. The chat panel has a tab per model, ▲/▼ and **★ Pick as builder** on every reply, and recipient chips. Edits awaiting approval open as a diff with **Accept / Reject**; applied changes open as diffs with **Revert this turn**.
- **Endpoints** (left bar): each endpoint's status (● green ok, orange no key, red error), **Check now** / **Check all keys now**, and *Import the vendor's chat models automatically*.
- **Stats** (left bar): KPI tiles, a sortable per-model table, and cost, win-rate and cost-per-day charts.
- The status bar shows per-model tokens and $ for the session, plus the runner state.

## 4. iPhone app

Native SwiftUI (iOS 17+, iPhone 13 and newer): sessions and history, chat with model tabs, votes, pick builder, approvals with diffs, statistics, attachments (📎 Photos, Files, paste image), and a searchable model picker with unavailable models greyed out. Build it with Xcode on a Mac: see [`ios/README.md`](ios/README.md). The phone needs the server over HTTPS. The optional `compose.https.yaml` puts Caddy with automatic Let's Encrypt certificates in front:

```bash
echo "MULTICHAT_DOMAIN=multichat.example.com" >> .env
docker compose -f compose.yaml -f compose.https.yaml up -d
```

## How a session works

1. **PLAN**: your message goes to the ticked models. *Isolated* runs them in parallel, each seeing only its own thread. *Shared* also runs them in parallel; each sees the other models' replies from earlier turns as `[Model said]: …`. *Relay* runs them one after another, so each also sees the replies given before it in the same turn (slower, more of a discussion). With a folder attached, models get read-only tools (`list_dir`, `read_file`, `grep`), `web_fetch` / `web_search`, and **read-only commands**: they can inspect code, configuration, installed software, services and logs, on this PC or on other machines (`ssh host "cat /etc/app.conf"`, `docker exec`, `kubectl exec`). Every command is checked before it runs. Anything that writes files, installs or uninstalls, changes settings, restarts services or runs scripts is refused, and the model is told to put that step in its plan for BUILD. The checker understands PowerShell, cmd and bash, including pipelines, `$(...)`, script blocks and the command inside `ssh`/`bash -c`/`docker exec`. Unknown commands are refused rather than guessed. `defaults.plan_commands` in `config.yaml`: `auto` (default; read-only commands run without asking), `ask`, or `off`; `defaults.plan_command_timeout` (120 s) limits each one. The commands of one reply run side by side, and the tool tells the model which shell it gets (Windows PowerShell 5.1, PowerShell 7 or bash) with the quoting rules for `ssh` there. Installing [PowerShell 7](https://aka.ms/powershell) on Windows is recommended: the runner uses it automatically and it passes quotes to `ssh` correctly.
2. **Vote** ▲/▼ on replies. Votes feed the statistics and the builder's hand-off.
3. **★ Pick as builder**: the session switches to BUILD. The builder's system prompt contains the plan discussion, with upvoted ideas marked, and the target OS and shell. It gets `write_file`, `edit_file` and `run_command`, and is told to use non-interactive flags (`winget … --silent --accept-package-agreements`, `apt-get -y`).
4. **Approvals**: with `ask`, each edit or command waits for Approve/Reject in any connected client. A rejection, with your note, goes back to the model.
5. **Revert**: every write is journaled per turn. For agent CLIs, the folder is snapshotted with git trees or copies. **Revert** restores the turn.

## Security notes

- Every API route and the runner WebSocket require `Authorization: Bearer $MULTICHAT_API_TOKEN`. Treat the token like a password: it lets its holder run commands on connected runners within their allowed folders.
- Runners only touch the folders you allowed (symlink escapes are blocked) and run with your user's rights. Command approval defaults to `ask`.
- SSH host keys are pinned on first use after you confirm the fingerprint. A changed key refuses to connect.
- `web_fetch` blocks private and loopback addresses unless `web.allow_private` is set.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e "server[test]" -e clients/core -e clients/tui -e clients/desktop pytest-asyncio
# Postgres for tests: docker compose -f compose.test.yaml run --rm tests   (or a local one:)
export DATABASE_URL=postgresql+asyncpg://multichat:multichat@localhost:5432/multichat_test
cd server && pytest            # API, providers, shared/isolated context, runner, SSH, agent CLIs, TUI pilot
cd ../clients/core && pytest   # runner units (snapshots, confinement, PowerShell argv, MCP bridge)
cd ../desktop && QT_QPA_PLATFORM=offscreen pytest -p no:asyncio   # desktop app end-to-end
```

The `mock` endpoint kind gives deterministic offline models. A user line `@tool <name> {json}` makes a mock model call that tool, which is handy for trying approvals and diffs without API keys.

```
server/            FastAPI app, Alembic migrations, Dockerfile
  app/providers/   OpenAI-compatible, Anthropic, mock, agent_cli (claude_code, cursor, copilot)
  app/agent/       context (shared/isolated), tools, approvals, turn loop, event bus
  app/workspaces/  runner / SSH / none, git snapshots
  app/runners/     runner hub (WebSocket RPC)
clients/core/      multichat_client: API client, session state, runner, approval MCP bridge
clients/tui/       multichat_tui: Textual TUI + `multichat` CLI
clients/desktop/   multichat_desktop: PySide6 app, PyInstaller spec, build_exe.ps1
ios/               SwiftUI iPhone app + MultiChatKit Swift package (XcodeGen project.yml)
install.sh  compose.yaml  compose.https.yaml  compose.test.yaml  Caddyfile  config.example.yaml  .env.example  Makefile
```
