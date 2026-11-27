from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import Unavailable


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    cosmos_endpoint: str = ""
    cosmos_database: str = "incident-memory"
    cosmos_auth_mode: Literal["entra", "key"] = "entra"
    cosmos_key: SecretStr = Field(default=SecretStr(""), repr=False)
    cosmos_operations_container: str = "operations"
    cosmos_evidence_container: str = "evidence"
    cosmos_memory_container: str = "agent-memory"
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    scope_id: str = "demo-payments-development"
    service_id: str = "payments"
    allow_azure_use: bool = False
    allow_demo_reset: bool = False
    foundry_endpoint: str = ""
    reasoning_deployment: str = ""
    memory_deployment: str = ""
    memory_embedding_deployment: str = ""
    blob_account_url: str = ""
    blob_container: str = ""
    embedding_endpoint: str = ""
    embedding_model: str = "microsoft-embedding-1-large"
    embedding_dimensions: int = Field(default=1536, ge=1, le=4096)
    agent_max_steps: int = Field(default=12, ge=1, le=100)
    agent_max_tool_calls: int = Field(default=15, ge=1, le=100)
    agent_timeout_seconds: int = Field(default=120, ge=1, le=600)
    agent_max_tokens: int = Field(default=4096, ge=1, le=32768)
    evidence_top_k: int = Field(default=5, ge=1, le=50)
    memory_top_k: int = Field(default=5, ge=1, le=50)
    approval_expiration_seconds: int = Field(default=300, ge=1, le=3600)
    monitor_interval_seconds: int = Field(default=10, ge=1, le=3600)
    operator_tenant_id: str = ""
    operator_audience: str = ""
    operator_memberships_json: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    local_operator_id: str = "engineer-a"
    local_admin_ids: list[str] = Field(default_factory=lambda: ["engineer-a"])
    trusted_auth_proxy: bool = False

    @model_validator(mode="after")
    def validate_explicit_key(self) -> "Settings":
        if self.cosmos_auth_mode == "key" and not self.cosmos_key.get_secret_value():
            raise ValueError("Explicit key authentication requires COSMOS_KEY.")
        return self

    def require_live(self, *required: str) -> None:
        """Explicit opt-in and configuration gate, without contacting Azure."""
        if not self.allow_azure_use:
            raise Unavailable("Azure use is disabled; explicit opt-in is required.")
        names = required or ("cosmos_endpoint", "cosmos_database")
        if any(not getattr(self, name, None) for name in names):
            raise Unavailable("Required Azure configuration is incomplete.")
