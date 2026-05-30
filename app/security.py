import re
from packaging.requirements import Requirement, InvalidRequirement

_SAFE_PACKAGE_RE = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
_SECRET_FIELDS = {"api_key", "token", "secret", "password", "key", "auth"}


def validate_package_spec(spec: str) -> bool:
    """Return True if spec is a safe PyPI requirement string."""
    spec = spec.strip()
    if not spec or spec.startswith(("#", "-", "git+", "http")):
        return False
    try:
        req = Requirement(spec)
        return bool(_SAFE_PACKAGE_RE.match(req.name))
    except InvalidRequirement:
        return False


def mask_secrets(values: dict) -> dict:
    """Return a copy of values with secret fields masked."""
    result = {}
    for k, v in values.items():
        key_lower = k.lower()
        is_secret = any(s in key_lower for s in _SECRET_FIELDS)
        if is_secret and isinstance(v, str) and v:
            result[k] = "********"
        else:
            result[k] = v
    return result
