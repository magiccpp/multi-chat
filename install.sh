#!/usr/bin/env bash
# MultiChat installer for Linux (terminal UI, CLI and runner; optionally the server).
#
#   curl -fsSL https://raw.githubusercontent.com/magiccpp/multi-chat/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --server          # also deploy the server with docker/nerdctl
#
# Options:
#   --ref <branch|tag>    install from this git ref (default: main, or $MULTICHAT_REF)
#   --upgrade             reinstall the latest code from the ref, keeping your config
#   --uninstall           remove the virtualenv, the `multichat` link and the runner service
#   --no-config           do not ask for the server URL / token
#   --runner-service      install a systemd user service running `multichat runner`
#   --server [dir]        clone the repo into dir (default ~/multichat-server), create .env and
#                         config.yaml, and start the stack with docker compose or nerdctl compose
# Environment:
#   MULTICHAT_URL, MULTICHAT_TOKEN   non-interactive server settings
#   MULTICHAT_REPO                   git URL to install from (default: the GitHub repo)
#   MULTICHAT_RUNNER_ROOTS           colon-separated folders for --runner-service
set -euo pipefail

REPO="${MULTICHAT_REPO:-https://github.com/magiccpp/multi-chat.git}"
REF="${MULTICHAT_REF:-main}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/multichat"
VENV="$DATA_HOME/venv"
BIN_DIR="$HOME/.local/bin"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}/multichat"
CONFIG_FILE="$CONFIG_HOME/client.toml"
SERVICE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/multichat-runner.service"

ACTION=install
ASK_CONFIG=1
RUNNER_SERVICE=0
SERVER_DIR=""

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }
die() { printf '\033[31mx %s\033[0m\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
    --upgrade) ACTION=upgrade; shift ;;
    --uninstall) ACTION=uninstall; shift ;;
    --no-config) ASK_CONFIG=0; shift ;;
    --runner-service) RUNNER_SERVICE=1; shift ;;
    --server)
      if [ $# -gt 1 ] && [ "${2#--}" = "$2" ]; then SERVER_DIR="$2"; shift 2; else SERVER_DIR="$HOME/multichat-server"; shift; fi ;;
    -h|--help) sed -n '2,24p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

# Read from the terminal even when the script itself arrives on stdin (curl | bash).
ask() { # ask VAR "prompt" [default] [secret]
  local __var="$1" __prompt="$2" __default="${3:-}" __secret="${4:-}" __reply=""
  if [ -r /dev/tty ]; then
    if [ -n "$__secret" ]; then
      read -r -s -p "$__prompt" __reply </dev/tty || true; echo >/dev/tty
    else
      read -r -p "$__prompt" __reply </dev/tty || true
    fi
  fi
  printf -v "$__var" '%s' "${__reply:-$__default}"
}

random_hex() { # random_hex BYTES
  if command -v openssl >/dev/null 2>&1; then openssl rand -hex "$1"; else python3 -c "import secrets; print(secrets.token_hex($1))"; fi
}

toml_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

uninstall() {
  bold "Uninstalling MultiChat client"
  if [ -f "$SERVICE_FILE" ]; then
    systemctl --user disable --now multichat-runner.service >/dev/null 2>&1 || true
    rm -f "$SERVICE_FILE"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    info "removed runner service"
  fi
  rm -f "$BIN_DIR/multichat"
  rm -rf "$VENV"
  info "removed $VENV and $BIN_DIR/multichat"
  info "kept your settings in $CONFIG_HOME (delete it to forget the server token)"
}

check_python() {
  PYTHON=""
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      PYTHON="$(command -v "$candidate")"; break
    fi
  done
  if [ -z "$PYTHON" ]; then
    warn "Python 3.11 or newer is required."
    hint_packages "python3 python3-venv"
    exit 1
  fi
  if ! "$PYTHON" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    warn "The Python venv module is missing."
    hint_packages "python3-venv"
    exit 1
  fi
  command -v git >/dev/null 2>&1 || { warn "git is required."; hint_packages git; exit 1; }
}

hint_packages() {
  if command -v apt-get >/dev/null 2>&1; then info "Install it with: sudo apt-get install -y $1"
  elif command -v dnf >/dev/null 2>&1; then info "Install it with: sudo dnf install -y ${1//python3-venv/python3}"
  elif command -v pacman >/dev/null 2>&1; then info "Install it with: sudo pacman -S ${1//python3-venv/python}"
  elif command -v zypper >/dev/null 2>&1; then info "Install it with: sudo zypper install -y ${1//python3-venv/python3}"
  else info "Install: $1"; fi
}

install_client() {
  check_python
  bold "Installing MultiChat client from $REPO@$REF"
  mkdir -p "$DATA_HOME" "$BIN_DIR"
  if [ ! -x "$VENV/bin/python" ]; then
    "$PYTHON" -m venv "$VENV"
  fi
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  local reinstall=()
  [ "$ACTION" = upgrade ] && reinstall=(--force-reinstall --no-deps)
  if [ ${#reinstall[@]} -gt 0 ]; then
    # refresh our two packages, then make sure their dependencies are present
    "$VENV/bin/python" -m pip install --quiet "${reinstall[@]}" \
      "multichat-client @ git+$REPO@$REF#subdirectory=clients/core" \
      "multichat-tui @ git+$REPO@$REF#subdirectory=clients/tui"
  fi
  "$VENV/bin/python" -m pip install --quiet \
    "multichat-client @ git+$REPO@$REF#subdirectory=clients/core" \
    "multichat-tui @ git+$REPO@$REF#subdirectory=clients/tui"
  ln -sf "$VENV/bin/multichat" "$BIN_DIR/multichat"
  info "installed $BIN_DIR/multichat"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this to your shell profile:"
       info "export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac
}

write_config() {
  local url="${MULTICHAT_URL:-}" token="${MULTICHAT_TOKEN:-}"
  if [ -f "$CONFIG_FILE" ] && [ -z "$url" ] && [ "$ACTION" = upgrade ]; then return; fi
  if [ -z "$url" ]; then ask url "MultiChat server URL [http://localhost:8000]: " "http://localhost:8000"; fi
  if [ -z "$token" ]; then ask token "API token (MULTICHAT_API_TOKEN from the server's .env): " "" secret; fi
  if [ -z "$token" ]; then
    warn "No token given; run 'multichat login' later."
    return
  fi
  mkdir -p "$CONFIG_HOME"
  umask 077
  cat >"$CONFIG_FILE" <<EOF
[server]
url = "$(toml_escape "${url%/}")"
token = "$(toml_escape "$token")"

[runner]
name = ""
roots = []
confirm_commands_locally = false
EOF
  chmod 600 "$CONFIG_FILE"
  info "saved $CONFIG_FILE"
  if "$VENV/bin/multichat" models >/dev/null 2>&1; then info "connected to $url"; else warn "could not reach $url yet (is the server running?)"; fi
}

install_runner_service() {
  command -v systemctl >/dev/null 2>&1 || { warn "systemd not found; start the runner with: multichat runner --root DIR"; return; }
  local roots="${MULTICHAT_RUNNER_ROOTS:-}"
  if [ -z "$roots" ]; then ask roots "Folders the runner may use (colon-separated) [$HOME]: " "$HOME"; fi
  local args="" IFS=":"
  for r in $roots; do args="$args --root \"$r\""; done
  unset IFS
  mkdir -p "$(dirname "$SERVICE_FILE")"
  cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=MultiChat runner (lets the MultiChat server work on this machine)
After=network-online.target

[Service]
ExecStart=$VENV/bin/multichat runner$args
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now multichat-runner.service
  info "runner service enabled (logs: journalctl --user -u multichat-runner -f)"
}

detect_compose() {
  if docker compose version >/dev/null 2>&1; then COMPOSE="docker compose"
  elif nerdctl compose version >/dev/null 2>&1; then COMPOSE="nerdctl compose"
  elif command -v docker-compose >/dev/null 2>&1; then COMPOSE="docker-compose"
  else die "neither 'docker compose' nor 'nerdctl compose' is available"; fi
}

install_server() {
  detect_compose
  bold "Deploying the MultiChat server in $SERVER_DIR with '$COMPOSE'"
  if [ -d "$SERVER_DIR/.git" ]; then
    git -C "$SERVER_DIR" fetch --quiet origin "$REF"
    git -C "$SERVER_DIR" checkout --quiet "$REF"
    git -C "$SERVER_DIR" pull --quiet --ff-only origin "$REF" || true
  else
    git clone --quiet --branch "$REF" "$REPO" "$SERVER_DIR"
  fi
  cd "$SERVER_DIR"
  local token
  if [ ! -f .env ]; then
    token="$(random_hex 32)"
    sed -e "s/^MULTICHAT_API_TOKEN=.*/MULTICHAT_API_TOKEN=$token/" \
        -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(random_hex 16)/" .env.example > .env
    chmod 600 .env
    info "created .env (add vendor API keys there)"
  fi
  [ -f config.yaml ] || { cp config.example.yaml config.yaml; info "created config.yaml (endpoints and models)"; }
  token="$(sed -n 's/^MULTICHAT_API_TOKEN=//p' .env)"
  local port; port="$(sed -n 's/^SERVER_PORT=//p' .env)"; port="${port:-8000}"
  $COMPOSE up -d --build
  for _ in $(seq 1 60); do
    if curl -fsS "http://localhost:$port/api/health" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  bold "Server is up"
  info "URL:   http://$(hostname -f 2>/dev/null || hostname):$port"
  info "Token: $token"
  info "Edit $SERVER_DIR/.env and config.yaml, then: cd $SERVER_DIR && $COMPOSE up -d"
  export MULTICHAT_URL="${MULTICHAT_URL:-http://localhost:$port}" MULTICHAT_TOKEN="${MULTICHAT_TOKEN:-$token}"
}

main() {
  if [ "$ACTION" = uninstall ]; then uninstall; exit 0; fi
  [ -n "$SERVER_DIR" ] && install_server
  install_client
  [ "$ASK_CONFIG" = 1 ] && write_config
  [ "$RUNNER_SERVICE" = 1 ] && install_runner_service
  bold "Done. Start with: multichat"
  info "multichat --help      all commands (run, vote, history, stats, runner, login)"
}

main
