"""Dashboard server: stdlib HTTP + SSE, no web framework, no CDN.

Binds to loopback by default. Every mutating endpoint requires the session
token when one is configured, and requests are checked for Host-header
injection (DNS-rebinding protection) before anything is served.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import queue
import secrets
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from ..core.agent import RunResult
from ..runtime import Runtime
from ..voice import asr as asr_mod
from ..voice import tts as tts_mod

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 8 * 1024 * 1024


class EventBus:
    """Fan-out of agent events to every connected SSE client."""

    def __init__(self) -> None:
        self._clients: List[queue.Queue] = []
        self._lock = threading.Lock()
        self.history: List[Dict[str, Any]] = []

    def subscribe(self) -> queue.Queue:
        channel: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._clients.append(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue) -> None:
        with self._lock:
            if channel in self._clients:
                self._clients.remove(channel)

    def publish(self, event: Dict[str, Any]) -> None:
        event.setdefault("ts", time.time())
        self.history.append(event)
        del self.history[:-200]
        with self._lock:
            clients = list(self._clients)
        for channel in clients:
            try:
                channel.put_nowait(event)
            except queue.Full:
                pass

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class Dashboard:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.bus = EventBus()
        self.runs: Dict[str, RunResult] = {}
        self.token = str(runtime.config.get("server.session_token", "") or "")
        self.host = str(runtime.config.get("server.host", "127.0.0.1"))
        self.port = int(runtime.config.get("server.port", 8765))
        runtime.on_event(self.bus.publish)
        runtime.agent.confirmer = self._confirm
        self._httpd: Optional[ThreadingHTTPServer] = None

    # -- approvals ---------------------------------------------------------
    def _confirm(self, decision: Any, detail: Dict[str, Any]) -> Dict[str, Any]:
        """Headless confirmer: the dashboard is the approval surface.

        Blocks up to `wait` seconds for /api/approve; times out as a denial.
        """
        pending = {
            "decision": decision.to_dict(), "detail": detail,
            "id": uuid.uuid4().hex[:8], "ts": time.time(),
        }
        self.bus.publish({"type": "approval_required", "pending": pending})
        self.runtime.audit.record("agent.awaiting_approval", risk=decision.risk,
                                  detail={"tool": decision.action, "reason": decision.reason})
        deadline = time.time() + 180
        while time.time() < deadline:
            if getattr(self.runtime.agent, "_approved", None):
                approval = self.runtime.agent._approved
                self.runtime.agent._approved = None
                return {"approve": bool(approval)}
            time.sleep(0.25)
        return {"approve": False, "reason": "no response within 180s"}

    def resolve_approval(self, approve: bool) -> None:
        self.runtime.agent._approved = bool(approve)  # type: ignore[attr-defined]
        self.bus.publish({"type": "approval_resolved", "approved": bool(approve)})

    # -- routes ------------------------------------------------------------
    def handle(self, method: str, path: str, query: Dict[str, List[str]],
               body: bytes, headers: Dict[str, str]) -> Tuple[int, Any, Dict[str, str]]:
        route = path.rstrip("/") or "/"

        if method == "GET" and route == "/":
            return self._static("index.html")
        if method == "GET" and route.startswith("/static/"):
            return self._static(route[len("/static/"):])
        if method == "GET" and route == "/api/status":
            status = self.runtime.status()
            status["dashboard_clients"] = self.bus.client_count
            status["session_token_required"] = bool(self.token)
            return 200, status, {}
        if method == "GET" and route == "/api/tools":
            return 200, {"tools": self.runtime.registry.schemas()}, {}
        if method == "GET" and route == "/api/tasks":
            limit = int(query.get("limit", ["50"])[0])
            return 200, {"tasks": self.runtime.memory.list_tasks(limit)}, {}
        if method == "GET" and route == "/api/memory/facts":
            return 200, {"facts": self.runtime.memory.recall_facts()}, {}
        if method == "POST" and route == "/api/memory/fact":
            data = _json(body)
            if not data.get("key") or not data.get("value"):
                return 400, {"error": "key and value are required"}, {}
            self.runtime.memory.remember(str(data["key"]), str(data["value"]))
            self.runtime.audit.record("memory.remember", detail={"key": data["key"]})
            return 200, {"ok": True}, {}
        if method == "GET" and route == "/api/audit":
            limit = int(query.get("limit", ["100"])[0])
            return 200, {"entries": self.runtime.audit.entries(limit)}, {}
        if method == "GET" and route == "/api/audit/verify":
            return 200, self.runtime.audit.verify(), {}
        if method == "GET" and route == "/api/policy":
            return 200, self.runtime.policy.snapshot(), {}
        if method == "POST" and route == "/api/policy":
            data = _json(body)
            overrides = data.get("overrides")
            if isinstance(overrides, dict):
                self.runtime.policy.overrides = {**self.runtime.policy.overrides, **overrides}
                self.runtime.audit.record("policy.update",
                                          detail={"overrides": self.runtime.policy.overrides})
                return 200, {"ok": True, "policy": self.runtime.policy.snapshot()}, {}
            return 400, {"error": "body must contain an overrides object"}, {}
        if method == "GET" and route == "/api/vault":
            from ..security.vault import get_keyring_backend, mask
            if not self.runtime.vault or not self.runtime.vault.unlocked:
                return 200, {"unlocked": False, "hint": "POST /api/vault/unlock"}, {}
            return 200, {"unlocked": True, "keys": self.runtime.vault.keys(),
                         "values": {k: mask(self.runtime.vault.get(k, "") or "")
                                    for k in self.runtime.vault.keys()},
                         "keyring": get_keyring_backend()}, {}
        if method == "POST" and route == "/api/vault/unlock":
            from ..security.vault import Vault
            data = _json(body)
            password = str(data.get("password", ""))
            if not password:
                return 400, {"error": "password required"}, {}
            vault = self.runtime.vault or Vault(
                str(self.runtime.config.path_for("security.vault_file", "~/.ultron/vault.json")))
            if not vault.unlock(password, create=bool(data.get("create", False))):
                self.runtime.audit.record("vault.unlock", status="failed", risk=0.6,
                                          detail={"reason": "wrong password"})
                return 401, {"error": "wrong password"}, {}
            self.runtime.vault = vault
            self.runtime.audit.record("vault.unlock", detail={"keys": len(vault.keys())})
            return 200, {"ok": True, "keys": vault.keys()}, {}
        if method == "POST" and route == "/api/vault/secret":
            from ..security.vault import Vault
            if not self.runtime.vault or not self.runtime.vault.unlocked:
                return 401, {"error": "vault locked"}, {}
            data = _json(body)
            self.runtime.vault.set(str(data.get("key", "")), str(data.get("value", "")))
            self.runtime.audit.record("vault.set", risk=0.4, capability="secrets",
                                      detail={"key": data.get("key")})
            return 200, {"ok": True, "keys": self.runtime.vault.keys()}, {}
        if method == "POST" and route == "/api/plan":
            data = _json(body)
            goal = str(data.get("goal", "")).strip()
            if not goal:
                return 400, {"error": "goal required"}, {}
            return 200, self.runtime.agent.plan(goal), {}
        if method == "POST" and route == "/api/chat":
            data = _json(body)
            message = str(data.get("message", "")).strip()
            if not message:
                return 400, {"error": "message required"}, {}
            session = str(data.get("session", "default"))
            result = self.runtime.agent.run(
                message, session=session,
                task_id=data.get("task_id"), context=str(data.get("context", "")),
                allow_tools=bool(data.get("use_tools", True)),
            )
            self.runs[result.task_id] = result
            return 200, result.to_dict(), {}
        if method == "POST" and route == "/api/approve":
            data = _json(body)
            self.resolve_approval(bool(data.get("approve", False)))
            return 200, {"ok": True}, {}
        if method == "POST" and route == "/api/auto-approve":
            data = _json(body)
            enabled = bool(data.get("enabled", False))
            self.runtime.agent.autonomous = enabled
            self.runtime.audit.record("agent.autonomous", risk=0.6,
                                      detail={"enabled": enabled})
            return 200, {"ok": True, "autonomous": enabled}, {}
        if method == "POST" and route == "/api/voice/stt":
            fields, files = parse_multipart(body, headers.get("content-type", ""))
            audio = files.get("audio")
            if not audio:
                return 400, {"error": "multipart field 'audio' is required"}, {}
            filename, data = audio
            transcript = asr_mod.transcribe_bytes(self.runtime.config, data, filename)
            self.runtime.audit.record("voice.stt", detail={"backend": transcript.backend,
                                                          "chars": len(transcript.text)})
            return 200, transcript.to_dict(), {}
        if method == "POST" and route == "/api/voice/tts":
            data = _json(body)
            speech_text = tts_mod.strip_for_speech(str(data.get("text", "")))
            speech = tts_mod.TTS(self.runtime.config).speak(speech_text)
            payload = speech.to_dict()
            if speech.ok and speech.audio_path:
                audio_bytes = Path(speech.audio_path).read_bytes()
                return 200, {"ok": True, "backend": speech.backend,
                             "audio": audio_bytes.hex()}, {"Content-Type": "audio/wav"}
            return 200, payload, {}
        if method == "GET" and route == "/api/voice/backends":
            return 200, {"stt": asr_mod.STT(self.runtime.config).available(),
                         "tts": tts_mod.TTS(self.runtime.config).available()}, {}
        return 404, {"error": f"no route for {method} {path}"}, {}

    # -- static ------------------------------------------------------------
    def _static(self, relative: str) -> Tuple[int, Any, Dict[str, str]]:
        target = (STATIC_DIR / relative).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return 404, {"error": "not found"}, {}
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        return 200, target.read_bytes(), {"Content-Type": content_type}

    # -- lifecycle ---------------------------------------------------------
    def serve_forever(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        print(f"Ultron dashboard: http://{self.host}:{self.port}")
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nshutting down")
        finally:
            self._httpd.server_close()

    def shutdown(self) -> None:
        if self._httpd:
            self._httpd.shutdown()


def _make_handler(dashboard: Dashboard):
    runtime = dashboard.runtime

    class Handler(BaseHTTPRequestHandler):
        server_version = "Ultron"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter console
            return

        # -- helpers -------------------------------------------------------
        def _headers(self) -> Dict[str, str]:
            return {key.lower(): value for key, value in self.headers.items()}

        def _guard(self) -> bool:
            """DNS-rebinding + token check."""
            headers = self._headers()
            if self.command == "OPTIONS":
                self._send(204, b"", {})
                return False
            host = (headers.get("host") or "").split(":")[0]
            if host and host not in ("localhost", "127.0.0.1", "::1", "[::1]") and not host.endswith(".local"):
                self._send(403, json.dumps({"error": "host not allowed"}).encode(),
                           {"Content-Type": "application/json"})
                return False
            origin = headers.get("origin")
            allowed = runtime.config.get("server.cors_origins", []) or []
            if origin and allowed and origin not in allowed:
                self._send(403, json.dumps({"error": "origin not allowed"}).encode(),
                           {"Content-Type": "application/json"})
                return False
            if dashboard.token and self.command in ("POST", "PUT", "DELETE", "PATCH"):
                supplied = (headers.get("x-ultron-token") or "").strip()
                if not hmac.compare_digest(supplied, dashboard.token):
                    self._send(401, json.dumps({"error": "session token required"}).encode(),
                               {"Content-Type": "application/json"})
                    return False
            return True

        def _send(self, status: int, payload: Any, headers: Dict[str, str]) -> None:
            if isinstance(payload, (dict, list)):
                body = json.dumps(payload, default=str).encode()
                headers.setdefault("Content-Type", "application/json")
            elif isinstance(payload, bytes):
                body = payload
            else:
                body = str(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return b""
            if length <= 0:
                return b""
            if length > MAX_BODY:
                return self.rfile.read(MAX_BODY)
            return self.rfile.read(length)

        # -- verbs ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/stream":
                self._sse()
                return
            status, payload, headers = dashboard.handle(
                "GET", parsed.path, parse_qs(parsed.query), b"", self._headers())
            self._send(status, payload, headers)

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            parsed = urlparse(self.path)
            body = self._read_body()
            status, payload, headers = dashboard.handle(
                "POST", parsed.path, parse_qs(parsed.query), body, self._headers())
            self._send(status, payload, headers)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._guard()

        # -- server-sent events -------------------------------------------
        def _sse(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            channel = dashboard.bus.subscribe()
            try:
                for event in dashboard.bus.history[-20:]:
                    self._sse_write(event)
                while True:
                    try:
                        event = channel.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._sse_write(event)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                dashboard.bus.unsubscribe(channel)

        def _sse_write(self, event: Dict[str, Any]) -> None:
            data = json.dumps(event, default=str)
            self.wfile.write(f"event: {event.get('type', 'message')}\ndata: {data}\n\n".encode())
            self.wfile.flush()

    return Handler


def _json(body: bytes) -> Dict[str, Any]:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {"value": data}
    except json.JSONDecodeError:
        return {}


def parse_multipart(body: bytes, content_type: str) -> Tuple[Dict[str, str], Dict[str, Tuple[str, bytes]]]:
    """Minimal multipart/form-data parser (fields + one file field)."""
    if "boundary=" not in content_type:
        return {}, {}
    boundary = content_type.split("boundary=")[-1].strip().strip('"')
    marker = b"--" + boundary.encode()
    fields: Dict[str, str] = {}
    files: Dict[str, Tuple[str, bytes]] = {}
    for part in body.split(marker):
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if b"\r\n\r\n" in part:
            raw_headers, payload = part.split(b"\r\n\r\n", 1)
        else:
            continue
        payload = payload.rstrip(b"\r\n")
        name, filename = "", ""
        for line in raw_headers.decode("utf-8", "replace").splitlines():
            if line.lower().startswith("content-disposition"):
                for piece in line.split(";"):
                    piece = piece.strip()
                    if piece.startswith("name="):
                        name = piece[5:].strip('"')
                    elif piece.startswith("filename="):
                        filename = piece[9:].strip('"')
        if name:
            if filename:
                files[name] = (filename, payload)
            else:
                fields[name] = payload.decode("utf-8", "replace")
    return fields, files
