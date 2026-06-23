"""Isolated live-adapter process (NFR-SEC-007).

Runs the compliance-locked :class:`LiveAdapter` in its **own OS process** with the
smallest possible API surface: a line-delimited JSON protocol over stdin/stdout
that accepts ONLY references (intent id, risk-decision id, confirmation token,
order ref) — never raw order parameters and never secrets. The signing vault and
secret store live only inside this process; the main daemon never holds key
material. This process never writes to the shared audit chain (avoiding
multi-writer corruption) and never prints secrets.

Protocol (one JSON object per line):
  {"cmd": "status"}                         -> {"ok": true, "vault": {...}}
  {"cmd": "place_intent", "trade_intent_id": "...", "risk_decision_id": "...",
   "user_confirmation_token": "..."}        -> {"ok": true, "result": {...}}
  {"cmd": "cancel", "order_ref": "..."}     -> {"ok": true, "result": {...}}
  {"cmd": "open_orders"}                     -> {"ok": true, "result": [...]}
  {"cmd": "shutdown"}                        -> process exits
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any


class _NullAudit:
    """No-op audit: the isolated process must not write the shared hash chain."""

    def append(self, *_a: Any, **_k: Any) -> None:  # noqa: D401
        return None


def _build_adapter():
    from hermes_pm.config import load_settings
    from hermes_pm.execution.live_adapter import LiveAdapter
    from hermes_pm.persistence.db import Database

    settings = load_settings()
    db = Database(settings.db_path)
    # geoblock_check=None -> ComplianceGate fails closed (no live data to verify).
    return LiveAdapter(
        settings,
        _NullAudit(),
        db.get_risk_decision,
        geoblock_check=None,
        load_vault=True,
        process_isolated=True,
    )


async def _handle(adapter, msg: dict[str, Any]) -> dict[str, Any]:
    cmd = msg.get("cmd")
    if cmd == "status":
        return {"ok": True, "vault": adapter.vault_status(), "enabled": adapter.enabled}
    if cmd == "place_intent":
        result = await adapter.place_order_intent(
            str(msg.get("trade_intent_id", "")), str(msg.get("risk_decision_id", "")),
            msg.get("user_confirmation_token"),
        )
        return {"ok": True, "result": result}
    if cmd == "cancel":
        return {"ok": True, "result": await adapter.cancel_order(str(msg.get("order_ref", "")))}
    if cmd == "open_orders":
        return {"ok": True, "result": await adapter.get_open_orders()}
    return {"ok": False, "error": f"unknown cmd: {cmd}"}


def _main() -> None:
    # Synchronous, cross-platform stdin loop (asyncio.connect_read_pipe does not
    # work for console pipes on the Windows Proactor loop). Each command is
    # handled on a fresh event loop; the adapter does no long-lived I/O.
    adapter = _build_adapter()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"ok": False, "error": "bad json"}) + "\n")
            sys.stdout.flush()
            continue
        if msg.get("cmd") == "shutdown":
            break
        resp = asyncio.run(_handle(adapter, msg))
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _main()



class LiveProcessClient:
    """Spawns and drives the isolated live-adapter process. The main daemon uses
    this instead of an in-process adapter when ``live_process_isolation`` is set,
    so secrets/keys never reside in the daemon's address space.

    Resilience (a hung or crashed child must never wedge the daemon):
      * every RPC is bounded by ``_timeout`` — a child that stops responding is
        killed and the call returns a clean error instead of blocking forever;
      * a child that has exited (EOF) or a broken pipe is detected, reaped, and
        the next call transparently respawns a fresh child;
      * all of the above happens under a single lock so concurrent callers cannot
        interleave writes/reads on the pipe.
    """

    #: Default per-RPC timeout (seconds). The adapter does only fast, local work
    #: (it is compliance-locked), so a slow response means a stuck child.
    RPC_TIMEOUT = 10.0

    def __init__(self, settings) -> None:
        self._s = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._timeout = float(getattr(settings, "live_rpc_timeout_s", self.RPC_TIMEOUT))
        #: Number of RPCs that faulted (timeout / crash / pipe error) and forced
        #: a child to be reaped. A climbing value is the signal to alarm on.
        self.faults = 0

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({
            "HPM_DATA_DIR": str(self._s.data_dir),
            "HPM_DB_FILENAME": self._s.db_filename,
            "HPM_LIVE_ENABLED": str(self._s.live_enabled),
            "HPM_SECRET_STORE": self._s.secret_store,
            "HPM_SIGNING_KEY_NAME": self._s.signing_key_name,
        })
        if self._s.secret_store_path:
            env["HPM_SECRET_STORE_PATH"] = str(self._s.secret_store_path)
        if self._s.secret_master_passphrase:
            env["HPM_SECRET_MASTER_PASSPHRASE"] = self._s.secret_master_passphrase
        return env

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"ok": False, "error": message}

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_started_locked()

    async def _ensure_started_locked(self) -> None:
        """Spawn a child if none is running. Caller must hold ``self._lock``."""
        if self._alive():
            return
        if self._proc is not None:  # a dead handle is lingering — reap it first
            await self._terminate_locked()
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "hermes_pm.execution.live_process",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            env=self._child_env(),
        )

    async def _terminate_locked(self) -> None:
        """Kill and reap the current child (best-effort). Caller holds the lock."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.returncode is None:
                proc.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=2.0)

    async def _rpc(self, msg: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            try:
                await self._ensure_started_locked()
            except Exception as exc:  # noqa: BLE001 - spawning the child can fail
                self.faults += 1
                await self._terminate_locked()
                return self._error(f"live process failed to start: {exc}")

            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                return self._error("live process unavailable")

            try:
                proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                await asyncio.wait_for(proc.stdin.drain(), timeout=self._timeout)
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=self._timeout)
            except TimeoutError:
                # Hung child: kill it so the NEXT call gets a fresh process.
                self.faults += 1
                await self._terminate_locked()
                return self._error("live process timed out and was terminated")
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                self.faults += 1
                await self._terminate_locked()
                return self._error(f"live process pipe error: {exc}")

            if not line:  # EOF — the child exited/crashed without replying
                self.faults += 1
                await self._terminate_locked()
                return self._error("live process exited without responding")
            try:
                return json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._error("live process returned a malformed response")

    async def vault_status(self) -> dict[str, Any]:
        return (await self._rpc({"cmd": "status"})).get("vault", {})

    async def place_order_intent(self, trade_intent_id: str, risk_decision_id: str,
                                 user_confirmation_token: str | None = None) -> dict[str, Any]:
        r = await self._rpc({"cmd": "place_intent", "trade_intent_id": trade_intent_id,
                             "risk_decision_id": risk_decision_id,
                             "user_confirmation_token": user_confirmation_token})
        return r.get("result", r)

    async def cancel_order(self, order_ref: str) -> dict[str, Any]:
        r = await self._rpc({"cmd": "cancel", "order_ref": order_ref})
        return r.get("result", r)

    async def get_open_orders(self) -> list[dict[str, Any]]:
        r = await self._rpc({"cmd": "open_orders"})
        return r.get("result", []) if r.get("ok", True) else []

    async def stop(self) -> None:
        async with self._lock:
            proc = self._proc
            if proc is not None and proc.returncode is None and proc.stdin is not None:
                with contextlib.suppress(Exception):
                    proc.stdin.write(b'{"cmd": "shutdown"}\n')
                    await asyncio.wait_for(proc.stdin.drain(), timeout=2.0)
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
            await self._terminate_locked()
