"""
Tool editor backend: validation and OpenWebUI-compatible JSON export.
"""
import ast
import inspect
import time
from typing import Any


STARTER_TEMPLATE = '''\
"""
title: My Tool
description: A short description of what this tool does
author:
version: 0.1.0
"""
import requests
from pydantic import BaseModel, Field
from typing import Optional


class Tools:
    class Valves(BaseModel):
        api_url: str = Field(default="https://api.example.com", description="Base API URL")
        api_key: str = Field(default="", description="API key for authentication")

    def __init__(self):
        self.valves = self.Valves()

    def get_data(self, query: str) -> str:
        """Fetch data based on a query.

        Args:
            query: The search term to look up

        Returns:
            Result as a formatted string
        """
        # TODO: implement your tool logic here
        return f"Result for: {query}"
'''


def validate_tool_code(code: str) -> dict:
    """Validate Python tool code for OpenWebUI compatibility.

    Returns a dict with: valid, errors, warnings, tools (specs), valves.
    """
    errors: list[str] = []
    warnings: list[str] = []
    tools: list[dict] = []
    valves: dict = {}

    # 1. Syntax check — safe, no execution
    try:
        ast.parse(code)
    except SyntaxError as e:
        errors.append(f"SyntaxError line {e.lineno}: {e.msg}")
        return {"valid": False, "errors": errors, "warnings": warnings, "tools": [], "valves": {}}

    # 2. Runtime check + class introspection
    try:
        ns: dict = {}
        exec(code, ns)  # noqa: S102
    except Exception as e:
        errors.append(f"Runtime error: {type(e).__name__}: {e}")
        return {"valid": False, "errors": errors, "warnings": warnings, "tools": [], "valves": {}}

    ToolsClass = ns.get("Tools")
    if ToolsClass is None:
        errors.append("No 'Tools' class found — OpenWebUI requires a class named 'Tools'")
        return {"valid": False, "errors": errors, "warnings": warnings, "tools": [], "valves": {}}

    # 3. Valves defaults
    try:
        if hasattr(ToolsClass, "Valves"):
            valves = ToolsClass.Valves().model_dump()
    except Exception as e:
        warnings.append(f"Could not instantiate Valves: {e}")

    # 4. Method introspection → specs
    excluded = {"__init__", "valves", "user_valves"}
    for name, method in inspect.getmembers(ToolsClass, predicate=inspect.isfunction):
        if name.startswith("_") or name in excluded:
            continue

        sig = inspect.signature(method)
        doc = inspect.getdoc(method) or ""
        first_line = doc.split("\n")[0].strip() if doc else ""

        if not doc:
            warnings.append(f"'{name}': no docstring — description will be empty in OpenWebUI")

        properties: dict = {}
        required: list[str] = []

        for param_name, param in sig.parameters.items():
            if param_name in ("self", "cls"):
                continue

            if param.annotation is inspect.Parameter.empty:
                warnings.append(f"'{name}.{param_name}': no type hint, defaulting to string")

            prop = _annotation_to_schema(param.annotation)
            desc = _extract_param_doc(doc, param_name)
            if desc:
                prop["description"] = desc

            if param.default is inspect.Parameter.empty:
                required.append(param_name)
            elif param.default is not None:
                prop["default"] = param.default

            properties[param_name] = prop

        spec: dict = {
            "name": name,
            "description": first_line,
            "parameters": {"type": "object", "properties": properties},
        }
        if required:
            spec["parameters"]["required"] = required

        tools.append(spec)

    if not tools:
        errors.append("Tools class has no public methods — at least one tool function is required")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "tools": tools,
        "valves": valves,
    }


def generate_openwebui_json(code: str, tool_id: str, name: str, description: str) -> list[dict]:
    """Generate an OpenWebUI-compatible tool export JSON array.

    Raises ValueError if the code is invalid so callers can return a proper error.
    """
    result = validate_tool_code(code)
    if not result["valid"]:
        raise ValueError(f"Invalid tool code: {'; '.join(result['errors'])}")
    now = int(time.time())
    return [{
        "id": tool_id,
        "user_id": "",
        "name": name,
        "content": code,
        "specs": result["tools"],
        "meta": {
            "description": description,
            "manifest": {},
        },
        "is_active": True,
        "is_global": False,
        "updated_at": now,
        "created_at": now,
    }]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _annotation_to_schema(annotation: Any) -> dict:
    import typing
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    # Optional[X] / Union[X, None]
    if origin is typing.Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _annotation_to_schema(non_none[0])
        return {"type": "string"}
    if origin in (list,):
        return {"type": "array"}
    if origin in (dict,):
        return {"type": "object"}
    return {
        str:   {"type": "string"},
        int:   {"type": "integer"},
        float: {"type": "number"},
        bool:  {"type": "boolean"},
        list:  {"type": "array"},
        dict:  {"type": "object"},
    }.get(annotation, {"type": "string"})


def _extract_param_doc(docstring: str, param_name: str) -> str:
    """Extract parameter description from Google-style docstring Args section."""
    in_args = False
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped.lower() in ("args:", "arguments:", "parameters:"):
            in_args = True
            continue
        if in_args:
            if stripped and not line.startswith(" ") and not line.startswith("\t"):
                break  # left the Args section
            if stripped.startswith(f"{param_name}:"):
                return stripped.split(":", 1)[-1].strip()
            if stripped.startswith(f"{param_name} ("):
                part = stripped.split(")", 1)
                return part[1].lstrip(": ").strip() if len(part) > 1 else ""
    return ""
