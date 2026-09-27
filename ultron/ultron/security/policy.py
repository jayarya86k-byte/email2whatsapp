"""Permission / risk policy engine.

Tools declare a risk score and required capabilities. The policy engine maps
(risk, capability, path) to ALLOW / ASK / DENY before anything executes.
Users override the defaults per tool in ultron.config.json:

    "security": { "overrides": { "shell_exec": "ask", "file_delete": "deny" } }
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, Iterable, List, Optional

ALLOW = "allow"
ASK = "ask"
DENY = "deny"

# Capability -> human label used in confirmations and the dashboard.
CAPABILITIES = {
    "fs.read": "read your files",
    "fs.write": "create or modify files",
    "fs.delete": "delete files",
    "shell": "run shell commands",
    "code.exec": "execute generated code",
    "network": "access the network",
    "secrets": "read stored credentials",
    "system": "change system state",
}

RISK_BANDS = (
    (0.0, 0.2, "read-only"),
    (0.2, 0.5, "local-mutation"),
    (0.5, 0.8, "elevated"),
    (0.8, 1.01, "critical"),
)

_SECRET_PATTERNS = (
    re.compile(r"\b\d{2}[A-Z]{5}\d{4}[A-Z]\b"),                     # PAN
    re.compile(r"\b\d{4}\s?\d{4}\s?\d{4}\s?\d{4}\b"),               # card
    re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"),  # email
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token|passcode|credential)\b"
               r"\s*[:=]?\s*\S+"),
    re.compile(r"\b\d{12}\b"),                                       # Aadhaar-like
)


@dataclass
class Decision:
    action: str
    risk: float
    verdict: str
    reason: str
    band: str
    requires_confirmation: bool
    path: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.verdict in (ALLOW, ASK)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "risk": round(self.risk, 3),
            "verdict": self.verdict,
            "reason": self.reason,
            "band": self.band,
            "requires_confirmation": self.requires_confirmation,
            "path": self.path,
        }


def band_for(risk: float) -> str:
    for low, high, label in RISK_BANDS:
        if low <= risk < high:
            return label
    return "critical"


def scan_secrets(text: str) -> List[str]:
    found: List[str] = []
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(text or ""):
            found.append(match.group(0))
    return found


class PolicyEngine:
    def __init__(self, config: Any) -> None:
        sec = config.section("security")
        self.sandbox_root = Path(config.sandbox_root).resolve()
        self.ask_above = float(sec.get("require_confirmation_above_risk", 0.5))
        self.deny_at = float(sec.get("deny_risk_at_or_above", 0.95))
        self.forbidden: List[str] = list(sec.get("forbidden_path_patterns", []))
        self.overrides: Dict[str, str] = dict(sec.get("overrides", {}))
        self.network_allowlist: List[str] = list(sec.get("network_allowlist", []))
        self.max_output = int(sec.get("max_output_chars", 20000))
        self.approvals: List[Dict[str, Any]] = []  # runtime allow-list grants

    # -- path handling -----------------------------------------------------
    def _expand(self, raw: str) -> Path:
        return Path(os.path.expandvars(os.path.expanduser(str(raw)))).resolve()

    def matches_forbidden(self, raw_path: str) -> Optional[str]:
        target = self._expand(raw_path)
        text = str(target)
        for pattern in self.forbidden:
            expanded = os.path.expanduser(pattern)
            if expanded.startswith("~"):
                expanded = str(self._expand(expanded))
            if any(ch in pattern for ch in "*?["):
                if fnmatch.fnmatch(text, pattern) or PurePath(text).match(pattern):
                    return pattern
                if fnmatch.fnmatch(text, expanded) or PurePath(text).match(expanded):
                    return pattern
            elif text == expanded or text.startswith(expanded + os.sep):
                return pattern
        return None

    def in_sandbox(self, raw_path: str) -> bool:
        try:
            target = self._expand(raw_path)
        except OSError:
            return False
        return target == self.sandbox_root or self.sandbox_root in target.parents

    def relativize(self, raw_path: str) -> str:
        try:
            target = self._expand(raw_path)
        except OSError:
            return str(raw_path)
        try:
            return str(target.relative_to(self.sandbox_root))
        except ValueError:
            return str(target)

    # -- decisions ---------------------------------------------------------
    def check(
        self,
        action: str,
        *,
        risk: float = 0.0,
        capability: str = "system",
        path: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
        extra_risk: float = 0.0,
    ) -> Decision:
        risk = max(0.0, min(1.0, float(risk) + float(extra_risk)))
        band = band_for(risk)
        verdict = self._verdict_for(action, risk)
        reason = ""

        if path:
            forbidden = self.matches_forbidden(path)
            if forbidden:
                return Decision(action, risk, DENY,
                                f"path matches protected pattern '{forbidden}'", band, True, path)
            if capability.startswith("fs.") and not self.in_sandbox(path) and verdict != DENY:
                # Outside the workspace is tolerated but always asks first.
                verdict = ASK
                reason = f"path is outside sandbox {self.sandbox_root}"
            if capability == "fs.write":
                parent = Path(self._expand(path)).parent
                if not self.in_sandbox(str(parent)):
                    verdict = ASK
                    reason = "writing outside the sandbox requires confirmation"

        if verdict == ASK and not reason:
            reason = f"{CAPABILITIES.get(capability, capability)} is rated {band} (risk {risk:.2f})"
        if verdict == ALLOW and not reason:
            reason = "policy default"

        if capability == "network" and verdict == ASK and not self.network_allowlist:
            reason = "outbound network access is disabled by default"
        if capability == "network" and self.network_allowlist and detail:
            host = str(detail.get("host", ""))
            if not any(fnmatch.fnmatch(host, pat) for pat in self.network_allowlist):
                return Decision(action, risk, DENY,
                                f"host '{host}' is not in the network allowlist", band, True, path)

        return Decision(action, risk, verdict, reason, band,
                        verdict == ASK, path)

    def _verdict_for(self, action: str, risk: float) -> str:
        override = self.overrides.get(action)
        if override in (ALLOW, ASK, DENY):
            return override
        if risk >= self.deny_at:
            return DENY
        if risk >= self.ask_above:
            return ASK
        return ALLOW

    # -- runtime approvals -------------------------------------------------
    def grant(self, decision: Decision, approver: str = "user") -> None:
        self.approvals.append({"action": decision.action, "path": decision.path,
                               "approver": approver, "risk": decision.risk})

    def clear_approvals(self) -> None:
        self.approvals.clear()

    def snapshot(self) -> Dict[str, Any]:
        return {
            "sandbox_root": str(self.sandbox_root),
            "ask_above_risk": self.ask_above,
            "deny_at_risk": self.deny_at,
            "overrides": self.overrides,
            "forbidden_path_patterns": self.forbidden,
            "network_allowlist": self.network_allowlist,
            "grants": list(self.approvals),
        }
