"""Where to find the server: env vars, then ~/.config/multichat/client.toml."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "MultiChat"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "multichat"


CONFIG_PATH = config_dir() / "client.toml"


@dataclass
class ClientConfig:
    url: str = ""
    token: str = ""
    runner_name: str = ""
    runner_roots: list[str] = field(default_factory=list)
    confirm_commands_locally: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load(path: Path = CONFIG_PATH) -> ClientConfig:
    cfg = ClientConfig()
    if path.exists():
        data = tomllib.loads(path.read_text())
        server = data.get("server", {})
        runner = data.get("runner", {})
        cfg.url = server.get("url", "")
        cfg.token = server.get("token", "")
        cfg.runner_name = runner.get("name", "")
        cfg.runner_roots = list(runner.get("roots", []))
        cfg.confirm_commands_locally = bool(runner.get("confirm_commands_locally", False))
    cfg.url = os.environ.get("MULTICHAT_URL", cfg.url).rstrip("/")
    cfg.token = os.environ.get("MULTICHAT_TOKEN", cfg.token)
    return cfg


def save(cfg: ClientConfig, path: Path = CONFIG_PATH, *, include_token: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[server]", f"url = {_quote(cfg.url)}"]
    if include_token:
        lines.append(f"token = {_quote(cfg.token)}")
    lines += [
        "",
        "[runner]",
        f"name = {_quote(cfg.runner_name)}",
        "roots = [" + ", ".join(_quote(r) for r in cfg.runner_roots) + "]",
        f"confirm_commands_locally = {'true' if cfg.confirm_commands_locally else 'false'}",
        "",
    ]
    path.write_text("\n".join(lines))
    if sys.platform != "win32":
        path.chmod(0o600)
