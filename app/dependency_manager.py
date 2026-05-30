import subprocess
import sys

from .logger import get_install_log_path, get_manager_logger
from .security import validate_package_spec

logger = get_manager_logger()


def _log(instance_id: str, msg: str) -> None:
    log_path = get_install_log_path(instance_id)
    with open(log_path, "a") as f:
        f.write(msg + "\n")
    logger.info(f"[{instance_id}] {msg}")


def install_dependencies(
    instance_id: str,
    dependencies: list[str],
    upgrade: bool = False,
) -> tuple[bool, str]:
    """Install dependencies. Returns (success, error_message)."""
    log_path = get_install_log_path(instance_id)
    log_path.write_text("")  # clear log

    if not dependencies:
        _log(instance_id, "No dependencies to install.")
        return True, ""

    invalid = [d for d in dependencies if not validate_package_spec(d)]
    if invalid:
        msg = f"Invalid/unsafe package specs: {invalid}"
        _log(instance_id, f"[ERROR] {msg}")
        return False, msg

    _log(instance_id, f"Installing {len(dependencies)} dependencies...")

    for dep in dependencies:
        cmd = [sys.executable, "-m", "pip", "install", dep]
        if upgrade:
            cmd.append("--upgrade")

        _log(instance_id, f"$ {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    _log(instance_id, line)
            if result.returncode != 0:
                err = result.stderr.strip()
                _log(instance_id, f"[ERROR] Failed: {err}")
                return False, f"Failed to install {dep}: {err}"
            else:
                _log(instance_id, f"[OK] {dep}")
        except subprocess.TimeoutExpired:
            msg = f"Timeout installing {dep}"
            _log(instance_id, f"[ERROR] {msg}")
            return False, msg
        except Exception as e:
            msg = f"Exception installing {dep}: {e}"
            _log(instance_id, f"[ERROR] {msg}")
            return False, msg

    _log(instance_id, "All dependencies installed successfully.")
    return True, ""
