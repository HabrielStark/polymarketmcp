"""MCP Streamable HTTP transport (MCP-SR-002, S2).

Wires the same low-level :class:`Server` to the SDK's
``StreamableHTTPSessionManager`` behind a Starlette app that:
  * binds only to ``127.0.0.1`` by default,
  * enables DNS-rebinding protection (Origin + Host allow-lists),
  * requires a bearer token on every request (configurable via
    ``HPM_MCP_HTTP_TOKEN``).

stdio remains the primary local transport (MCP-SR-001); HTTP is opt-in via
``HPM_MCP_HTTP_ENABLED``."""

from __future__ import annotations

import contextlib

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount

from hermes_pm.config import Settings, load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.mcp.server import build_server
from hermes_pm.util.security import tokens_match


def _security_settings(s: Settings) -> TransportSecuritySettings:
    hosts = [f"{s.mcp_http_host}:{s.mcp_http_port}", f"127.0.0.1:{s.mcp_http_port}",
             f"localhost:{s.mcp_http_port}"]
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins
    )


def create_http_app(daemon: TradingDaemon) -> Starlette:
    s = daemon.settings
    server = build_server(daemon)
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=False,
        security_settings=_security_settings(s),
    )

    async def handle_mcp(scope, receive, send) -> None:
        # Bearer-token gate (MCP-SR-002 "require authentication"). When a token is
        # configured every request must present it; the SDK security settings
        # independently enforce Origin/Host (DNS-rebinding protection).
        token = s.mcp_http_token
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if token:
            if not tokens_match(f"Bearer {token}", headers.get("authorization")):
                resp = JSONResponse({"error": "unauthorized"}, status_code=401)
                await resp(scope, receive, send)
                return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await daemon.start()
        async with manager.run():
            try:
                yield
            finally:
                await daemon.stop()

    return Starlette(routes=[Mount("/mcp", app=handle_mcp)], lifespan=lifespan)


async def _serve(daemon: TradingDaemon) -> None:
    import uvicorn

    s = daemon.settings
    config = uvicorn.Config(create_http_app(daemon), host=s.mcp_http_host, port=s.mcp_http_port,
                            log_level="warning")
    await uvicorn.Server(config).serve()


def run_http(settings: Settings | None = None) -> None:
    import asyncio

    settings = settings or load_settings()
    asyncio.run(_serve(TradingDaemon(settings)))
