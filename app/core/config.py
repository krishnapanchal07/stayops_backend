from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    property_image_directory: str = "storage/property-images"
    property_max_images: int = Field(12, ge=1, le=12)
    app_name: str = "StayOps API"
    app_env: Literal["development", "test", "production"] = "development"
    database_url: SecretStr
    frontend_url: str = "http://localhost:5173"
    cors_origins: list[str] = ["http://localhost:5173"]
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256"] = "HS256"
    access_token_expire_minutes: int = Field(15, ge=1, le=60)
    refresh_token_expire_days: int = Field(7, ge=1, le=30)
    reset_token_expire_minutes: int = Field(20, ge=1, le=30)
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from_email: str = ""
    smtp_use_tls: bool = True
    razorpay_enabled: bool = False
    razorpay_mode: Literal['test'] = 'test'
    razorpay_key_id: SecretStr = SecretStr('')
    razorpay_key_secret: SecretStr = SecretStr('')
    razorpay_webhook_secret: SecretStr = SecretStr('')

    @property
    def razorpay_ready(self) -> bool:
        return self.razorpay_enabled and bool(self.razorpay_key_id.get_secret_value()
            and self.razorpay_key_secret.get_secret_value().strip())

    @model_validator(mode="after")
    def validate_security(self):
        key_id = self.razorpay_key_id.get_secret_value()
        if key_id and (not key_id.startswith('rzp_test_') or not key_id[9:].isalnum()):
            raise ValueError('Razorpay requires a valid Test Mode key ID')
        secret = self.jwt_secret_key.get_secret_value()
        if len(secret) < 32 or secret.startswith("<"):
            raise ValueError("Configure a random JWT_SECRET_KEY of at least 32 characters")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("CORS origins must be explicit HTTP(S) origins without paths")
        frontend = urlsplit(self.frontend_url)
        if frontend.scheme not in {"http", "https"} or not frontend.netloc or frontend.query or frontend.fragment:
            raise ValueError("FRONTEND_URL must be a trusted HTTP(S) URL")
        if self.app_env != "test" and not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Use PostgreSQL with the psycopg driver")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("SameSite=None requires Secure cookies")
        if self.app_env == "production":
            if not self.cookie_secure or not self.smtp_use_tls or frontend.scheme != "https" or any(not o.startswith("https://") for o in self.cors_origins):
                raise ValueError("Production requires HTTPS, Secure cookies, and SMTP TLS")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
