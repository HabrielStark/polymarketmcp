"""Tests for the secret store (NFR-SEC-001) and the SigningVault built on it."""

from __future__ import annotations

import pytest

from hermes_pm.config import load_settings
from hermes_pm.execution.live_adapter import SigningVault
from hermes_pm.execution.secrets import (
    EncryptedFileSecretStore,
    EnvSecretStore,
    KeyringSecretStore,
    make_secret_store,
)


def test_env_store_roundtrip():
    s = EnvSecretStore(env={"HPM_SECRET_X_API": "tok"})
    assert s.available() and s.get("x_api") == "tok"
    assert "x_api" in s.names()
    assert EnvSecretStore(env={}).available() is False


def test_encrypted_file_roundtrip_and_at_rest(tmp_path):
    path = tmp_path / "secrets.enc"
    store = EncryptedFileSecretStore(path, passphrase="correct horse battery staple")
    store.set("live_signing_key", "SUPER-SECRET-KEY-123")
    # New instance (cold read) decrypts correctly.
    store2 = EncryptedFileSecretStore(path, passphrase="correct horse battery staple")
    assert store2.get("live_signing_key") == "SUPER-SECRET-KEY-123"
    # On-disk file is ciphertext: the plaintext value must not appear.
    assert "SUPER-SECRET-KEY-123" not in path.read_text(encoding="utf-8")


def test_encrypted_file_wrong_passphrase_fails(tmp_path):
    path = tmp_path / "secrets.enc"
    EncryptedFileSecretStore(path, "right-pass").set("k", "v")
    with pytest.raises(PermissionError):
        EncryptedFileSecretStore(path, "wrong-pass").get("k")


def test_encrypted_file_unavailable_without_passphrase(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "s.enc", passphrase=None)
    assert store.available() is False
    with pytest.raises(PermissionError):
        store.get("k")


def test_keyring_store_does_not_crash():
    store = KeyringSecretStore()
    assert isinstance(store.available(), bool)
    assert store.get("nonexistent-secret-xyz") is None


def test_make_secret_store_selects_backend(tmp_path):
    env_s = make_secret_store(load_settings(data_dir=str(tmp_path)))
    assert env_s.backend == "env"
    enc = make_secret_store(load_settings(
        data_dir=str(tmp_path), secret_store="encrypted_file",
        secret_master_passphrase="p"))
    assert enc.backend == "encrypted_file"
    kr = make_secret_store(load_settings(data_dir=str(tmp_path), secret_store="keyring"))
    assert kr.backend == "keyring"


# --- SigningVault built on the store ----------------------------------------- #
def test_vault_locked_by_default():
    v = SigningVault(EnvSecretStore(env={}), "live_signing_key")
    assert v.available is False
    with pytest.raises(PermissionError):
        v.sign("ref")
    assert v.status()["exposes_secrets"] is False


def test_vault_signs_without_exposing_key(tmp_path):
    store = EncryptedFileSecretStore(tmp_path / "s.enc", "master-pass")
    store.set("live_signing_key", "PRIVATE-MATERIAL")
    v = SigningVault(store, "live_signing_key")
    assert v.available is True
    sig = v.sign("order-ref-1")
    # signature is a hex HMAC, deterministic, and never the key itself
    assert len(sig) == 64 and all(c in "0123456789abcdef" for c in sig)
    assert "PRIVATE-MATERIAL" not in sig
    assert v.sign("order-ref-1") == sig  # deterministic
    assert v.sign("order-ref-2") != sig
    assert v.status()["exposes_secrets"] is False and v.status()["backend"] == "encrypted_file"


async def test_daemon_never_leaks_master_passphrase_or_key(tmp_path):
    import json

    from hermes_pm.daemon.core import TradingDaemon
    # Provision a real encrypted key store, then confirm no surface leaks it.
    store = EncryptedFileSecretStore(tmp_path / "s.enc", "MASTERPASS-XYZ")
    store.set("live_signing_key", "KEYMATERIAL-ABC")
    s = load_settings(data_dir=str(tmp_path), secret_store="encrypted_file",
                      secret_master_passphrase="MASTERPASS-XYZ")
    d = TradingDaemon(s)
    await d.start()
    try:
        blob = json.dumps(d.get_config()) + json.dumps(d.get_system_status()) + \
            json.dumps(d.live.vault_status())
        assert "MASTERPASS-XYZ" not in blob
        assert "KEYMATERIAL-ABC" not in blob
        assert d.live.vault_status()["exposes_secrets"] is False
    finally:
        await d.stop()
