"""Authentication request and response models."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.enums import UserRole

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]
    """No minimum on login — length rules belong on the *setting* of a password, and enforcing
    them here would tell an attacker which passwords are impossible."""


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: Annotated[str, Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)]
    new_password: Annotated[
        str, Field(min_length=MIN_PASSWORD_LENGTH, max_length=MAX_PASSWORD_LENGTH)
    ]


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 - a scheme name, not a credential
    expires_in: int
    """Seconds until the access token expires."""
    csrf_token: str
    """Mirror of the CSRF cookie, so a SPA can read it without cookie access."""
    user: UserResponse


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None = None
    role: UserRole
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    created_at: datetime


class SessionResponse(BaseModel):
    id: int
    family_id: str
    created_at: datetime
    expires_at: datetime
    ip_address: str | None = None
    user_agent: str | None = None
    current: bool = False


class MessageResponse(BaseModel):
    message: str
