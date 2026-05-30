import inspect
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

from .logger import get_manager_logger

logger = get_manager_logger()

_EXCLUDED = {"__init__", "valves", "user_valves"}

# OpenWebUI injected parameter names — skip from schema
_INJECTED_PARAMS = {"self", "__user__", "__event_emitter__", "__event_call__", "__request__"}


def _get_tools_class_methods(content: str) -> set[str]:
    """Return names of public methods defined directly in the Tools class."""
    try:
        ns: dict = {}
        exec(content, ns)  # noqa: S102
        ToolsClass = ns.get("Tools")
        if ToolsClass is None:
            return set()
        return {
            name
            for name, member in inspect.getmembers(ToolsClass, predicate=inspect.isfunction)
            if not name.startswith("_") and name not in _EXCLUDED
        }
    except Exception:
        return set()


def _type_to_json_schema(annotation: Any) -> dict:
    """Convert a Python type annotation to a JSON Schema fragment."""
    origin = get_origin(annotation)
    args = get_args(annotation)

    # Annotated[T, Field(...)] — unwrap, description is handled by the caller
    if origin is Annotated:
        return _type_to_json_schema(args[0])

    # Optional[X] / Union[X, None]
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _type_to_json_schema(non_none[0])
        return {"type": "string"}

    # Literal["a", "b", ...]
    if origin is Literal:
        values = list(args)
        if all(isinstance(v, str) for v in values):
            return {"type": "string", "enum": values}
        if all(isinstance(v, int) for v in values):
            return {"type": "integer", "enum": values}
        return {"enum": values}

    # List[X]
    if origin is list:
        item_schema = _type_to_json_schema(args[0]) if args else {}
        return {"type": "array", "items": item_schema}

    # Primitives
    _map = {str: "string", int: "integer", float: "number", bool: "boolean"}
    if annotation in _map:
        return {"type": _map[annotation]}

    return {"type": "string"}


def _field_description(annotation: Any) -> str:
    """Extract Field(description=...) from an Annotated type, if present."""
    if get_origin(annotation) is not Annotated:
        return ""
    for meta in get_args(annotation)[1:]:
        if hasattr(meta, "description") and isinstance(meta.description, str) and meta.description:
            return meta.description
        # plain string as second Annotated arg
        if isinstance(meta, str):
            return meta
    return ""


def _parse_docstring_params(doc: str) -> dict[str, str]:
    """Parse a Google-style 'Args:' / 'Parameters:' section from a docstring."""
    if not doc:
        return {}
    params: dict[str, str] = {}
    in_args = False
    current: str | None = None
    lines_buf: list[str] = []

    for line in doc.splitlines():
        stripped = line.strip()

        if stripped in ("Args:", "Arguments:", "Parameters:"):
            in_args = True
            continue

        if in_args:
            # New top-level section ends the Args block
            if stripped and not line.startswith("    "):
                break

            # Param line: "    param_name: description" or "    param_name (type): desc"
            if line.startswith("    ") and not line.startswith("        ") and ":" in stripped:
                if current is not None:
                    params[current] = " ".join(lines_buf).strip()
                colon = stripped.index(":")
                param_name = stripped[:colon].split("(")[0].strip()
                current = param_name
                lines_buf = [stripped[colon + 1:].strip()]
            elif line.startswith("        ") and current is not None:
                lines_buf.append(stripped)

    if current is not None:
        params[current] = " ".join(lines_buf).strip()

    return params


def _build_schema_from_method(method: Any, exec_ns: dict) -> dict:
    """
    Build a full JSON Schema for a Tools method by introspecting its
    type hints (Literal → enum, Annotated/Field → description) and defaults.
    Falls back gracefully when introspection fails.
    """
    try:
        hints = get_type_hints(method, globalns=exec_ns, localns=exec_ns, include_extras=True)
    except Exception:
        try:
            hints = get_type_hints(method, include_extras=True)
        except Exception:
            hints = {}

    try:
        sig = inspect.signature(method)
    except Exception:
        return {"type": "object", "properties": {}}

    doc_params = _parse_docstring_params(inspect.getdoc(method) or "")

    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in _INJECTED_PARAMS:
            continue

        raw_annotation = hints.get(param_name, inspect.Parameter.empty)
        fragment: dict

        if raw_annotation is inspect.Parameter.empty:
            fragment = {"type": "string"}
        else:
            fragment = _type_to_json_schema(raw_annotation)
            desc = _field_description(raw_annotation) or doc_params.get(param_name, "")
            if desc:
                fragment["description"] = desc

        if param.default is not inspect.Parameter.empty and param.default is not None:
            fragment["default"] = param.default
        elif param.default is inspect.Parameter.empty:
            required.append(param_name)

        properties[param_name] = fragment

    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


class OpenWebUITool:
    def __init__(self, raw: dict):
        self.id: str = raw.get("id", "unknown")
        self.name: str = raw.get("name", "Unknown Tool")
        self.description: str = raw.get("meta", {}).get("description", "")
        self.content: str = raw.get("content", "")
        self.specs: list[dict] = raw.get("specs", [])

    def extract_valves_defaults(self) -> dict[str, Any]:
        """Exec the tool code to extract Valves field defaults."""
        if not self.content:
            return {}
        try:
            ns: dict = {}
            exec(self.content, ns)  # noqa: S102
            ToolsClass = ns.get("Tools")
            if ToolsClass and hasattr(ToolsClass, "Valves"):
                return ToolsClass.Valves().model_dump()
        except Exception as e:
            logger.warning(f"Could not extract Valves defaults: {e}")
        return {}

    def get_mcp_tool_defs(self) -> list[dict]:
        """Return MCP tool definitions built from the live Python code."""
        # Exec once to get the real class and method objects
        exec_ns: dict = {}
        ToolsClass = None
        if self.content:
            try:
                exec(self.content, exec_ns)  # noqa: S102
                ToolsClass = exec_ns.get("Tools")
            except Exception as e:
                logger.warning(f"Could not exec tool code for schema generation: {e}")

        class_methods = _get_tools_class_methods(self.content)
        specs_by_name = {s["name"]: s for s in self.specs}

        result = []
        for name in sorted(class_methods):
            spec = specs_by_name.get(name)
            description = spec.get("description", "") if spec else ""

            input_schema: dict
            if ToolsClass is not None:
                method = getattr(ToolsClass, name, None)
                if method is not None:
                    input_schema = _build_schema_from_method(method, exec_ns)
                else:
                    input_schema = (
                        spec.get("parameters", {"type": "object", "properties": {}})
                        if spec else {"type": "object", "properties": {}}
                    )
            else:
                input_schema = (
                    spec.get("parameters", {"type": "object", "properties": {}})
                    if spec else {"type": "object", "properties": {}}
                )

            result.append({
                "name": name,
                "description": description,
                "inputSchema": input_schema,
            })
        return result


def load_openwebui_json(path: Path) -> Optional[OpenWebUITool]:
    """Load an OpenWebUI tool export JSON (array or single object)."""
    if not path.exists():
        logger.error(f"Tool file not found: {path}")
        return None
    try:
        raw = json.loads(path.read_text())
        if isinstance(raw, list):
            raw = raw[0]
        return OpenWebUITool(raw)
    except Exception as e:
        logger.error(f"Failed to load tool file {path}: {e}")
        return None


def create_tools_instance(tool: OpenWebUITool, values: dict[str, Any]) -> Any:
    """
    Exec the tool code, instantiate Tools, and inject config values into Valves.
    Returns the Tools instance or None on failure.
    """
    if not tool.content:
        return None
    try:
        ns: dict = {}
        exec(tool.content, ns)  # noqa: S102
        ToolsClass = ns.get("Tools")
        if ToolsClass is None:
            return None
        instance = ToolsClass()
        if hasattr(instance, "valves") and values:
            for k, v in values.items():
                if hasattr(instance.valves, k):
                    try:
                        setattr(instance.valves, k, v)
                    except Exception:
                        pass
        return instance
    except Exception as e:
        logger.error(f"Failed to create Tools instance: {e}")
        return None
