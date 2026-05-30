import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .logger import get_manager_logger, get_runtime_log_path
from .schema import MCPInstance, MCPStatus
from .config_store import get_instance_state, set_instance_state, load_config

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
PIDS_FILE = BASE_DIR / "runtime" / "pids.json"
RUNNER_SCRIPT = Path(__file__).parent / "mcp_runner.py"


def _load_pids() -> dict:
    if PIDS_FILE.exists():
        try:
            return json.loads(PIDS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_pids(pids: dict) -> None:
    PIDS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PIDS_FILE.write_text(json.dumps(pids, indent=2))


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _pid_is_our_runner(pid: int) -> bool:
    """Return True only if the PID belongs to our mcp_runner subprocess.

    Checks /proc on Linux, falls back to `ps` on macOS/other Unix.
    Returns False (safe) when the PID is confirmed to belong to something else,
    or when the process is not found. Returns True only on positive confirmation
    or when the check mechanism itself is completely unavailable.
    """
    # Linux: read /proc directly
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            cmdline = proc_path.read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            return "mcp_runner" in cmdline
        except Exception:
            return False

    # macOS / other Unix: ask ps
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False  # PID not found → not our process
        return "mcp_runner" in result.stdout
    except Exception:
        pass

    # Last resort: we have no way to verify — log and refuse to kill
    logger.warning(f"Cannot verify PID {pid} ownership — skipping kill for safety")
    return False


def sync_state_from_pids() -> None:
    """Reconcile in-memory state with pids.json on startup.

    Port/host/url always come from the current config file — never from the
    (potentially stale) pids.json — to avoid showing wrong URLs after a
    config change.
    """
    pids = _load_pids()
    for instance_id, info in pids.items():
        pid = info.get("pid")
        inst = get_instance_state(instance_id)
        if not inst:
            continue
        # Always sync connection details from the live config
        cfg = load_config(instance_id)
        if cfg:
            inst.port = cfg.server.port
            inst.host = cfg.server.host
            inst.endpoint = cfg.server.endpoint
            inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
        if pid and _is_pid_alive(pid) and _pid_is_our_runner(pid):
            inst.status = MCPStatus.running
            inst.pid = pid
        else:
            inst.status = MCPStatus.stopped
            inst.pid = None
        set_instance_state(inst)

    # Rewrite pids.json: remove dead/foreign entries, update ports from live config
    cleaned = {}
    for k, v in pids.items():
        pid = v.get("pid")
        if not (pid and _is_pid_alive(pid) and _pid_is_our_runner(pid)):
            continue
        cfg = load_config(k)
        if cfg:
            v["port"] = cfg.server.port
        cleaned[k] = v
    _save_pids(cleaned)


def start_instance(instance_id: str) -> tuple[bool, str]:
    cfg = load_config(instance_id)
    if not cfg:
        return False, "Config not found"

    inst = get_instance_state(instance_id)
    if inst and inst.status == MCPStatus.running:
        pid = inst.pid
        if pid and _is_pid_alive(pid) and _pid_is_our_runner(pid):
            return False, "Already running"

    # Sync instance state with fresh config (port/host may have changed)
    inst.port = cfg.server.port
    inst.host = cfg.server.host
    inst.endpoint = cfg.server.endpoint
    inst.url = f"http://{cfg.server.host}:{cfg.server.port}{cfg.server.endpoint}"
    inst.status = MCPStatus.starting
    inst.error = ""
    set_instance_state(inst)

    config_path = BASE_DIR / "configs" / f"{instance_id}.json"
    log_path = get_runtime_log_path(instance_id)
    log_file = open(log_path, "a")

    try:
        cmd = [sys.executable, str(RUNNER_SCRIPT), "--config", str(config_path)]
        runner_host = os.environ.get("MCP_RUNNER_HOST")
        if runner_host:
            cmd += ["--host", runner_host]

        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
            cwd=str(BASE_DIR),
        )
    except Exception as e:
        inst.status = MCPStatus.failed
        inst.error = str(e)
        set_instance_state(inst)
        return False, str(e)

    time.sleep(1.5)

    if proc.poll() is not None:
        inst.status = MCPStatus.failed
        inst.error = "Process exited immediately"
        set_instance_state(inst)
        pids = _load_pids()
        pids.pop(instance_id, None)
        _save_pids(pids)
        return False, inst.error

    inst.status = MCPStatus.running
    inst.pid = proc.pid
    set_instance_state(inst)

    pids = _load_pids()
    pids[instance_id] = {"pid": proc.pid, "status": "running", "port": cfg.server.port}
    _save_pids(pids)

    logger.info(f"Started {instance_id} (pid={proc.pid}, port={cfg.server.port})")
    return True, ""


def stop_instance(instance_id: str) -> tuple[bool, str]:
    inst = get_instance_state(instance_id)
    if not inst:
        return False, "Instance not found"

    pids = _load_pids()
    pid = inst.pid or (pids.get(instance_id, {}).get("pid"))

    inst.status = MCPStatus.stopping
    set_instance_state(inst)

    if pid and _is_pid_alive(pid):
        if not _pid_is_our_runner(pid):
            logger.warning(f"PID {pid} for '{instance_id}' is not our runner — skipping kill")
        else:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(10):
                    time.sleep(0.5)
                    if not _is_pid_alive(pid):
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except Exception as e:
                logger.warning(f"Error killing {instance_id} (pid={pid}): {e}")

    inst.status = MCPStatus.stopped
    inst.pid = None
    set_instance_state(inst)

    pids.pop(instance_id, None)
    _save_pids(pids)

    logger.info(f"Stopped {instance_id}")
    return True, ""


def restart_instance(instance_id: str) -> tuple[bool, str]:
    stop_instance(instance_id)
    time.sleep(0.5)
    return start_instance(instance_id)


def get_pid(instance_id: str) -> Optional[int]:
    pids = _load_pids()
    return pids.get(instance_id, {}).get("pid")
