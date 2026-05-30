"""
Runs a single MCP instance. Started as a subprocess by the process manager.
Usage: python app/mcp_runner.py --config configs/mcp1.json
"""
import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.schema import MCPConfig
from app.tool_loader import load_openwebui_json, create_tools_instance

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("mcp_runner")


def load_config(config_path: str) -> MCPConfig:
    raw = json.loads(Path(config_path).read_text())
    return MCPConfig.model_validate(raw)


def resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return BASE_DIR / path


async def run_server(config_path: str, host_override: str | None = None) -> None:
    from mcp.server import Server
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp import types
    import uvicorn

    cfg = load_config(config_path)
    if host_override:
        cfg.server.host = host_override
    logger.info(f"Starting MCP '{cfg.name}' on {cfg.server.host}:{cfg.server.port}")

    tool_path = resolve_path(cfg.tool_source.path)
    tool = load_openwebui_json(tool_path)
    if tool is None:
        logger.error(f"Failed to load tool source: {tool_path}")
        sys.exit(1)

    tools_instance = create_tools_instance(tool, cfg.values)
    if tools_instance is None:
        logger.error("Failed to instantiate Tools class")
        sys.exit(1)

    mcp_tool_defs = tool.get_mcp_tool_defs()
    logger.info(f"Loaded {len(mcp_tool_defs)} tools: {[t['name'] for t in mcp_tool_defs]}")

    server = Server(cfg.id)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in mcp_tool_defs
        ]

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict | None
    ) -> list[types.TextContent]:
        arguments = arguments or {}
        method = getattr(tools_instance, name, None)
        if method is None:
            return [types.TextContent(type="text", text=f"Tool '{name}' not found")]
        try:
            if inspect.iscoroutinefunction(method):
                result = await method(**arguments)
            else:
                result = await asyncio.to_thread(method, **arguments)
            return [types.TextContent(type="text", text=str(result))]
        except Exception as e:
            logger.error(f"Error calling {name}: {e}")
            return [types.TextContent(type="text", text=f"Error: {e}")]

    endpoint = cfg.server.endpoint.rstrip("/")
    session_manager = StreamableHTTPSessionManager(server)
    mcp_auth_token = os.environ.get("MCP_BEARER_TOKEN") or None
    if mcp_auth_token:
        logger.info("MCP Bearer token authentication enabled")

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with session_manager.run():
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                event = await receive()
                if event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
            return

        # Bearer token check
        if mcp_auth_token:
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth_header = headers.get(b"authorization", b"").decode("utf-8", errors="replace")
            provided = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
            if provided != mcp_auth_token:
                from starlette.responses import Response
                await Response(
                    "Unauthorized", status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return

        if scope.get("path", "").rstrip("/") == endpoint:
            await session_manager.handle_request(scope, receive, send)
            return
        from starlette.responses import Response
        await Response("Not Found", status_code=404)(scope, receive, send)

    config = uvicorn.Config(
        app,
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
    server_instance = uvicorn.Server(config)
    await server_instance.serve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default=None, help="Override bind host from config")
    args = parser.parse_args()
    asyncio.run(run_server(args.config, host_override=args.host))


if __name__ == "__main__":
    main()
