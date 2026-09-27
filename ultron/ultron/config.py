"""Configuration loading for Ultron.

Config is JSON so the runtime stays dependency free. Resolution order:
    explicit path  ->  $ULTRON_CONFIG  ->  ./ultron.config.json
                     ->  ~/.ultron/ultron.config.json  ->  defaults
Every value can be overridden by an environment variable so secrets never
have to live in the config file.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_NAME = "ultron.config.json"

DEFAULTS: Dict[str, Any] = {
    "agent": {
        "name": "Ultron",
        "workspace": "~/ultron-workspace",
        "max_steps": 24,
        "step_timeout_seconds": 300,
        "history_limit": 200,
        "autonomous": False,
    },
    "llm": {
        # Only loopback endpoints are considered local/offline.
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5-coder:14b-instruct",
        "planner_model": "",
        "temperature": 0.1,
        "top_p": 0.9,
        "max_tokens": 2048,
        "timeout_seconds": 300,
        "num_ctx": 8192,
        "keep_alive": "30m",
        # Refuse any endpoint that is not loopback/private. This is the
        # single most important privacy switch in the product.
        "offline_only": True,
        "allowed_hosts": ["127.0.0.1", "localhost", "::1", "0.0.0.0"],
    },
    "security": {
        "audit_log": "~/.ultron/audit/audit.jsonl",
        "vault_file": "~/.ultron/vault.json",
        "require_confirmation_above_risk": 0.5,
        "deny_risk_at_or_above": 0.95,
        "sandbox_root": "~/ultron-workspace",
        "network_allowlist": [],
        "forbidden_path_patterns": [
            "~/.ssh",
            "~/.aws",
            "~/.gnupg",
            "~/.config/gcloud",
            "/etc/shadow",
            "/etc/sudoers",
            "**/id_rsa*",
            "**/*.pem",
            "**/.env",
        ],
        "redact_secrets_in_output": True,
        "max_output_chars": 20000,
    },
    "voice": {
        "enabled": True,
        "stt": {
            "backend": "auto",  # auto | faster-whisper | whisper-cli | vosk | none
            "model": "base.en",
            "language": "en",
            "binary": "whisper-cli",
            "model_path": "",
            "compute_type": "int8",
        },
        "tts": {
            "backend": "auto",  # auto | piper | espeak | say | none
            "voice": "en-in-rahul-medium",
            "binary": "piper",
            "rate": 175,
        },
        "wake_word": "",
        "push_to_talk_key": "ctrl+shift+space",
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "session_token": "",
        "cors_origins": [],
    },
    "finance": {
        "currency": "INR",
        "default_fy": "2025-26",
        "payer_pan": "",
        "challan_bank": "State Bank of India",
        "taxpayer_type": "individual",
    },
    "tools": {
        "disabled": [],
        "shell_timeout_seconds": 120,
        "allow_shell": True,
        "allow_network_tools": False,
    },
    "memory": {
        "db_path": "~/.ultron/memory.db",
        "autosave_transcripts": True,
        "retention_days": 180,
    },
}

# config path -> env var overrides applied after load
ENV_OVERRIDES = {
    "llm.model": "ULTRON_MODEL",
    "llm.base_url": "ULTRON_LLM_URL",
    "llm.provider": "ULTRON_LLM_PROVIDER",
    "llm.offline_only": "ULTRON_OFFLINE_ONLY",
    "server.host": "ULTRON_HOST",
    "server.port": "ULTRON_PORT",
    "server.session_token": "ULTRON_SESSION_TOKEN",
    "finance.payer_pan": "ULTRON_PAYER_PAN",
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    data: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))
    path: Optional[Path] = None

    # -- section accessors -------------------------------------------------
    def section(self, name: str) -> Dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # -- derived paths -----------------------------------------------------
    @property
    def workspace(self) -> Path:
        return _expand(self.get("agent.workspace", "~/ultron-workspace"))

    @property
    def sandbox_root(self) -> Path:
        return _expand(self.get("security.sandbox_root") or self.get("agent.workspace"))

    @property
    def data_dir(self) -> Path:
        return Path(os.path.expanduser("~/.ultron"))

    def path_for(self, dotted: str, default: str) -> Path:
        return _expand(self.get(dotted) or default)

    def ensure_dirs(self) -> None:
        for directory in (self.workspace, self.sandbox_root, self.data_dir):
            try:
                Path(directory).mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
        audit = self.path_for("security.audit_log", "~/.ultron/audit/audit.jsonl")
        audit.parent.mkdir(parents=True, exist_ok=True)

    def to_json(self) -> str:
        return json.dumps(self.data, indent=2, sort_keys=True)


def candidate_paths(explicit: Optional[str] = None) -> List[Path]:
    out: List[Path] = []
    if explicit:
        out.append(Path(explicit).expanduser())
    if os.environ.get("ULTRON_CONFIG"):
        out.append(Path(os.environ["ULTRON_CONFIG"]).expanduser())
    out.append(Path.cwd() / DEFAULT_CONFIG_NAME)
    out.append(Path(os.path.expanduser("~/.ultron")) / DEFAULT_CONFIG_NAME)
    return out


def load_config(explicit: Optional[str] = None) -> Config:
    merged = copy.deepcopy(DEFAULTS)
    used: Optional[Path] = None
    for candidate in candidate_paths(explicit):
        if candidate.is_file():
            try:
                merged = _deep_merge(merged, json.loads(candidate.read_text("utf-8")))
                used = candidate
                break
            except json.JSONDecodeError as exc:
                raise SystemExit(f"ultron: invalid JSON in {candidate}: {exc}") from exc
    for dotted, env_name in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        current = merged.get(dotted.split(".")[0], {})
        default = current.get(dotted.split(".")[1]) if isinstance(current, dict) else None
        if isinstance(default, bool):
            merged.setdefault(dotted.split(".")[0], {})[dotted.split(".")[1]] = _as_bool(raw)
        elif isinstance(default, int) and not isinstance(default, bool):
            merged.setdefault(dotted.split(".")[0], {})[dotted.split(".")[1]] = int(raw)
        else:
            merged.setdefault(dotted.split(".")[0], {})[dotted.split(".")[1]] = raw
    config = Config(data=merged, path=used)
    config.ensure_dirs()
    return config


def write_default_config(path: str) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(DEFAULTS, indent=2), "utf-8")
    return target
