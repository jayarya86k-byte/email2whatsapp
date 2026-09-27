"""Encrypted local secret vault.

stdlib only. Format: scrypt-less PBKDF2-HMAC-SHA256 key derivation plus an
HMAC-CTR keystream and encrypt-then-MAC (Poly1305 is unavailable in stdlib,
so the MAC uses HMAC-SHA256 over ciphertext with a separate derived subkey).

    {"v":1,"kdf":{"algo":"pbkdf2-sha256","iters":600000,"salt":b64},
     "cipher":{"name":"hmac-ctr-sha256","iv":b64,"ct":b64},
     "mac":{"tag":b64}}

This keeps credentials off disk in plaintext. On a real deployment prefer a
hardware-backed keystore (macOS Keychain / Windows DPAPI / libsecret) --
`get_keyring_backend` reports whether one is importable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Dict, Iterable, List, Optional

PBKDF2_ITERS = 600_000
BLOCK = 32


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode())


def _keystream(key: bytes, iv: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, iv + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _derive(password: str, salt: bytes, iters: int) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters, dklen=64)
    return material[:32], material[32:]  # enc key, mac key


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


class Vault:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(os.path.expanduser(str(path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: Dict[str, str] = {}
        self._password: Optional[str] = None
        self._loaded = False

    # -- lifecycle ---------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    def unlock(self, password: str, create: bool = True) -> bool:
        self._password = password
        if not self.path.exists():
            if not create:
                raise RuntimeError("vault does not exist")
            self._data = {}
            self._loaded = True
            self._save()
            return True
        blob = json.loads(self.path.read_text("utf-8"))
        salt = _unb64(blob["kdf"]["salt"])
        iters = int(blob["kdf"]["iters"])
        enc_key, mac_key = _derive(password, salt, iters)
        iv = _unb64(blob["cipher"]["iv"])
        ciphertext = _unb64(blob["cipher"]["ct"])
        tag = _unb64(blob["mac"]["tag"])
        expected = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            self._password = None
            return False
        self._data = json.loads(_xor(ciphertext, _keystream(enc_key, iv, len(ciphertext))).decode())
        self._loaded = True
        return True

    def lock(self) -> None:
        self._data = {}
        self._password = None
        self._loaded = False

    @property
    def unlocked(self) -> bool:
        return self._loaded

    # -- accessors ---------------------------------------------------------
    def set(self, key: str, value: str) -> None:
        self._require_unlocked()
        self._data[key] = value
        self._save()

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        self._require_unlocked()
        return self._data.get(key, default)

    def delete(self, key: str) -> bool:
        self._require_unlocked()
        if key in self._data:
            del self._data[key]
            self._save()
            return True
        return False

    def keys(self) -> List[str]:
        self._require_unlocked()
        return sorted(self._data)

    def _require_unlocked(self) -> None:
        if not self._loaded:
            raise RuntimeError("vault is locked")

    # -- persistence -------------------------------------------------------
    def _save(self) -> None:
        if not self._password:
            return
        salt = secrets.token_bytes(16)
        iv = secrets.token_bytes(16)
        enc_key, mac_key = _derive(self._password, salt, PBKDF2_ITERS)
        plaintext = json.dumps(self._data, sort_keys=True).encode()
        ciphertext = _xor(plaintext, _keystream(enc_key, iv, len(plaintext)))
        tag = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        blob = {
            "v": 1,
            "kdf": {"algo": "pbkdf2-sha256", "iters": PBKDF2_ITERS, "salt": _b64(salt)},
            "cipher": {"name": "hmac-ctr-sha256", "iv": _b64(iv), "ct": _b64(ciphertext)},
            "mac": {"tag": _b64(tag)},
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def rotate_password(self, new_password: str) -> bool:
        if not self._loaded:
            return False
        self._password = new_password
        self._save()
        return True


def get_keyring_backend() -> Optional[str]:
    """Return the name of an importable OS keystore module, if any."""
    for name in ("keyring", "secretstorage"):
        try:
            __import__(name)
            return name
        except Exception:
            continue
    return None


def mask(value: str, keep: int = 3) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * (len(value) - keep * 2)}{value[-keep:]}"


def env_style_hints(keys: Iterable[str]) -> Dict[str, str]:
    return {key: key.upper().replace("-", "_").replace(".", "_") for key in keys}
