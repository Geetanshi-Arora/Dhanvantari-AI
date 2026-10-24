from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, field_validator


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr = Field(max_length=254)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def password_length(cls, value):
        if not 1 <= len(value.get_secret_value()) <= 1024:
            raise ValueError("Invalid credential length")
        return value


class StaffResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    display_name: str
    role: Literal["nurse", "doctor"]
