import errno
import json
import socket
from pathlib import Path
from typing import Optional

from .schema import MCPConfig, MCPInstance, MCPStatus
from .logger import get_manager_logger

logger = get_manager_logger()

BASE_DIR = Path(__file__).parent.parent
CONFIGS_DIR = BASE_DIR / "configs"
CONFIGS_DIR.mkdir(exist_ok=True)

_state: dict[str, MCPInstance] = {}


def _resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return BASE_DIR / path


def load_all_configs() -> dict[str, MCPConfig]:
    configs = {}
    for f in sorted(CONFIGS_DIR.glob("*.json")):
        if f.name == "example.json":
            continue
        try:
            raw = json.loads(f.read_text())
            cfg = MCPConfig.model_validate(raw)
            configs[cfg.id] = cfg
        except Exception as e:
            logger.error(f"Failed to load config {f.name}: {e}")
    return configs


def load_config(config_id: str) -> Optional[MCPConfig]:
    # Try canonical name first, then scan all configs (never example.json)
    candidates = [CONFIGS_DIR / f"{config_id}.json"] + [
        p for p in CONFIGS_DIR.glob("*.json") if p.name != "example.json"
    ]
    for path in candidates:
        if not path.exists():
            continue
        try:
            cfg = MCPConfig.model_validate(json.loads(path.read_text()))
            if cfg.id == config_id:
                return cfg
        except Exception:
            continue
    logger.error(f"Config not found: {config_id}")
    return None


def save_config(cfg: MCPConfig) -> None:
    path = CONFIGS_DIR / f"{cfg.id}.json"
    path.write_text(json.dumps(cfg.model_dump(), indent=2))
    logger.info(f"Saved config: {cfg.id}")


def delete_config(config_id: str) -> bool:
    path = CONFIGS_DIR / f"{config_id}.json"
    if path.exists():
        path.unlink()
        _state.pop(config_id, None)
        logger.info(f"Deleted config: {config_id}")
        return True
    return False


def config_exists(config_id: str) -> bool:
    if config_id == "example":
        return False
    return (CONFIGS_DIR / f"{config_id}.json").exists()


def _os_port_free(port: int) -> bool:
    """Return True if the port is available on the OS level.

    Strategy (most to least reliable):
    1. Linux: read /proc/net/tcp6 + /proc/net/tcp — no bind, no permissions needed.
    2. Fallback: try binding on 0.0.0.0 / ::; only trust EADDRINUSE as "in use",
       treat other errors (EAFNOSUPPORT, EPERM, …) as inconclusive.
    """
    # ── Linux fast path via /proc/net ──────────────────────────────────────────
    # Must check BOTH tcp and tcp6: IPv4 sockets appear in tcp, IPv6 (and
    # IPv4-mapped) appear in tcp6. We scan all available files.
    hex_port = f"{port:04X}"
    proc_files = [f for f in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")) if f.exists()]
    if proc_files:
        try:
            for proc_file in proc_files:
                for line in proc_file.read_text().splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "0A":   # 0A = TCP_LISTEN
                        continue
                    if parts[1].split(":")[1].upper() == hex_port:
                        return False    # port is listening in this file
            return True                 # not found in any LISTEN entry
        except Exception:
            pass                        # /proc unreadable → fall through to bind

    # ── Bind-based fallback (non-Linux or unreadable /proc) ───────────────────
    for family, addr in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind((addr, port))
                return True         # bind succeeded → free
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    return False    # definitely in use
                # EAFNOSUPPORT, EPERM, sandbox … → try next family
    # Could not confirm either way; assume free to avoid blocking all ports
    return True


def find_free_port(start: int = 8101) -> int:
    used = {cfg.server.port for cfg in load_all_configs().values()}
    for port in range(start, start + 200):
        if port in used:
            continue
        if _os_port_free(port):
            return port
    raise RuntimeError("No free port available")


def is_port_free(port: int, exclude_id: Optional[str] = None) -> bool:
    for cfg_id, cfg in load_all_configs().items():
        if cfg_id == exclude_id:
            continue
        if cfg.server.port == port:
            return False
    return _os_port_free(port)


def get_instance_state(config_id: str) -> MCPInstance:
    if config_id not in _state:
        cfg = load_config(config_id)
        if cfg:
            _state[config_id] = MCPInstance(
                id=cfg.id,
                name=cfg.name,
                description=cfg.description,
                status=MCPStatus.stopped,
                port=cfg.server.port,
                host=cfg.server.host,
                endpoint=cfg.server.endpoint,
            )
    return _state.get(config_id)


def set_instance_state(instance: MCPInstance) -> None:
    _state[instance.id] = instance


def get_all_states() -> list[MCPInstance]:
    configs = load_all_configs()
    result = []
    for cfg in configs.values():
        inst = get_instance_state(cfg.id)
        if inst:
            result.append(inst)
    return result


def resolve_tool_path(cfg: MCPConfig) -> Path:
    return _resolve_path(cfg.tool_source.path)
