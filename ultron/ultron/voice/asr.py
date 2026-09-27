"""Offline speech-to-text adapters.

Nothing here touches the network. Backends, tried in order for `auto`:
    faster-whisper  (pip install faster-whisper)
    whisper-cli     (whisper.cpp binary, `whisper-cli -m model -f audio.wav`)
    vosk            (pip install vosk, model dir in config)
    system          (macOS `whisper`/`dictation`, Windows speech bridge)

Every backend returns plain text; the agent then treats it exactly like a
typed prompt.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

AUDIO_SAMPLE_RATE = 16000


@dataclass
class Transcript:
    text: str
    backend: str
    duration: float = 0.0
    language: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error

    def to_dict(self) -> Dict[str, Any]:
        return {"text": self.text, "backend": self.backend, "duration": self.duration,
                "language": self.language, "error": self.error, "ok": self.ok}


def decode_audio(data: bytes, source_format: str = "wav") -> str:
    """Normalise browser uploads to 16 kHz mono WAV via ffmpeg when present."""
    with tempfile.TemporaryDirectory(prefix="ultron-voice-") as tmp:
        raw = Path(tmp) / f"in.{source_format}"
        raw.write_bytes(data)
        wav = Path(tmp) / "audio.wav"
        if source_format == "wav":
            try:
                with wave.open(str(raw), "rb") as handle:
                    if (handle.getframerate(), handle.getnchannels()) == (AUDIO_SAMPLE_RATE, 1):
                        return str(raw)
            except wave.Error:
                pass
        if not shutil.which("ffmpeg"):
            return str(raw)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(raw), "-ar", str(AUDIO_SAMPLE_RATE),
             "-ac", "1", "-f", "wav", str(wav)],
            capture_output=True, check=False,
        )
        return str(wav) if wav.exists() else str(raw)


def audio_duration(path: str) -> float:
    try:
        with wave.open(path, "rb") as handle:
            return round(handle.getnframes() / float(handle.getframerate() or 1), 2)
    except (wave.Error, ZeroDivisionError):
        return 0.0


class STT:
    def __init__(self, config: Any) -> None:
        voice = config.section("voice")
        stt = voice.get("stt", {}) if isinstance(voice.get("stt"), dict) else {}
        self.backend = str(stt.get("backend", "auto"))
        self.model = str(stt.get("model", "base.en"))
        self.language = str(stt.get("language", "en"))
        self.binary = str(stt.get("binary", "whisper-cli"))
        self.model_path = str(stt.get("model_path", ""))
        self.compute_type = str(stt.get("compute_type", "int8"))
        self._model: Any = None
        self._lock = threading.Lock()

    def available(self) -> List[str]:
        found: List[str] = []
        try:
            import faster_whisper  # type: ignore  # noqa: F401
            found.append("faster-whisper")
        except ImportError:
            pass
        if shutil.which(self.binary):
            found.append("whisper.cpp")
        try:
            import vosk  # type: ignore  # noqa: F401
            found.append("vosk")
        except ImportError:
            pass
        return found

    def transcribe(self, audio_path: str, *, language: str = "") -> Transcript:
        language = language or self.language
        backends = ([self.backend] if self.backend != "auto" else self.available())
        if not backends:
            return Transcript("", "none", error=(
                "no offline speech backend found. Install one of: "
                "`pip install faster-whisper`, build whisper.cpp, or `pip install vosk`."))
        errors: List[str] = []
        for backend in backends or ["none"]:
            try:
                if backend == "faster-whisper":
                    return self._faster_whisper(audio_path, language)
                if backend in ("whisper.cpp", "whisper-cli"):
                    return self._whisper_cpp(audio_path)
                if backend == "vosk":
                    return self._vosk(audio_path)
            except Exception as exc:
                errors.append(f"{backend}: {exc}")
        return Transcript("", "+".join(backends), error="; ".join(errors))

    # -- backends ----------------------------------------------------------
    def _faster_whisper(self, path: str, language: str) -> Transcript:
        from faster_whisper import WhisperModel  # type: ignore

        with self._lock:
            if self._model is None:
                self._model = WhisperModel(self.model, device="cpu",
                                           compute_type=self.compute_type)
        segments, info = self._model.transcribe(path, language=language, vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return Transcript(text, "faster-whisper", getattr(info, "duration", 0.0), language)

    def _whisper_cpp(self, path: str) -> Transcript:
        binary = shutil.which(self.binary) or self.binary
        args = [binary, "-f", path, "-nt", "-otxt", "-of", path + ".out"]
        if self.model_path:
            args += ["-m", self.model_path]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=600)
        out_file = Path(path + ".out.txt")
        text = out_file.read_text("utf-8", "ignore").strip() if out_file.exists() else ""
        if not text:
            text = (proc.stdout or "").strip()
        return Transcript(text, "whisper.cpp", audio_duration(path), self.language)

    def _vosk(self, path: str) -> Transcript:
        import wave as _wave

        import vosk  # type: ignore

        vosk.SetLogLevel(-1)
        model = vosk.Model(self.model_path) if self.model_path else vosk.Model(lang="en-us")
        with _wave.open(path, "rb") as handle:
            data = json.dumps({"recognition": {"model": "vosk-model-small-en-us-0.15",
                                                "result": model.recognize(words=list(handle.readframes(
                                                    handle.getnframes())), language="en")}})
        return Transcript(json.loads(data)["recognition"]["result"].get("text", ""),
                          "vosk", audio_duration(path), "en")


def transcribe_bytes(config: Any, data: bytes, filename: str = "clip.webm") -> Transcript:
    suffix = Path(filename).suffix.lstrip(".") or "wav"
    path = decode_audio(data, suffix)
    return STT(config).transcribe(path)
