from typing import Literal
import base64
import binascii

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

RoleCode = Literal["SUPER_ADMIN", "OWNER", "WARDEN", "ACCOUNTANT", "SECURITY", "MAINTENANCE", "TENANT"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EmailInput(Input):
    email: EmailStr

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value


class LoginInput(EmailInput):
    password: str = Field(min_length=1, max_length=128)
    remember: bool = True


def validate_password(value: str) -> str:
    if len(value) < 12 or len(value) > 128 or not any(c.isalpha() for c in value) or not any(c.isdigit() for c in value):
        raise ValueError("Use 12-128 characters including a letter and a number")
    return value


class NewPassword(Input):
    new_password: str
    _password = field_validator("new_password")(validate_password)


class ResetInput(NewPassword):
    token: str = Field(min_length=20, max_length=256)


class ChangePasswordInput(NewPassword):
    current_password: str = Field(min_length=1, max_length=128)


class ProvisionInput(EmailInput):
    full_name: str = Field(min_length=1, max_length=120)
    role: RoleCode
    password: str
    _password = field_validator("password")(validate_password)

    @field_validator("full_name")
    @classmethod
    def validate_name(cls, value: str):
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()


class ProfileInput(Input):
    name: str = Field(min_length=1, max_length=120)
    avatar: str | None = Field(None, max_length=7_000_000)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value):
        if not value.strip():
            raise ValueError("Name is required")
        return value.strip()

    @field_validator("avatar")
    @classmethod
    def validate_avatar(cls, value):
        if value and value.startswith("data:"):
            try:
                header, encoded = value.split(",", 1)
                image = base64.b64decode(encoded, validate=True)
                signatures = {"data:image/jpeg;base64": image.startswith(b"\xff\xd8\xff"),
                    "data:image/png;base64": image.startswith(b"\x89PNG\r\n\x1a\n"),
                    "data:image/webp;base64": image.startswith(b"RIFF") and image[8:12] == b"WEBP"}
                if not signatures.get(header) or len(image) > 5 * 1024 * 1024:
                    raise ValueError("Invalid profile image")
            except (ValueError, binascii.Error):
                raise ValueError("Use a JPG, PNG, or WEBP profile image under 5 MB") from None
        elif value and (len(value) > 2048 or not value.startswith("https://")):
            raise ValueError("Avatar must be an HTTPS URL or a supported profile image")
        return value or None


class PublicUser(BaseModel):
    id: str
    name: str
    email: str
    roleId: RoleCode
    role: RoleCode
    roleLabel: str
    initials: str
    status: Literal["ACTIVE", "INACTIVE"]
    avatar: str | None = None
    propertyIds: list[str]


class SessionOutput(BaseModel):
    user: PublicUser
    role: RoleCode
    permissions: list[str]


class TokenOutput(SessionOutput):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class MessageOutput(BaseModel):
    message: str
