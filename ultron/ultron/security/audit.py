"""Tamper-evident audit log.

Every consequential action is appended to a JSONL file where each record
carries the SHA-256 of the previous record. Removing or editing a line
breaks the chain and `verify()` reports the first bad offset.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

GENESIS = "0" * 64

# Exact key names plus distinctive substrings (kept tight so ordinary keys
# such as "company" or "expand" are not caught by the "pan" rule).
SECRET_KEYS = {
    "password", "passwd", "secret", "token", "api_key", "apikey", "passphrase",
    "pan", "pan_number", "pan_no", "aadhaar", "aadhaar_number", "credential",
    "credentials", "auth", "authorization", "private_key", "otp", "pin", "cvv",
}
SECRET_SUBSTRINGS = ("password", "passwd", "secret", "token", "api_key", "apikey",
                     "passphrase", "private_key", "credential")


def _is_secret_key(key: Any) -> bool:
    lowered = str(key).strip().lower()
    if lowered in SECRET_KEYS:
        return True
    return any(tag in lowered for tag in SECRET_SUBSTRINGS)


def _scrub_text(value: str) -> str:
    """Redact secret-looking values and cap runaway strings."""
    from .policy import scan_secrets

    for secret in scan_secrets(value):
        if len(secret) > 4:
            value = value.replace(secret, "***redacted***")
    if len(value) > 4000:
        value = value[:4000] + "...<truncated>"
    return value


def _canon(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, path: str | os.PathLike[str], redact: bool = True) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self.redact = redact
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------
    def _last_digest(self) -> str:
        last = GENESIS
        for record in self._iter_raw():
            digest = record.get("digest")
            if digest:
                last = digest
        return last

    def record(
        self,
        action: str,
        *,
        actor: str = "agent",
        status: str = "ok",
        risk: float = 0.0,
        detail: Optional[Dict[str, Any]] = None,
        task_id: Optional[str] = None,
        capability: str = "",
    ) -> Dict[str, Any]:
        with self._lock:
            prev = self._last_digest()
            body: Dict[str, Any] = {
                "id": uuid.uuid4().hex,
                "ts": time.time(),
                "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "actor": actor,
                "action": action,
                "capability": capability,
                "status": status,
                "risk": round(float(risk), 4),
                "task_id": task_id,
                "detail": self._scrub(detail or {}),
            }
            body["prev"] = prev
            body["digest"] = hashlib.sha256((prev + _canon(body)).encode()).hexdigest()
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(body, default=str) + "\n")
            return body

    def _scrub(self, value: Any) -> Any:
        if not self.redact:
            return value
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                out[key] = "***redacted***" if _is_secret_key(key) else self._scrub(item)
            return out
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if isinstance(value, str):
            return _scrub_text(value)
        return value

    # -- reading -----------------------------------------------------------
    def _iter_raw(self) -> Iterator[Dict[str, Any]]:
        if not self.path.exists():
            return iter(())
        def generator() -> Iterator[Dict[str, Any]]:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
        return generator()

    def entries(self, limit: int = 200) -> List[Dict[str, Any]]:
        return list(self._iter_raw())[-limit:]

    def verify(self) -> Dict[str, Any]:
        prev = GENESIS
        count = 0
        for index, record in enumerate(self._iter_raw()):
            expected_prev = record.get("prev")
            if expected_prev != prev:
                return {"ok": False, "offset": index, "reason": "broken prev link", "records": count}
            body = {k: v for k, v in record.items() if k != "digest"}
            digest = hashlib.sha256((prev + _canon(body)).encode()).hexdigest()
            if digest != record.get("digest"):
                return {"ok": False, "offset": index, "reason": "digest mismatch", "records": count}
            prev = record["digest"]
            count += 1
        return {"ok": True, "offset": -1, "reason": "", "records": count}
