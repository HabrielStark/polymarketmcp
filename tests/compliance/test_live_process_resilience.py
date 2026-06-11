"""Resilience tests for LiveProcessClient (NFR-SEC-007 hardening).

A hung or crashed isolated live-adapter process must never wedge the daemon:
every RPC is time-bounded, a dead child is reaped, and the next call respawns a
fresh one. These use a fake process so the failure modes are deterministic and
fast (no reliance on a real child actually hanging).
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_pm.config import load_settings
from hermes_pm.execution.live_process import LiveProcessClient

pytestmark = pytest.mark.asyncio


class _FakeStdin:
    def __init__(self, write_exc: Exception | None = None) -> None:
        self._write_exc = write_exc
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        if self._write_exc is not None:
            raise self._write_exc
        self.writes.append(data)

    async def drain(self) -> None:
        return None


class _FakeStdout:
    def __init__(self, mode: object) -> None:
        # mode: "hang" (never returns), "eof" (returns b""), or bytes (one line)
        self._mode = mode

    async def readline(self) -> bytes:
        if self._mode == "hang":
            await asyncio.Event().wait()  # blocks until cancelled
        if self._mode == "eof":
            return b""
        return self._mode  # type: ignore[return-value]


class _FakeProc:
    def __init__(self, stdout_mode: object, write_exc: Exception | None = None) -> None:
        self.stdin = _FakeStdin(write_exc)
        self.stdout = _FakeStdout(stdout_mode)
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


def _client(tmp_path) -> LiveProcessClient:
    c = LiveProcessClient(load_settings(data_dir=str(tmp_path)))
    c._timeout = 0.2  # fail fast in tests
    return c


async def test_hung_child_times_out_and_is_reaped(tmp_path):
    c = _client(tmp_path)
    fake = _FakeProc("hang")
    c._proc = fake  # pretend a live child is running
    r = await asyncio.wait_for(c._rpc({"cmd": "status"}), timeout=3)
    assert r["ok"] is False and "timed out" in r["error"]
    assert fake.killed is True          # the stuck child was killed
    assert c._proc is None              # reaped -> next call will respawn


async def test_crashed_child_eof_is_clean(tmp_path):
    c = _client(tmp_path)
    c._proc = _FakeProc("eof")
    r = await c._rpc({"cmd": "status"})
    assert r["ok"] is False and "without responding" in r["error"]
    assert c._proc is None


async def test_broken_pipe_on_write_is_clean(tmp_path):
    c = _client(tmp_path)
    c._proc = _FakeProc(b'{"ok": true}\n', write_exc=BrokenPipeError("child gone"))
    r = await c._rpc({"cmd": "status"})
    assert r["ok"] is False and "pipe error" in r["error"]
    assert c._proc is None


async def test_recovers_by_respawning_after_failure(tmp_path, monkeypatch):
    c = _client(tmp_path)
    c._proc = _FakeProc("eof")  # first child is dead
    r1 = await c._rpc({"cmd": "status"})
    assert r1["ok"] is False and c._proc is None

    # Next call must transparently spawn a fresh, healthy child.
    healthy = _FakeProc(b'{"ok": true, "vault": {"locked": true}}\n')

    async def fake_spawn(*_a, **_k):
        return healthy

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    r2 = await c._rpc({"cmd": "status"})
    assert r2.get("ok") is True
    assert c._proc is healthy
    assert c.faults >= 1  # the earlier crash was counted as a fault


async def test_vault_status_never_raises_on_dead_child(tmp_path):
    # The public surface must degrade to a safe empty status, not raise.
    c = _client(tmp_path)
    c._proc = _FakeProc("eof")
    status = await c.vault_status()
    assert status == {}  # safe default; caller treats missing vault as locked


async def test_get_open_orders_safe_default_on_failure(tmp_path):
    c = _client(tmp_path)
    c._proc = _FakeProc("eof")
    orders = await c.get_open_orders()
    assert orders == []
