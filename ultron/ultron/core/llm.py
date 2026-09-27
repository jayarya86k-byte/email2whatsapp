"""Local model client.

Talks to Ollama (`/api/chat`), llama.cpp server and any OpenAI-compatible
server that runs on this machine. `offline_only` (default true) blocks any
host that is not loopback, so prompts can never leave the device.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


class OfflineViolation(RuntimeError):
    pass


class LLMUnavailable(RuntimeError):
    pass


def is_local_endpoint(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    if not host:
        return False
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_endpoint(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").strip().lower()
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return host.endswith(".local")


@dataclass
class Message:
    role: str
    content: str

    def to_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class LLMResponse:
    text: str
    model: str
    duration: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "model": self.model,
            "duration": round(self.duration, 3),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
        }


class LocalLLM:
    def __init__(self, config: Any) -> None:
        llm = config.section("llm")
        self.provider = str(llm.get("provider", "ollama")).lower()
        self.base_url = str(llm.get("base_url", "http://127.0.0.1:11434")).rstrip("/")
        self.model = str(llm.get("model", ""))
        self.planner_model = str(llm.get("planner_model", "")) or self.model
        self.temperature = float(llm.get("temperature", 0.1))
        self.top_p = float(llm.get("top_p", 0.9))
        self.max_tokens = int(llm.get("max_tokens", 2048))
        self.timeout = float(llm.get("timeout_seconds", 300))
        self.num_ctx = int(llm.get("num_ctx", 8192))
        self.keep_alive = str(llm.get("keep_alive", "30m"))
        self.offline_only = bool(llm.get("offline_only", True))
        self.allowed_hosts = [str(h).lower() for h in llm.get("allowed_hosts", [])]
        if self.offline_only:
            self._assert_local(self.base_url)

    # -- guards ------------------------------------------------------------
    def _assert_local(self, url: str) -> None:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if host in self.allowed_hosts or is_local_endpoint(url):
            return
        if is_private_endpoint(url) and "0.0.0.0" in self.allowed_hosts:
            return
        raise OfflineViolation(
            f"refusing to contact '{url}': offline_only is enabled. "
            "Point llm.base_url at a local server (Ollama, llama.cpp, LM Studio)."
        )

    def _post(self, path: str, payload: Dict[str, Any], stream: bool = False) -> Any:
        self._assert_local(self.base_url)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base_url + path, data=body, headers={"Content-Type": "application/json"}
        )
        started = time.time()
        try:
            handle = urllib.request.urlopen(request, timeout=self.timeout)
        except (urllib.error.URLError, socket.timeout, ConnectionError) as exc:
            raise LLMUnavailable(
                f"local model server unreachable at {self.base_url} ({exc}). "
                "Start Ollama (`ollama serve`) or change llm.base_url."
            ) from exc
        if stream:
            return handle, started
        with handle:
            data = handle.read().decode("utf-8", "replace")
        return json.loads(data), started

    # -- public API --------------------------------------------------------
    def health(self) -> Dict[str, Any]:
        try:
            data, _ = self._post("/api/tags", {})
        except (LLMUnavailable, OfflineViolation) as exc:
            return {"ok": False, "provider": self.provider, "url": self.base_url, "error": str(exc)}
        models: List[str] = []
        for entry in data.get("models", []) if isinstance(data, dict) else []:
            name = entry.get("name") or entry.get("id")
            if name:
                models.append(name)
        return {
            "ok": True,
            "provider": self.provider,
            "url": self.base_url,
            "model": self.model,
            "models": models,
            "offline": is_local_endpoint(self.base_url),
        }

    def chat(
        self,
        messages: List[Message],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        stop: Optional[List[str]] = None,
    ) -> LLMResponse:
        target = model or self.model
        if self.provider == "openai":
            return self._chat_openai(messages, target, temperature, max_tokens, stop)
        payload = {
            "model": target,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "top_p": self.top_p,
                "num_ctx": self.num_ctx,
                "num_predict": self.max_tokens if max_tokens is None else max_tokens,
            },
        }
        if stop:
            payload["options"]["stop"] = stop
        data, started = self._post("/api/chat", payload)
        message = (data.get("message") or {}).get("content", "") if isinstance(data, dict) else ""
        return LLMResponse(
            text=message.strip(),
            model=target,
            duration=time.time() - started,
            prompt_tokens=int(data.get("prompt_eval_count", 0) or 0),
            completion_tokens=int(data.get("eval_count", 0) or 0),
            raw=data if isinstance(data, dict) else {},
        )

    def chat_stream(
        self, messages: List[Message], *, model: Optional[str] = None,
        temperature: Optional[float] = None, max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        if self.provider == "openai":
            yield self.chat(messages, model=model, temperature=temperature,
                            max_tokens=max_tokens).text
            return
        payload = {
            "model": model or self.model,
            "messages": [m.to_dict() for m in messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "temperature": self.temperature if temperature is None else temperature,
                "top_p": self.top_p,
                "num_ctx": self.num_ctx,
                "num_predict": self.max_tokens if max_tokens is None else max_tokens,
            },
        }
        handle, _ = self._post("/api/chat", payload, stream=True)
        with handle:
            for line in handle:
                raw = line.decode("utf-8", "replace").strip()
                if not raw or not raw.startswith("{"):
                    continue
                try:
                    chunk = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                piece = (chunk.get("message") or {}).get("content", "")
                if piece:
                    yield piece
                if chunk.get("done"):
                    break

    def _chat_openai(
        self, messages: List[Message], model: str,
        temperature: Optional[float], max_tokens: Optional[int],
        stop: Optional[List[str]],
    ) -> LLMResponse:
        payload = {
            "model": model or self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if stop:
            payload["stop"] = stop
        data, started = self._post("/v1/chat/completions", payload)
        choices = data.get("choices", [{}])
        text = choices[0].get("message", {}).get("content", "") if choices else ""
        usage = data.get("usage", {}) if isinstance(data, dict) else {}
        return LLMResponse(
            text=(text or "").strip(), model=model, duration=time.time() - started,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0), raw=data,
        )

    def embed(self, texts: List[str]) -> List[List[float]]:
        """Optional embeddings for memory search; degrades to hashed vectors."""
        if self.provider == "ollama" and texts:
            vectors: List[List[float]] = []
            for text in texts:
                data, _ = self._post("/api/embeddings", {"model": self.model, "prompt": text})
                vectors.append(list((data or {}).get("embedding", [])))
            return vectors
        return [hashed_vector(text) for text in texts]


def hashed_vector(text: str, dims: int = 256) -> List[float]:
    """Deterministic bag-of-tokens vector; used when no embedding model exists."""
    import hashlib as _hashlib

    vector = [0.0] * dims
    for token in (text or "").lower().split():
        digest = _hashlib.sha256(token.encode()).digest()
        vector[digest[0] % dims] += 1.0
    norm = sum(value * value for value in vector) ** 0.5 or 1.0
    return [value / norm for value in vector]


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return 0.0
    size = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(size))
    na = sum(x * x for x in a[:size]) ** 0.5
    nb = sum(x * x for x in b[:size]) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def is_offline() -> bool:
    """Best-effort connectivity probe used for the dashboard badge."""
    try:
        socket.setdefaulttimeout(2)
        with socket.create_connection(("1.1.1.1", 53)):
            return False
    except OSError:
        return True
