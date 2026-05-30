from pydantic import BaseModel, Field, field_validator
from typing import Optional, Any
from enum import Enum


class MCPStatus(str, Enum):
    uploaded = "uploaded"
    installing = "installing"
    installed = "installed"
    starting = "starting"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    failed = "failed"
    dependency_error = "dependency_error"
    port_error = "port_error"
    config_error = "config_error"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(ge=1024, le=65535)
    endpoint: str = "/mcp"

    @field_validator("host")
    @classmethod
    def host_must_be_local(cls, v: str) -> str:
        allowed = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
        if v not in allowed:
            raise ValueError(f"Host must be local or 0.0.0.0. Got: {v}")
        return v


class RuntimeConfig(BaseModel):
    venv_mode: str = "shared"
    python: str = "shared"


class InstallConfig(BaseModel):
    dependencies: list[str] = []
    requirements_file: Optional[str] = None
    install_on_upload: bool = True
    upgrade: bool = False


class ToolSourceConfig(BaseModel):
    type: str = "openwebui_json"
    path: str


class LifecycleConfig(BaseModel):
    auto_start: bool = False
    restart_on_change: bool = True


class MCPConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    enabled: bool = True
    server: ServerConfig
    runtime: RuntimeConfig = RuntimeConfig()
    install: InstallConfig = InstallConfig()
    tool_source: ToolSourceConfig
    values: dict[str, Any] = {}
    lifecycle: LifecycleConfig = LifecycleConfig()

    @field_validator("id")
    @classmethod
    def id_safe(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"[a-zA-Z0-9_\-]+", v):
            raise ValueError(
                "ID must contain only letters, digits, underscores and hyphens"
            )
        return v


class MCPInstance(BaseModel):
    id: str
    name: str
    description: str = ""
    status: MCPStatus = MCPStatus.stopped
    port: int
    host: str
    endpoint: str
    pid: Optional[int] = None
    url: str = ""
    error: str = ""

    def model_post_init(self, __context: Any) -> None:
        if not self.url:
            self.url = f"http://{self.host}:{self.port}{self.endpoint}"
