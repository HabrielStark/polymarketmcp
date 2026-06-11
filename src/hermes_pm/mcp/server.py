"""MCP server (Section 10).

Exposes the daemon's operations as MCP tools/resources/prompts over stdio
(MCP-SR-001). Inputs are strictly validated against each tool's JSON Schema with
``additionalProperties: false`` so unknown fields are rejected (MCP-SR-005,
NFR-SEC-003). No tool ever executes shell from arguments (MCP-SR-004), and the
live tools are reference-only and compliance-locked (AC-006). On stdio, stdout is
reserved for JSON-RPC; all logs go to stderr (MCP-SR-003)."""

from __future__ import annotations

import json
import sys

import jsonschema
import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import ValidationError as PydanticValidationError

from hermes_pm.config import Settings, load_settings
from hermes_pm.daemon.core import TradingDaemon
from hermes_pm.errors import HermesPMError
from hermes_pm.mcp.prompts import PROMPT_SPECS, PROMPTS_BY_NAME, render_prompt
from hermes_pm.mcp.resources import RESOURCE_TEMPLATES, resolve_resource
from hermes_pm.mcp.tools import TOOL_SPECS, TOOLS_BY_NAME


def _json(data: object) -> str:
    return json.dumps(data, default=str, ensure_ascii=False)


def _validate(schema: dict, args: dict) -> str | None:
    """Return None if valid, else a human-readable rejection reason."""
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(args or {}), key=lambda e: list(e.path))
    if errors:
        return "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors)
    return None


def build_server(daemon: TradingDaemon) -> Server:
    server: Server = Server("hermes-pm")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.schema)
            for t in TOOL_SPECS
        ]

    @server.call_tool(validate_input=False)  # we validate explicitly for strict control
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        arguments = arguments or {}
        spec = TOOLS_BY_NAME.get(name)
        if spec is None:
            return [types.TextContent(type="text",
                                      text=_json({"error": {"code": "unknown_tool", "message": name}}))]
        reason = _validate(spec.schema, arguments)
        if reason is not None:
            return [types.TextContent(type="text", text=_json(
                {"error": {"code": "schema_rejected", "message": reason}}))]
        try:
            method = getattr(daemon, spec.method)
            result = await method(**arguments) if spec.is_async else method(**arguments)
            return [types.TextContent(type="text", text=_json(result))]
        except HermesPMError as exc:
            return [types.TextContent(type="text", text=_json({"error": exc.to_dict()}))]
        except PydanticValidationError as exc:
            # Out-of-range / non-finite / wrong-typed numeric args reach the model
            # layer and raise here. Surface a compact, structured rejection instead
            # of letting a pydantic traceback escape the tool boundary.
            reasons = "; ".join(
                f"{'/'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors()
            )
            return [types.TextContent(type="text", text=_json(
                {"error": {"code": "validation_error", "message": reasons}}))]
        except (TypeError, ValueError) as exc:
            return [types.TextContent(type="text", text=_json(
                {"error": {"code": "bad_request", "message": str(exc)}}))]
        except Exception as exc:  # noqa: BLE001 - the tool boundary must never crash the server
            # Log the detail to stderr for diagnosis; return a generic message so
            # internal state (and any secrets in it) is never leaked to the client.
            print(f"[mcp] tool {name!r} internal error: {exc!r}", file=sys.stderr)
            return [types.TextContent(type="text", text=_json(
                {"error": {"code": "internal_error", "message": "internal error"}}))]

    @server.list_resources()
    async def list_resources() -> list[types.Resource]:
        out = [types.Resource(uri="system://status", name="System status",
                              description="System health, connectivity, mode, locks.")]
        for c in daemon.db.list_campaigns()[:20]:
            out.append(types.Resource(uri=f"campaign://{c.campaign_id}/summary",
                                      name=f"Campaign {c.name}", description="Campaign summary."))
            out.append(types.Resource(uri=f"portfolio://paper/{c.campaign_id}",
                                      name=f"Paper portfolio {c.name}", description="Paper P&L."))
        for m in daemon.db.list_markets()[:40]:
            out.append(types.Resource(uri=f"market://{m.market_id}", name=m.question[:60],
                                      description="Market metadata."))
        return out

    @server.list_resource_templates()
    async def list_resource_templates() -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(uriTemplate=uri, name=uri.split("://")[0], description=desc)
            for uri, desc in RESOURCE_TEMPLATES
        ]

    @server.read_resource()
    async def read_resource(uri) -> str:
        return _json(resolve_resource(daemon, str(uri)))

    @server.list_prompts()
    async def list_prompts() -> list[types.Prompt]:
        return [
            types.Prompt(
                name=p.name, description=p.description,
                arguments=[types.PromptArgument(name=a["name"], description=a["description"],
                                                required=a["required"]) for a in p.arguments],
            )
            for p in PROMPT_SPECS
        ]

    @server.get_prompt()
    async def get_prompt(name: str, arguments: dict | None) -> types.GetPromptResult:
        spec = PROMPTS_BY_NAME.get(name)
        text = render_prompt(name, arguments or {})
        return types.GetPromptResult(
            description=spec.description if spec else f"Prompt {name}",
            messages=[types.PromptMessage(role="user",
                                          content=types.TextContent(type="text", text=text))],
        )

    return server


async def run_stdio(settings: Settings | None = None) -> None:
    from mcp.server.stdio import stdio_server

    settings = settings or load_settings()
    daemon = TradingDaemon(settings)
    await daemon.start()
    server = build_server(daemon)
    print(f"[hermes-pm] MCP stdio server up; source={settings.market_data_source}", file=sys.stderr)
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        await daemon.stop()
