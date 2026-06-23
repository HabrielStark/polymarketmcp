"""Tests for the isolated live-adapter process (NFR-SEC-007)."""

from __future__ import annotations

import json

import pytest

from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.execution.live_process import LiveProcessClient
from hermes_pm.execution.secrets import EncryptedFileSecretStore

pytestmark = pytest.mark.asyncio


async def test_isolated_process_blocks_and_hides_secrets(tmp_path):
    # Provision a real key inside an encrypted store the SUBPROCESS will load.
    store = EncryptedFileSecretStore(tmp_path / "secrets.enc", "MASTER-PASS-9")
    store.set("live_signing_key", "KEY-MATERIAL-SECRET")
    settings = load_settings(
        data_dir=str(tmp_path), db_filename="lp.sqlite3", secret_store="encrypted_file",
        secret_store_path=str(tmp_path / "secrets.enc"), secret_master_passphrase="MASTER-PASS-9",
        live_enabled=True,  # even enabled, every other gate keeps it blocked
    )
    client = LiveProcessClient(settings)
    await client.start()
    try:
        status = await client.vault_status()
        # The subprocess CAN see its key (vault available), but never exposes it.
        assert status["exposes_secrets"] is False
        blob = json.dumps(status)

        placed = await client.place_order_intent("ti_x", "rd_y", "confirm")
        assert placed["status"] == "blocked"  # geoblock/red-team/etc. fail closed
        blob += json.dumps(placed)

        cancelled = await client.cancel_order("ord-1")
        assert cancelled["cancelled"] is True

        assert (await client.get_open_orders()) == []

        # No secret material crosses the IPC boundary.
        assert "KEY-MATERIAL-SECRET" not in blob
        assert "MASTER-PASS-9" not in blob
    finally:
        await client.stop()


async def test_daemon_uses_isolated_process_when_enabled(tmp_path):
    settings = load_settings(data_dir=str(tmp_path), db_filename="lp2.sqlite3",
                             live_process_isolation=True, reconcile_interval_ms=60000,
                             ws_reconnect_stale_ms=60000)
    d = TradingDaemon(settings)
    await d.start()
    try:
        result = await d.live_place_order_intent("ti", "rd")
        assert result["status"] == "blocked"
        # daemon recorded an audit event for the isolated call
        assert any(e["type"] == "live_order_blocked" for e in d.get_audit_events(limit=50))
        assert d.audit.verify_chain()["ok"] is True
    finally:
        await d.stop()


async def test_daemon_parent_vault_stays_locked_when_process_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HPM_SECRET_LIVE_SIGNING_KEY", "PARENT-SHOULD-NOT-LOAD")
    settings = load_settings(data_dir=str(tmp_path), db_filename="lp3.sqlite3",
                             live_process_isolation=True, secret_store="env",
                             reconcile_interval_ms=60000, ws_reconnect_stale_ms=60000)
    d = TradingDaemon(settings)
    await d.start()
    try:
        status = d.get_system_status()["signing_vault"]
        assert status["process_isolated"] is True
        assert status["backend"] == "none"
        assert status["unlocked"] is False
        assert "PARENT-SHOULD-NOT-LOAD" not in json.dumps(status)
    finally:
        await d.stop()
