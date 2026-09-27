"""Wiring: builds the object graph (config -> llm -> tools -> agent)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config, load_config
from .core.agent import Agent
from .core.llm import LocalLLM
from .core.memory import Memory
from .security.audit import AuditLog
from .security.policy import PolicyEngine
from .tools import doc_tools, finance_tools, fs_tools, shell_tools
from .tools.registry import ToolRegistry

__all__ = ["Runtime", "build_runtime", "build_registry"]


def build_registry(config: Config, policy: PolicyEngine, audit: AuditLog) -> ToolRegistry:
    registry = ToolRegistry()
    fs_tools.register(registry, policy, audit)
    shell_tools.register(registry, policy, audit, config)
    doc_tools.register(registry, policy, audit)
    finance_tools.register(registry, policy, audit, config)
    registry.disabled(list(config.get("tools.disabled", []) or []))
    return registry


class Runtime:
    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or load_config()
        self.audit = AuditLog(
            self.config.path_for("security.audit_log", "~/.ultron/audit/audit.jsonl"),
            redact=bool(self.config.get("security.redact_secrets_in_output", True)),
        )
        self.policy = PolicyEngine(self.config)
        self.registry = build_registry(self.config, self.policy, self.audit)
        self.llm = LocalLLM(self.config)
        self.memory = Memory(self.config.path_for("memory.db_path", "~/.ultron/memory.db"))
        self.agent = Agent(self.config, self.llm, self.registry, self.policy,
                           self.audit, self.memory)
        self.vault: Any = None
        self._emit_hook: Optional[Callable[[Dict[str, Any]], None]] = None
        # Interactive terminals approve in the terminal; the dashboard
        # replaces this with its own approval surface.
        self.agent.confirmer = self.confirm_sync

    # -- events ------------------------------------------------------------
    def on_event(self, hook: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        self._emit_hook = hook
        self.agent.emit = hook or (lambda event: None)

    def broadcast(self, event: Dict[str, Any]) -> None:
        if self._emit_hook:
            self._emit_hook(event)

    # -- approvals ---------------------------------------------------------
    def confirm_sync(self, decision: Any, detail: Dict[str, Any]) -> Dict[str, Any]:
        """CLI confirmation hook: read from stdin when interactive."""
        if not sys_isatty():
            return {"approve": False, "reason": "non-interactive session"}
        print(f"\n[approval] {decision.action} ({decision.band}, risk {decision.risk:.2f})")
        print(f"          {decision.reason}")
        print(f"          {detail}")
        reply = input("Approve? [y/N/a(ll)] ").strip().lower()
        if reply in ("a", "all"):
            return {"approve": True, "session": True}
        return {"approve": reply in ("y", "yes")}

    # -- status ------------------------------------------------------------
    def status(self) -> Dict[str, Any]:
        health = self.llm.health()
        return {
            "ok": True,
            "agent": self.config.get("agent.name", "Ultron"),
            "model": health,
            "workspace": str(self.config.workspace),
            "sandbox": self.policy.snapshot(),
            "tools": [{"name": t.name, "risk": t.risk, "capability": t.capability,
                       "category": t.category} for t in self.registry.all()],
            "memory": self.memory.stats(),
            "audit": {"path": str(self.audit.path), **self.audit.verify()},
            "autonomous": self.agent.autonomous,
        }


def sys_isatty() -> bool:
    import sys

    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def build_runtime(config_path: Optional[str] = None) -> Runtime:
    return Runtime(load_config(config_path))
