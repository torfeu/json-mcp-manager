"""
Persistent settings for the MCP Manager.
Stored in runtime/settings.json (gitignored).
Priority: env vars / CLI flags > this file > built-in defaults.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
SETTINGS_FILE = BASE_DIR / "runtime" / "settings.json"


def load_settings() -> dict:
    """Return the persisted settings dict (empty dict if file missing or unreadable)."""
    try:
        if SETTINGS_FILE.exists():
            return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        pass
    return {}


def save_settings(updates: dict) -> None:
    """Merge *updates* into the persisted settings file. None values remove the key."""
    current = load_settings()
    for k, v in updates.items():
        if v is None:
            current.pop(k, None)
        else:
            current[k] = v
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(current, indent=2))
