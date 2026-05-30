import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent.parent / "runtime" / "logs"


def get_manager_logger() -> logging.Logger:
    logger = logging.getLogger("mcp_manager")
    if not logger.handlers:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        fh = logging.FileHandler(LOG_DIR / "manager.log")
        fh.setFormatter(fmt)
        logger.addHandler(sh)
        logger.addHandler(fh)
        logger.setLevel(logging.INFO)
    return logger


def get_install_log_path(instance_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{instance_id}.install.log"


def get_runtime_log_path(instance_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{instance_id}.runtime.log"
