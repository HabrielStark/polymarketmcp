"""Isolated secret storage (NFR-SEC-001).

Three interchangeable backends behind one ``SecretStore`` protocol:
  * ``EnvSecretStore``     — reads ``HPM_SECRET_<NAME>`` env vars (default).
  * ``EncryptedFileSecretStore`` — secrets encrypted at rest with Fernet; the
    key is derived from a master passphrase via PBKDF2-HMAC-SHA256. The file on
    disk is ciphertext only.
  * ``KeyringSecretStore`` — the OS keychain (Windows Credential Locker, macOS
    Keychain, Linux Secret Service) via ``keyring``.

No backend ever logs a secret value, and ``redact`` masks any accidental
serialization by key name. The ``SigningVault`` is the only component that reads
material from the store, and it never returns it to a caller."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


@runtime_checkable
class SecretStore(Protocol):
    backend: str

    def available(self) -> bool: ...
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def names(self) -> list[str]: ...


class EnvSecretStore:
    """Reads secrets from ``HPM_SECRET_<NAME>`` (or an injected mapping)."""

    backend = "env"

    def __init__(self, env: dict[str, str] | None = None, prefix: str = "HPM_SECRET_") -> None:
        self._env = env if env is not None else dict(os.environ)
        self._prefix = prefix

    def available(self) -> bool:
        return any(k.startswith(self._prefix) for k in self._env)

    def get(self, name: str) -> str | None:
        return self._env.get(self._prefix + name.upper())

    def set(self, name: str, value: str) -> None:
        self._env[self._prefix + name.upper()] = value

    def names(self) -> list[str]:
        return sorted(
            k[len(self._prefix):].lower() for k in self._env if k.startswith(self._prefix)
        )


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


class EncryptedFileSecretStore:
    """Fernet-encrypted secret file; key derived from a master passphrase.

    Layout on disk: ``{"salt": <b64>, "blob": <fernet token over JSON dict>}``.
    Without the correct passphrase the contents are unreadable."""

    backend = "encrypted_file"

    def __init__(self, path: str | Path, passphrase: str | None) -> None:
        self._path = Path(path)
        self._passphrase = passphrase
        self._cache: dict[str, str] | None = None

    def available(self) -> bool:
        return bool(self._passphrase)

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._passphrase:
            raise PermissionError("encrypted secret store: no master passphrase configured")
        if not self._path.exists():
            self._cache = {}
            return self._cache
        text = self._path.read_text(encoding="utf-8").strip()
        if not text:  # empty file -> empty store (robustness)
            self._cache = {}
            return self._cache
        try:
            raw = json.loads(text)
            salt = base64.urlsafe_b64decode(raw["salt"])
            fernet = Fernet(_derive_key(self._passphrase, salt))
            self._cache = json.loads(fernet.decrypt(raw["blob"].encode("utf-8")).decode("utf-8"))
        except (InvalidToken, KeyError, ValueError) as exc:
            raise PermissionError("encrypted secret store: wrong passphrase or corrupt file") from exc
        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        if not self._passphrase:
            raise PermissionError("encrypted secret store: no master passphrase configured")
        salt = os.urandom(16)
        fernet = Fernet(_derive_key(self._passphrase, salt))
        blob = fernet.encrypt(json.dumps(data).encode("utf-8")).decode("utf-8")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"salt": base64.urlsafe_b64encode(salt).decode(),
                                          "blob": blob}), encoding="utf-8")
        self._cache = data

    def get(self, name: str) -> str | None:
        return self._load().get(name.lower())

    def set(self, name: str, value: str) -> None:
        data = dict(self._load())
        data[name.lower()] = value
        self._save(data)

    def names(self) -> list[str]:
        return sorted(self._load().keys())


class KeyringSecretStore:
    """OS keychain backend. ``available`` is False on headless/fail backends."""

    backend = "keyring"

    def __init__(self, service: str = "hermes-pm") -> None:
        self._service = service

    def _kr(self):
        import keyring  # local import; only needed for this backend
        return keyring

    def available(self) -> bool:
        try:
            kr = self._kr()
            name = kr.get_keyring().__class__.__name__.lower()
            return "fail" not in name and "null" not in name
        except Exception:  # noqa: BLE001
            return False

    def get(self, name: str) -> str | None:
        try:
            return self._kr().get_password(self._service, name.lower())
        except Exception:  # noqa: BLE001
            return None

    def set(self, name: str, value: str) -> None:
        self._kr().set_password(self._service, name.lower(), value)

    def names(self) -> list[str]:  # keyring has no portable enumeration
        return []


def make_secret_store(settings) -> SecretStore:
    """Build the configured secret store. Defaults to env (paper needs none)."""
    kind = getattr(settings, "secret_store", "env")
    if kind == "encrypted_file":
        path = getattr(settings, "secret_store_path", None) or (settings.data_dir / "secrets.enc")
        return EncryptedFileSecretStore(path, getattr(settings, "secret_master_passphrase", None))
    if kind == "keyring":
        return KeyringSecretStore()
    return EnvSecretStore()
