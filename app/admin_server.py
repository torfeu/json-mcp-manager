import json
import os
import sys
import threading

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config_store import (
    BASE_DIR,
    config_exists,
    delete_config,
    find_free_port,
    get_all_states,
    get_instance_state,
    is_port_free,
    load_all_configs,
    load_config,
    resolve_tool_path,
    save_config,
    set_instance_state,
)
from .dependency_manager import install_dependencies
from .logger import get_install_log_path, get_manager_logger, get_runtime_log_path
from .process_manager import restart_instance, start_instance, stop_instance, sync_state_from_pids
from .schema import MCPConfig, MCPInstance, MCPStatus, ServerConfig, InstallConfig, ToolSourceConfig
from .auth import require_auth, auth_enabled, edit_mode, mcp_bearer_token, token_edit_enabled
from .security import mask_secrets
from .tool_editor import validate_tool_code, generate_openwebui_json, STARTER_TEMPLATE
from .tool_loader import load_openwebui_json

logger = get_manager_logger()

app = FastAPI(title="MCP Manager", version="0.1.0")


def require_upload_or_edit() -> None:
    """Raises 403 in readonly mode (--no-edit). Upload, config edit and delete are blocked."""
    if edit_mode() == "readonly":
        raise HTTPException(403, "Disabled: server is running in read-only mode (--no-edit)")


def require_code_edit() -> None:
    """Raises 403 in upload mode and readonly mode (--no-code-edit / --no-edit)."""
    if edit_mode() in ("upload", "readonly"):
        raise HTTPException(403, "Disabled: code editing is turned off on this server")
TOOLS_DIR = BASE_DIR / "tools"
TOOLS_DIR.mkdir(exist_ok=True)


# ── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    import asyncio
    sync_state_from_pids()
    logger.info("MCP Manager started")

    # Auto-start instances with lifecycle.auto_start = True that aren't already running
    for cfg in load_all_configs().values():
        if not cfg.lifecycle.auto_start:
            continue
        inst = get_instance_state(cfg.id)
        if inst and inst.status == MCPStatus.running:
            continue
        if not is_port_free(cfg.server.port, exclude_id=cfg.id):
            new_port = find_free_port(cfg.server.port + 1)
            logger.warning(
                f"Auto-start: port {cfg.server.port} busy for '{cfg.id}', reassigning to {new_port}"
            )
            cfg.server.host = cfg.server.host  # keep host
            cfg.server = cfg.server.model_copy(update={"port": new_port})
            save_config(cfg)
            if inst:
                inst.port = new_port
                inst.url = f"http://{inst.host}:{new_port}{inst.endpoint}"
                set_instance_state(inst)
        logger.info(f"Auto-starting '{cfg.id}'")
        await asyncio.to_thread(start_instance, cfg.id)


# ── API ───────────────────────────────────────────────────────────────────────

@app.get("/api/auth-status")
async def auth_status() -> dict:
    return {"auth_enabled": auth_enabled(), "edit_mode": edit_mode()}


@app.get("/api/auth-check", dependencies=[Depends(require_auth)])
async def auth_check() -> dict:
    return {"ok": True}


@app.get("/api/instances")
async def list_instances(request: Request) -> list[dict]:
    display_host = _request_host(request)
    states = get_all_states()
    return [_instance_to_dict(s, display_host) for s in states]


@app.get("/api/instances/{instance_id}")
async def get_instance(instance_id: str, request: Request) -> dict:
    inst = get_instance_state(instance_id)
    if not inst:
        raise HTTPException(404, f"Instance '{instance_id}' not found")
    return _instance_to_dict(inst, _request_host(request))


@app.get("/api/instances/{instance_id}/config", dependencies=[Depends(require_auth)])
async def get_config(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")
    d = cfg.model_dump()
    d["values"] = mask_secrets(d.get("values", {}))
    return d


@app.post("/api/instances/upload", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def upload_json(file: UploadFile = File(...)) -> dict:
    content = await file.read()
    try:
        raw = json.loads(content)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid JSON: {e}")

    # Detect: OpenWebUI export (array or has 'content'+'specs') vs MCP config
    if isinstance(raw, list):
        if not raw:
            raise HTTPException(400, "Uploaded JSON array is empty")
        raw = raw[0]

    if "content" in raw and "specs" in raw:
        return await _import_openwebui_tool(raw)
    else:
        return await _import_mcp_config(raw)


@app.put("/api/instances/{instance_id}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def update_config(instance_id: str, body: dict) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, f"Config '{instance_id}' not found")

    old_server = (cfg.server.host, cfg.server.port, cfg.server.endpoint)

    # Only allow updating safe fields
    if "name" in body:
        cfg.name = body["name"]
    if "description" in body:
        cfg.description = body["description"]
    if "enabled" in body:
        cfg.enabled = body["enabled"]
    if "server" in body:
        s = body["server"]
        new_port = s.get("port", cfg.server.port)
        try:
            new_server = ServerConfig(
                host=s.get("host", cfg.server.host),
                port=new_port,
                endpoint=s.get("endpoint", cfg.server.endpoint),
            )
        except Exception as e:
            raise HTTPException(422, f"Invalid server config: {e}")
        if new_server.port != cfg.server.port and not is_port_free(new_server.port, exclude_id=instance_id):
            raise HTTPException(409, f"Port {new_server.port} is already in use")
        cfg.server = new_server
    if "values" in body:
        cfg.values.update(body["values"])
    if "install" in body:
        i = body["install"]
        cfg.install = InstallConfig(
            dependencies=i.get("dependencies", cfg.install.dependencies),
            requirements_file=i.get("requirements_file", cfg.install.requirements_file),
            install_on_upload=i.get("install_on_upload", cfg.install.install_on_upload),
            upgrade=i.get("upgrade", cfg.install.upgrade),
        )
    if "lifecycle" in body:
        lc = body["lifecycle"]
        cfg.lifecycle.auto_start = lc.get("auto_start", cfg.lifecycle.auto_start)
        cfg.lifecycle.restart_on_change = lc.get("restart_on_change", cfg.lifecycle.restart_on_change)

    save_config(cfg)

    inst = get_instance_state(instance_id)
    server_changed = (cfg.server.host, cfg.server.port, cfg.server.endpoint) != old_server

    if inst:
        inst.name = cfg.name
        if server_changed and inst.status == MCPStatus.running:
            if cfg.lifecycle.restart_on_change:
                # Restart so the subprocess actually binds to the new address
                logger.info(f"Server config changed for '{instance_id}', restarting")
                restart_instance(instance_id)
            else:
                # Keep UI pointing at what is actually running until user restarts manually
                pass
        else:
            # Not running or no server change: safe to update displayed URL now
            inst.port = cfg.server.port
            inst.host = cfg.server.host
            inst.endpoint = cfg.server.endpoint
            inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
            set_instance_state(inst)

    restarted = server_changed and inst and inst.status == MCPStatus.running and cfg.lifecycle.restart_on_change
    return {"ok": True, "restarted": restarted}


@app.post("/api/instances/{instance_id}/start", dependencies=[Depends(require_auth)])
async def start(instance_id: str) -> dict:
    if not config_exists(instance_id):
        raise HTTPException(404, "Config not found")
    cfg = load_config(instance_id)
    if not is_port_free(cfg.server.port, exclude_id=instance_id):
        new_port = find_free_port(cfg.server.port + 1)
        logger.warning(f"Port {cfg.server.port} busy for '{instance_id}', reassigning to {new_port}")
        cfg.server = cfg.server.model_copy(update={"port": new_port})
        save_config(cfg)
        inst = get_instance_state(instance_id)
        if inst:
            inst.port = new_port
            inst.url = f"http://{inst.host}:{new_port}{inst.endpoint}"
            set_instance_state(inst)
    ok, err = start_instance(instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}


@app.post("/api/instances/{instance_id}/stop", dependencies=[Depends(require_auth)])
async def stop(instance_id: str) -> dict:
    ok, err = stop_instance(instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}


@app.post("/api/instances/{instance_id}/restart", dependencies=[Depends(require_auth)])
async def restart(instance_id: str) -> dict:
    ok, err = restart_instance(instance_id)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}


@app.get("/api/instances/{instance_id}/tool-code", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def get_tool_code(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    tool_path = resolve_tool_path(cfg)
    tool = load_openwebui_json(tool_path)
    if not tool:
        raise HTTPException(404, "Tool source not found or unreadable")
    return {
        "id": cfg.id,
        "name": cfg.name,
        "description": cfg.description,
        "code": tool.content,
    }


@app.put("/api/instances/{instance_id}/tool-code", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def save_tool_code(instance_id: str, body: dict) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    code = body.get("code", "")
    if not code.strip():
        raise HTTPException(400, "No code provided")

    # Validate before saving
    from .tool_editor import validate_tool_code, generate_openwebui_json
    result = validate_tool_code(code)
    if not result["valid"]:
        raise HTTPException(422, {"errors": result["errors"]})

    # Update the tool JSON file (preserve id/name/description from config)
    tool_path = resolve_tool_path(cfg)
    import json as _json
    updated = generate_openwebui_json(code, cfg.id, cfg.name, cfg.description)
    tool_path.write_text(_json.dumps(updated, indent=2, ensure_ascii=False))

    # Restart if running and restart_on_change
    inst = get_instance_state(instance_id)
    restarted = False
    if inst and inst.status == MCPStatus.running and cfg.lifecycle.restart_on_change:
        restart_instance(instance_id)
        restarted = True

    return {"ok": True, "restarted": restarted, "warnings": result["warnings"]}


@app.post("/api/instances/{instance_id}/reinstall", dependencies=[Depends(require_auth)])
async def reinstall(instance_id: str) -> dict:
    cfg = load_config(instance_id)
    if not cfg:
        raise HTTPException(404, "Config not found")
    inst = get_instance_state(instance_id)
    was_running = inst and inst.status == MCPStatus.running
    if inst:
        inst.status = MCPStatus.installing
        set_instance_state(inst)
    ok, err = install_dependencies(instance_id, cfg.install.dependencies, cfg.install.upgrade)
    if inst:
        if was_running:
            inst.status = MCPStatus.running  # process is still running — restore status
        else:
            inst.status = MCPStatus.installed if ok else MCPStatus.dependency_error
        inst.error = err
        set_instance_state(inst)
    if not ok:
        raise HTTPException(500, err)
    return {"ok": True}


@app.get("/api/instances/{instance_id}/logs/install", dependencies=[Depends(require_auth)])
async def logs_install(instance_id: str) -> PlainTextResponse:
    path = get_install_log_path(instance_id)
    text = path.read_text() if path.exists() else "(no install log)"
    return PlainTextResponse(text)


@app.get("/api/instances/{instance_id}/logs/runtime", dependencies=[Depends(require_auth)])
async def logs_runtime(instance_id: str) -> PlainTextResponse:
    path = get_runtime_log_path(instance_id)
    text = path.read_text() if path.exists() else "(no runtime log)"
    return PlainTextResponse(text)


@app.get("/api/tools/template")
async def get_tool_template() -> PlainTextResponse:
    return PlainTextResponse(STARTER_TEMPLATE)


@app.post("/api/tools/validate", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def tool_validate(body: dict) -> dict:
    code = body.get("code", "")
    if not code.strip():
        raise HTTPException(400, "No code provided")
    return validate_tool_code(code)


@app.post("/api/tools/export", dependencies=[Depends(require_auth), Depends(require_code_edit)])
async def tool_export(body: dict) -> JSONResponse:
    code = body.get("code", "")
    tool_id = body.get("id", "").strip().replace(" ", "_")
    name = body.get("name", "").strip()
    description = body.get("description", "").strip()
    if not code.strip():
        raise HTTPException(400, "No code provided")
    if not tool_id:
        raise HTTPException(400, "id is required")
    import re as _re
    if not _re.fullmatch(r"[a-zA-Z0-9_\-]+", tool_id):
        raise HTTPException(400, "id must contain only letters, digits, underscores and hyphens")
    if not name:
        raise HTTPException(400, "name is required")
    try:
        result = generate_openwebui_json(code, tool_id, name, description)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return JSONResponse(
        content=result,
        headers={"Content-Disposition": f'attachment; filename="{tool_id}.json"'},
    )


def require_token_edit() -> None:
    """Raises 403 when --no-token-edit was passed at startup."""
    if not token_edit_enabled():
        raise HTTPException(403, "MCP token editing is disabled on this server (--no-token-edit)")


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings() -> dict:
    return {
        "auth_enabled": auth_enabled(),
        "edit_mode": edit_mode(),
        "host": os.environ.get("MCP_RUNNER_HOST", "127.0.0.1"),
        "port": int(os.environ.get("MCP_MANAGER_PORT", "7860")),
        "mcp_token_set": mcp_bearer_token() is not None,
        "token_edit_enabled": token_edit_enabled(),
    }


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def update_settings(body: dict) -> dict:
    from .auth import set_password, set_edit_mode_setting, set_mcp_bearer_token, verify_password
    changed = []

    pw = body.get("password", "").strip()
    if pw:
        if len(pw) < 4:
            raise HTTPException(400, "Password must be at least 4 characters")
        confirm = body.get("password_confirm", "").strip()
        if pw != confirm:
            raise HTTPException(400, "Passwords do not match")
        if auth_enabled():
            current = body.get("current_password", "").strip()
            if not current:
                raise HTTPException(400, "Current password required")
            if not verify_password(current):
                raise HTTPException(401, "Current password is wrong")
        set_password(pw)
        changed.append("password")

    if "edit_mode" in body:
        mode = body["edit_mode"]
        if mode not in ("full", "upload", "readonly"):
            raise HTTPException(400, f"Invalid edit_mode: {mode}")
        set_edit_mode_setting(mode)
        changed.append("edit_mode")

    if "mcp_token" in body or body.get("mcp_token_clear"):
        require_token_edit()
        if body.get("mcp_token_clear"):
            set_mcp_bearer_token(None)
            changed.append("mcp_token_cleared")
        else:
            token = body.get("mcp_token", "").strip()
            if len(token) < 8:
                raise HTTPException(400, "MCP token must be at least 8 characters")
            set_mcp_bearer_token(token)
            changed.append("mcp_token")

    return {"ok": True, "changed": changed}


@app.get("/api/settings/mcp-token", dependencies=[Depends(require_auth), Depends(require_token_edit)])
async def get_mcp_token_value() -> dict:
    return {"token": mcp_bearer_token() or ""}


def _restart_after_delay() -> None:
    import time
    time.sleep(0.8)
    os.execv(sys.executable, [sys.executable] + sys.argv)


@app.post("/api/server/restart", dependencies=[Depends(require_auth)])
async def restart_server_endpoint() -> dict:
    threading.Thread(target=_restart_after_delay, daemon=True).start()
    return {"ok": True}


@app.delete("/api/instances/{instance_id}", dependencies=[Depends(require_auth), Depends(require_upload_or_edit)])
async def delete_instance(instance_id: str) -> dict:
    inst = get_instance_state(instance_id)
    if inst and inst.status == MCPStatus.running:
        stop_instance(instance_id)
    if not delete_config(instance_id):
        raise HTTPException(404, "Config not found")
    return {"ok": True}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _import_openwebui_tool(raw: dict) -> dict:
    import re as _re
    tool_id = raw.get("id", "").strip().replace(" ", "_")
    if not tool_id or not _re.fullmatch(r"[a-zA-Z0-9_\-]+", tool_id):
        raise HTTPException(400, f"Invalid tool ID '{tool_id}': only letters, digits, underscores and hyphens allowed")
    tool_name = raw.get("name", tool_id)

    if config_exists(tool_id):
        raise HTTPException(409, f"ID '{tool_id}' already exists")

    # Save tool JSON into tools/  (tool_id is validated above — safe for use as filename)
    tool_file = TOOLS_DIR / f"{tool_id}.json"
    tool_file.write_text(json.dumps([raw] if not isinstance(raw, list) else raw, indent=2))

    # Extract Valves defaults
    from .tool_loader import OpenWebUITool
    tool_obj = OpenWebUITool(raw)
    values = tool_obj.extract_valves_defaults()
    description = raw.get("meta", {}).get("description", "")

    port = find_free_port()

    cfg = MCPConfig(
        id=tool_id,
        name=tool_name,
        description=description,
        enabled=True,
        server=ServerConfig(host="127.0.0.1", port=port, endpoint="/mcp"),
        tool_source=ToolSourceConfig(type="openwebui_json", path=f"./tools/{tool_id}.json"),
        values=values,
    )

    inst = MCPInstance(
        id=cfg.id,
        name=cfg.name,
        description=cfg.description,
        status=MCPStatus.installing,
        port=port,
        host="127.0.0.1",
        endpoint="/mcp",
    )
    set_instance_state(inst)

    ok, err = install_dependencies(tool_id, cfg.install.dependencies, cfg.install.upgrade)
    inst.status = MCPStatus.installed if ok else MCPStatus.dependency_error
    inst.error = err
    set_instance_state(inst)

    save_config(cfg)
    logger.info(f"Imported OpenWebUI tool: {tool_id}")
    return {"ok": True, "id": tool_id, "port": port}


async def _import_mcp_config(raw: dict) -> dict:
    try:
        cfg = MCPConfig.model_validate(raw)
    except Exception as e:
        raise HTTPException(400, f"Invalid MCP config: {e}")

    if config_exists(cfg.id):
        raise HTTPException(409, f"ID '{cfg.id}' already exists")

    if not is_port_free(cfg.server.port):
        raise HTTPException(409, f"Port {cfg.server.port} is already in use")

    tool_path = resolve_tool_path(cfg)
    # Reject paths that escape the project directory
    try:
        tool_path.resolve().relative_to(BASE_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "tool_source.path must be inside the project directory")
    if not tool_path.exists():
        raise HTTPException(400, f"Tool source not found: {tool_path}")

    inst = MCPInstance(
        id=cfg.id,
        name=cfg.name,
        description=cfg.description,
        status=MCPStatus.installing,
        port=cfg.server.port,
        host=cfg.server.host,
        endpoint=cfg.server.endpoint,
    )
    set_instance_state(inst)

    ok, err = install_dependencies(cfg.id, cfg.install.dependencies, cfg.install.upgrade)
    inst.status = MCPStatus.installed if ok else MCPStatus.dependency_error
    inst.error = err
    set_instance_state(inst)

    save_config(cfg)
    return {"ok": True, "id": cfg.id, "port": cfg.server.port}


def _request_host(request: Request) -> str | None:
    """Extract just the hostname from the request Host header (no port).
    Handles IPv6 bracket notation: [::1]:7860 → ::1
    """
    host_header = request.headers.get("host", "")
    if host_header.startswith("["):
        # IPv6: [::1]:7860 or [::1]
        end = host_header.find("]")
        hostname = host_header[1:end] if end != -1 else host_header[1:]
    else:
        hostname = host_header.split(":")[0]
    return hostname if hostname else None


def _instance_to_dict(inst: MCPInstance, display_host: str | None = None) -> dict:
    host = inst.host
    # If binding on all interfaces and caller knows the real IP, show that
    if display_host and host in ("0.0.0.0", "127.0.0.1", "::1", "localhost"):
        host = display_host
    host_in_url = f"[{host}]" if ":" in host else host  # bracket IPv6 addresses
    url = f"http://{host_in_url}:{inst.port}{inst.endpoint}"
    return {
        "id": inst.id,
        "name": inst.name,
        "description": inst.description,
        "status": inst.status.value,
        "port": inst.port,
        "host": host,
        "endpoint": inst.endpoint,
        "url": url,
        "pid": inst.pid,
        "error": inst.error,
    }


# ── Static files (must be last) ───────────────────────────────────────────────

WEB_DIR = BASE_DIR / "web"

@app.get("/")
async def root() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")

app.mount("/", StaticFiles(directory=str(WEB_DIR)), name="static")
