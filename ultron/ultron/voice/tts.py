"""Offline text-to-speech adapters: piper, espeak-ng, macOS say, pyttsx3."""

from __future__ import annotations

import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Speech:
    ok: bool
    backend: str
    audio_path: str = ""
    error: str = ""
    spoken_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "backend": self.backend, "audio_path": self.audio_path,
                "error": self.error, "chars": len(self.spoken_text)}


class TTS:
    def __init__(self, config: Any) -> None:
        tts = config.section("voice").get("tts", {}) or {}
        self.backend = str(tts.get("backend", "auto"))
        self.voice = str(tts.get("voice", "en-in-rahul-medium"))
        self.binary = str(tts.get("binary", "piper"))
        self.rate = int(tts.get("rate", 175))
        self.output_dir = Path(tempfile.gettempdir()) / "ultron-voice"

    def available(self) -> List[str]:
        found: List[str] = []
        if shutil.which("piper"):
            found.append("piper")
        if shutil.which("espeak-ng") or shutil.which("espeak"):
            found.append("espeak")
        if platform.system() == "Darwin" and shutil.which("say"):
            found.append("say")
        try:
            import pyttsx3  # type: ignore  # noqa: F401
            found.append("pyttsx3")
        except ImportError:
            pass
        return found

    def speak(self, text: str) -> Speech:
        text = (text or "").strip()
        if not text:
            return Speech(False, "none", error="nothing to speak")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        backends = [self.backend] if self.backend != "auto" else (self.available() or ["none"])
        errors: List[str] = []
        for backend in backends:
            try:
                if backend == "piper":
                    return self._piper(text)
                if backend == "espeak":
                    return self._espeak(text)
                if backend == "say":
                    return self._say(text)
                if backend == "pyttsx3":
                    return self._pyttsx3(text)
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
        return Speech(False, "none", error="; ".join(errors) or
                      "no offline TTS backend found (install piper or espeak-ng)")

    # -- backends ----------------------------------------------------------
    def _piper(self, text: str) -> Speech:
        model_dir = Path.home() / ".local/share/piper-voices"
        model = None
        if model_dir.exists():
            candidates = sorted(model_dir.rglob("*.onnx"))
            model = candidates[0] if candidates else None
        if model is None:
            raise RuntimeError("no piper voice model found in ~/.local/share/piper-voices")
        out = self.output_dir / "reply.wav"
        proc = subprocess.run(
            ["piper", "--model", str(model), "--output_file", str(out)],
            input=text.encode(), capture_output=True, timeout=180,
        )
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:200])
        return Speech(True, "piper", str(out), spoken_text=text)

    def _espeak(self, text: str) -> Speech:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        out = self.output_dir / "reply.wav"
        proc = subprocess.run([binary, "-s", str(self.rate), "-w", str(out), text],
                              capture_output=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.decode("utf-8", "replace")[:200])
        return Speech(True, "espeak", str(out), spoken_text=text)

    def _say(self, text: str) -> Speech:
        out = self.output_dir / "reply.aiff"
        subprocess.run(["say", "-o", str(out), text], capture_output=True, timeout=120, check=True)
        return Speech(True, "say", str(out), spoken_text=text)

    def _pyttsx3(self, text: str) -> Speech:
        import pyttsx3  # type: ignore

        engine = pyttsx3.init()
        engine.setProperty("rate", self.rate)
        engine.say(text)
        engine.runAndWait()
        return Speech(True, "pyttsx3", spoken_text=text)


def strip_for_speech(text: str, max_chars: int = 900) -> str:
    """Make answers pleasant to listen to: drop code, tables, collapse blanks."""
    lines: List[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("|", "```", "  - ", "- ", "* ")) and stripped.count("|") > 1:
            continue
        if stripped.startswith("```"):
            continue
        cleaned = stripped.lstrip("#*").strip()
        if cleaned:
            lines.append(cleaned)
    out = ". ".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(".", 1)[0] + "."
    return out
