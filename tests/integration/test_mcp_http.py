"""MCP Streamable HTTP transport tests (MCP-SR-002): real handshake over HTTP,
bearer-token enforcement, and Origin/DNS-rebinding rejection."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from hermes_pm.config import load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.mcp.http_server import create_http_app

pytestmark = pytest.mark.asyncio
TOKEN = "HTTP-TEST-TOKEN"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def _server(tmp_path):
    port = _free_port()
    settings = load_settings(data_dir=str(tmp_path), db_filename="http.sqlite3",
                             mcp_http_enabled=True, mcp_http_host="127.0.0.1",
                             mcp_http_port=port, mcp_http_token=TOKEN)
    daemon = TradingDaemon(settings)
    app = create_http_app(daemon)  # lifespan starts/stops the daemon
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if server.started:
            break
    return server, task, port


async def test_http_full_mcp_handshake(tmp_path):
    server, task, port = await _server(tmp_path)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {TOKEN}"}) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 45
                result = await session.call_tool("get_system_status", {})
                import json
                status = json.loads(result.content[0].text)
                assert status["mode"] in ("paper", "emergency")
    finally:
        server.should_exit = True
        await task


async def test_http_rejects_missing_token(tmp_path):
    server, task, port = await _server(tmp_path)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            # No Authorization header -> 401 before reaching the MCP machinery.
            resp = await c.post(f"http://127.0.0.1:{port}/mcp/", json={"jsonrpc": "2.0", "id": 1,
                                "method": "initialize", "params": {}},
                                headers={"Origin": f"http://127.0.0.1:{port}"})
            assert resp.status_code == 401
    finally:
        server.should_exit = True
        await task


async def test_http_rejects_bad_origin(tmp_path):
    server, task, port = await _server(tmp_path)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            resp = await c.post(f"http://127.0.0.1:{port}/mcp/",
                                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                                headers={"Authorization": f"Bearer {TOKEN}",
                                         "Origin": "http://evil.example.com",
                                         "Content-Type": "application/json",
                                         "Accept": "application/json, text/event-stream"})
            # DNS-rebinding protection rejects a foreign Origin.
            assert resp.status_code in (400, 403)
    finally:
        server.should_exit = True
        await task
