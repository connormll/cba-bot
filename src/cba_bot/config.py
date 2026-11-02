"""Configuration.

Precedence: CLI flag > environment variable > ``~/.config/cba-bot/config.json``
> default.  No credentials are baked into the source.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

APP_NAME = "cba-bot"

PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "opencode-go": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "model": "deepseek-v4.1-flash",
        "key_env": "OPENCODE_GO_API_KEY",
        "label": "OpenCode Go",
    },
    "mock": {
        "base_url": "",
        "model": "mock",
        "key_env": None,
        "label": "Mock (retrieval only)",
    },
}


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME


@dataclass
class Settings:
    provider: str = "opencode-go"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    data_dir: str | None = None
    contracts_dir: str | None = None
    embed_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    top_k: int = 6
    temperature: float = 0.2
    max_tokens: int = 6000
    history_turns: int = 6
    request_timeout: float = 180.0
    # Retrieval defaults exposed in the TUI as /fresh, /sector, /state.
    current_only: bool = False
    sector: str = "all"
    jurisdiction: str = "all"

    def resolved_model(self) -> str:
        return self.model or PROVIDER_DEFAULTS[self.provider]["model"]

    def resolved_base_url(self) -> str:
        url = self.base_url or PROVIDER_DEFAULTS[self.provider]["base_url"]
        return (url or "").rstrip("/")

    def data_path(self) -> Path:
        if self.data_dir:
            return Path(self.data_dir).expanduser()
        return default_data_dir()

    def contracts_path(self) -> Path:
        if self.contracts_dir:
            return Path(self.contracts_dir).expanduser().resolve()
        return (Path.cwd() / "contracts").resolve()

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


_ENV = {
    "provider": "CBA_PROVIDER",
    "model": "CBA_MODEL",
    "base_url": "CBA_BASE_URL",
    "api_key": "CBA_API_KEY",
    "data_dir": "CBA_DATA_DIR",
    "contracts_dir": "CBA_CONTRACTS_DIR",
    "embed_model": "CBA_EMBED_MODEL",
    "top_k": "CBA_TOP_K",
    "current_only": "CBA_CURRENT_ONLY",
    "sector": "CBA_SECTOR",
    "jurisdiction": "CBA_JURISDICTION",
}

_FIELDS = {f.name for f in fields(Settings)}


def _read_file() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        backup = path.with_suffix(".corrupt")
        try:
            path.replace(backup)
            print(
                f"cba-bot: {path} was unreadable; moved it to {backup}. "
                "Re-enter the API key with `cba-bot config --set-key`.",
                file=sys.stderr,
            )
        except OSError:
            pass
        return {}
    return {k: v for k, v in raw.items() if k in _FIELDS} if isinstance(raw, dict) else {}


def load_settings(**overrides: Any) -> Settings:
    values = _read_file()

    for name, env_name in _ENV.items():
        raw = os.environ.get(env_name)
        if raw in (None, ""):
            continue
        if name in {"top_k"}:
            values[name] = int(raw)
        elif name == "current_only":
            values[name] = raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            values[name] = raw

    for env_name in ("CBA_API_KEY", "OPENCODE_GO_API_KEY", "OPENCODE_API_KEY"):
        if os.environ.get(env_name):
            values["api_key"] = os.environ[env_name]
            break

    for key, value in overrides.items():
        if key in _FIELDS and value is not None:
            values[key] = value

    settings = Settings(**values)
    if settings.provider not in PROVIDER_DEFAULTS:
        raise ValueError(
            f"unknown provider {settings.provider!r}; "
            f"choose {', '.join(PROVIDER_DEFAULTS)}"
        )
    return settings


def save_settings(settings: Settings) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in settings.to_jsonable().items() if v is not None}
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def update_saved_settings(**updates: Any) -> Settings:
    values = _read_file()
    for key, value in updates.items():
        if key in _FIELDS and value is not None:
            values[key] = value
    merged = Settings(**values)
    save_settings(merged)
    return merged


def resolve_api_key(settings: Settings, provided: str | None = None) -> str | None:
    if provided:
        return provided
    if settings.api_key:
        return settings.api_key
    info = PROVIDER_DEFAULTS[settings.provider]
    for name in (info.get("key_env"), "CBA_API_KEY", "OPENCODE_GO_API_KEY"):
        if name and os.environ.get(name):
            return os.environ[name]

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive:
        raise RuntimeError(
            "No OpenCode Go API key. Set OPENCODE_GO_API_KEY or run "
            "`cba-bot config --set-key`."
        )
    import getpass

    key = getpass.getpass("OpenCode Go API key: ").strip()
    if not key:
        raise RuntimeError("No API key entered.")
    save = input("Save it to ~/.config/cba-bot/config.json? [Y/n] ").strip().lower()
    if save not in {"n", "no"}:
        update_saved_settings(api_key=key)
    return key


def mask_key(key: str | None) -> str:
    if not key:
        return "(not set)"
    return "*" * len(key) if len(key) <= 8 else f"{key[:4]}…{key[-4:]}"
