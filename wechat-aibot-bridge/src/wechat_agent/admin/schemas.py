"""Validated public request models for the admin API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SetupRequest(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=256)
    display_name: str = Field(default="", max_length=100)


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    mode: Literal["cookie", "token"] = "cookie"


class ConnectionCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    channel_type: Literal["WECOM_AIBOT"] = "WECOM_AIBOT"
    bot_id: str = Field(default="", max_length=256)
    secret: str = Field(default="", max_length=4096)
    environment: str = Field(default="local", max_length=50)
    notes: str = Field(default="", max_length=2000)


class ConnectionUpdate(StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    bot_id: str | None = Field(default=None, max_length=256)
    secret: str | None = Field(default=None, max_length=4096)
    environment: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    status: Literal["DRAFT", "READY", "DISABLED"] | None = None


class UserUpdate(StrictModel):
    display_name: str | None = Field(default=None, max_length=100)
    status: Literal["PENDING", "ALLOWED", "DISABLED", "OBSERVE"] | None = None
    policy: dict[str, Any] | None = None

    @field_validator("policy")
    @classmethod
    def limit_policy(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and len(str(value)) > 32_000:
            raise ValueError("Policy is too large")
        return value


class ControlRequest(StrictModel):
    reason: str = Field(default="", max_length=1000)
    fresh_session: bool = True


class SettingsUpdate(StrictModel):
    log_retention_days: int | None = Field(default=None, ge=1, le=3650)
    message_retention_days: int | None = Field(default=None, ge=1, le=3650)


class ConfigProfileCreate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)


class ConfigProfileUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)


class ConfigRevisionCreate(StrictModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1, max_length=200_000)
    request_timeout_seconds: float = Field(default=450, gt=0, le=7200)
    task_timeout_seconds: float = Field(default=480, gt=0, lt=590)
    tool_policy: dict[str, Any] = Field(default_factory=dict)


class AlertAction(StrictModel):
    note: str = Field(default="", max_length=2000)


class AdminUserCreate(StrictModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9_.@-]+$")
    display_name: str = Field(default="", max_length=100)
    password: str = Field(min_length=12, max_length=256)
    roles: list[str] = Field(default_factory=lambda: ["viewer"], min_length=1, max_length=10)


class RoleAssignment(StrictModel):
    roles: list[str] = Field(min_length=1, max_length=10)


class RetentionRequest(StrictModel):
    event_days: int = Field(default=90, ge=1, le=3650)
    log_days: int = Field(default=30, ge=1, le=3650)
    session_days: int = Field(default=30, ge=1, le=3650)
    audit_days: int = Field(default=365, ge=30, le=3650)
    dry_run: bool = True
