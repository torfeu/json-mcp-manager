"""
MCP Manager entry point.
Usage: python app/manager.py [--host HOST] [--port PORT]
"""
import argparse
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BASE_DIR))

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="MCP Manager")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument(
        "--no-code-edit", action="store_true",
        help="Disable inline code editor. Upload, config edit and delete remain available.",
    )
    parser.add_argument(
        "--no-edit", action="store_true",
        help="Full read-only mode: disables upload, code editor, config edit and delete.",
    )
    parser.add_argument(
        "--mcp-token", default=None, metavar="TOKEN",
        help="Require this Bearer token on all MCP endpoints (overrides settings file).",
    )
    parser.add_argument(
        "--no-token-edit", action="store_true",
        help="Disable changing the MCP Bearer token via the web UI.",
    )
    args = parser.parse_args()

    # Propagate bind host and port so the settings page can read them
    os.environ["MCP_RUNNER_HOST"] = args.host
    os.environ["MCP_MANAGER_PORT"] = str(args.port)

    # Edit mode: CLI flags > settings file > default "full"
    from app.settings_store import load_settings
    _file_settings = load_settings()
    if args.no_edit:
        os.environ["MCP_EDIT_MODE"] = "readonly"
    elif args.no_code_edit:
        os.environ["MCP_EDIT_MODE"] = "upload"
    elif _file_settings.get("edit_mode") and _file_settings["edit_mode"] != "full":
        os.environ["MCP_EDIT_MODE"] = _file_settings["edit_mode"]
    else:
        os.environ.pop("MCP_EDIT_MODE", None)

    # MCP Bearer Token: CLI > settings file > off
    if args.mcp_token:
        os.environ["MCP_BEARER_TOKEN"] = args.mcp_token
    elif _file_settings.get("mcp_bearer_token"):
        os.environ["MCP_BEARER_TOKEN"] = _file_settings["mcp_bearer_token"]
    else:
        os.environ.pop("MCP_BEARER_TOKEN", None)

    if args.no_token_edit:
        os.environ["MCP_NO_TOKEN_EDIT"] = "1"
    else:
        os.environ.pop("MCP_NO_TOKEN_EDIT", None)

    from app.auth import configure_auth
    auth_active = configure_auth()

    if args.host not in ("127.0.0.1", "localhost", "::1") and not auth_active:
        print(
            "\n⚠  WARNING: Binding to a public/network interface without a password.\n"
            "   Set MCP_MANAGER_PASSWORD=<secret> to enable authentication.\n"
        )

    edit_label = {"readonly": "readonly", "upload": "upload-only"}.get(
        os.environ.get("MCP_EDIT_MODE", ""), "full"
    )
    mcp_token = os.environ.get("MCP_BEARER_TOKEN")
    mcp_label = "bearer-token" if mcp_token else "open"
    if mcp_token and args.no_token_edit:
        mcp_label += ", locked"
    print(f"\nMCP Manager starting on http://{args.host}:{args.port}"
          f"  [auth: {'enabled' if auth_active else 'disabled'}, edit: {edit_label}, mcp-auth: {mcp_label}]\n")

    uvicorn.run(
        "app.admin_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
