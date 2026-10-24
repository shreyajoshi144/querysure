"""Pydantic request bodies for endpoints that historically parsed JSON by hand."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from auth import _validate_password, normalize_email


class _OptionalJsonBody(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_id: str | None = None
    question: str | None = None
    sql: str | None = None
    api_key: str | None = None


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: EmailStr | None = None
    password: str | None = None

    @field_validator("email")
    @classmethod
    def normalize_email_value(cls, v):
        return normalize_email(str(v)) if v is not None else v


class FlushSessionRequest(_OptionalJsonBody):
    pass


class SampleRequest(_OptionalJsonBody):
    pass


class SchemaIntelligenceRequest(_OptionalJsonBody):
    pass


class SemanticLayerRequest(_OptionalJsonBody):
    pass


class RetrieveTablesRequest(_OptionalJsonBody):
    pass


class GenerateSqlRequest(_OptionalJsonBody):
    pass


class ValidateSqlRequest(_OptionalJsonBody):
    pass


class ExecuteSqlRequest(_OptionalJsonBody):
    pass


class ReliabilityRequest(_OptionalJsonBody):
    pass


class InsightsRequest(_OptionalJsonBody):
    pass


class ReportRequest(_OptionalJsonBody):
    pass


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password(v)
